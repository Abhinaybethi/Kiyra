"""Email-OTP authentication primitives for the desktop overlay.

Design notes
------------
* OTPs are 6 random digits, stored only as an HMAC-SHA256 digest. A database
  leak therefore does not expose live codes.
* A code is single-use, expires after ``auth_otp_ttl_seconds`` and dies after
  ``auth_otp_max_attempts`` wrong guesses.
* Issuing a new code invalidates every older unconsumed code for that address,
  so only the newest email can be used.
* Session tokens are 256-bit URL-safe random strings; only their SHA-256
  digest is persisted. Revocation is a timestamp, not a delete, so an audit
  trail survives.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from config import settings
from db.models import AuthAccount, AuthOtp, AuthSession

_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[A-Za-z]{2,}$")
_NAME_RE = re.compile(r"[^\W\d_]", re.UNICODE)
_PHONE_ALLOWED_RE = re.compile(r"^\+?[\d\s().-]+$")

# HMAC key: configured value, otherwise a per-process random key. A random key
# means a backend restart invalidates outstanding OTPs — acceptable, and much
# safer than a hard-coded default.
_SECRET = (settings.auth_otp_secret or "").encode() or secrets.token_bytes(32)


# ── Errors ─────────────────────────────────────────────────────────────────

class AuthError(Exception):
    """Base class carrying a machine code, message and HTTP status."""

    status_code = 400
    code = "auth_error"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class ValidationError(AuthError):
    status_code = 422
    code = "invalid_input"


class AccountExists(AuthError):
    status_code = 409
    code = "account_exists"


class AccountNotFound(AuthError):
    status_code = 404
    code = "account_not_found"


class OtpInvalid(AuthError):
    status_code = 400
    code = "otp_invalid"


class OtpExpired(AuthError):
    status_code = 400
    code = "otp_expired"


class OtpLocked(AuthError):
    status_code = 429
    code = "otp_locked"


class RateLimited(AuthError):
    status_code = 429
    code = "rate_limited"


class Unauthorized(AuthError):
    status_code = 401
    code = "unauthorized"


# ── Validation ─────────────────────────────────────────────────────────────

def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def validate_email(email: str) -> str:
    email = normalize_email(email)
    if not email or len(email) > 255 or not _EMAIL_RE.match(email):
        raise ValidationError("Enter a valid email address.")
    return email


def normalize_phone(phone: str) -> str:
    raw = (phone or "").strip()
    if not raw:
        return ""
    if not _PHONE_ALLOWED_RE.match(raw):
        raise ValidationError("Phone number can only contain digits, spaces, +, -, ( and ).")
    has_plus = raw.startswith("+")
    digits = re.sub(r"\D", "", raw)
    if not 7 <= len(digits) <= 15:
        raise ValidationError("Enter a valid phone number (7–15 digits).")
    return ("+" if has_plus else "") + digits


def validate_full_name(name: str) -> str:
    name = " ".join((name or "").split())
    if len(name) < 2 or len(name) > 200 or not _NAME_RE.search(name):
        raise ValidationError("Enter your full name.")
    return name


# ── OTP ────────────────────────────────────────────────────────────────────

def _generate_code() -> str:
    digits = max(4, min(10, int(settings.auth_otp_length)))
    return str(secrets.randbelow(10 ** digits)).zfill(digits)


def _hash_code(email: str, purpose: str, code: str) -> str:
    payload = f"{email}:{purpose}:{code}".encode()
    return hmac.new(_SECRET, payload, hashlib.sha256).hexdigest()


def _utcnow() -> datetime:
    return datetime.utcnow()


@dataclass
class IssuedOtp:
    code: str
    expires_at: datetime
    ttl_seconds: int


def _latest_otp(db: Session, email: str, purpose: str) -> Optional[AuthOtp]:
    return (
        db.query(AuthOtp)
        .filter(AuthOtp.email == email, AuthOtp.purpose == purpose)
        .order_by(AuthOtp.created_at.desc(), AuthOtp.id.desc())
        .first()
    )


def _enforce_rate_limits(db: Session, email: str, purpose: str) -> None:
    now = _utcnow()
    cooldown = int(settings.auth_otp_resend_cooldown_seconds)
    latest = _latest_otp(db, email, purpose)
    if latest and cooldown > 0:
        elapsed = (now - latest.created_at).total_seconds()
        if elapsed < cooldown:
            raise RateLimited(
                f"Please wait {int(cooldown - elapsed) + 1}s before requesting another code.")

    window_start = now - timedelta(hours=1)
    recent = (
        db.query(func.count(AuthOtp.id))
        .filter(AuthOtp.email == email, AuthOtp.created_at >= window_start)
        .scalar()
        or 0
    )
    if recent >= int(settings.auth_otp_max_per_hour):
        raise RateLimited("Too many codes requested. Try again in an hour.")


def issue_otp(db: Session, email: str, purpose: str) -> IssuedOtp:
    """Create (and persist) a fresh code, invalidating older ones."""
    email = validate_email(email)
    _enforce_rate_limits(db, email, purpose)

    now = _utcnow()
    # Only the newest email may be redeemed.
    (
        db.query(AuthOtp)
        .filter(AuthOtp.email == email, AuthOtp.consumed_at.is_(None))
        .update({"consumed_at": now}, synchronize_session=False)
    )

    code = _generate_code()
    ttl = int(settings.auth_otp_ttl_seconds)
    row = AuthOtp(
        email=email,
        purpose=purpose,
        code_hash=_hash_code(email, purpose, code),
        expires_at=now + timedelta(seconds=ttl),
        max_attempts=int(settings.auth_otp_max_attempts),
    )
    db.add(row)
    db.commit()
    return IssuedOtp(code=code, expires_at=row.expires_at, ttl_seconds=ttl)


def verify_otp(db: Session, email: str, code: str) -> AuthOtp:
    """Redeem a code. Raises AuthError subclasses on every failure path."""
    email = normalize_email(email)
    code = (code or "").strip()
    if not email or not code:
        raise ValidationError("Email and code are required.")
    if not code.isdigit():
        raise OtpInvalid("Enter the 6-digit code from your email.")

    row = (
        db.query(AuthOtp)
        .filter(AuthOtp.email == email, AuthOtp.consumed_at.is_(None))
        .order_by(AuthOtp.created_at.desc(), AuthOtp.id.desc())
        .first()
    )
    if row is None:
        raise OtpInvalid("Request a new code to continue.")

    now = _utcnow()
    if row.expires_at <= now:
        row.consumed_at = now
        db.commit()
        raise OtpExpired("That code has expired. Request a new one.")

    if row.attempts >= row.max_attempts:
        row.consumed_at = now
        db.commit()
        raise OtpLocked("Too many incorrect attempts. Request a new code.")

    expected = _hash_code(email, row.purpose, code)
    if not hmac.compare_digest(expected, row.code_hash):
        row.attempts += 1
        remaining = max(0, row.max_attempts - row.attempts)
        if remaining == 0:
            row.consumed_at = now
        db.commit()
        if remaining == 0:
            raise OtpLocked("Too many incorrect attempts. Request a new code.")
        raise OtpInvalid(
            f"Incorrect code. {remaining} attempt{'s' if remaining != 1 else ''} left.")

    row.consumed_at = now
    db.commit()
    return row


# ── Accounts ───────────────────────────────────────────────────────────────

def get_account_by_email(db: Session, email: str) -> Optional[AuthAccount]:
    return db.query(AuthAccount).filter(AuthAccount.email == normalize_email(email)).first()


def register_account(db: Session, full_name: str, phone: str, email: str) -> AuthAccount:
    full_name = validate_full_name(full_name)
    phone = normalize_phone(phone)
    email = validate_email(email)

    account = get_account_by_email(db, email)
    if account and account.is_verified:
        raise AccountExists("An account already exists for this email. Sign in instead.")
    if account is None:
        account = AuthAccount(full_name=full_name, phone=phone, email=email)
        db.add(account)
    else:
        # Finish a previously abandoned signup with the latest details.
        account.full_name = full_name
        account.phone = phone
    db.commit()
    db.refresh(account)
    return account


def mark_verified(db: Session, account: AuthAccount) -> AuthAccount:
    now = _utcnow()
    account.is_verified = True
    account.last_login_at = now
    db.commit()
    db.refresh(account)
    return account


def update_account(db: Session, account: AuthAccount, *,
                   full_name: Optional[str] = None,
                   phone: Optional[str] = None) -> AuthAccount:
    if full_name is not None:
        account.full_name = validate_full_name(full_name)
    if phone is not None:
        account.phone = normalize_phone(phone)
    db.commit()
    db.refresh(account)
    return account


# ── Sessions ───────────────────────────────────────────────────────────────

def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(db: Session, account: AuthAccount) -> tuple[str, datetime]:
    token = secrets.token_urlsafe(32)
    expires_at = _utcnow() + timedelta(days=int(settings.auth_session_days))
    db.add(AuthSession(
        account_id=account.id,
        token_hash=_hash_token(token),
        expires_at=expires_at,
    ))
    db.commit()
    return token, expires_at


def account_from_token(db: Session, token: str) -> AuthAccount:
    token = (token or "").strip()
    if not token:
        raise Unauthorized("Sign in to continue.")
    session = (
        db.query(AuthSession)
        .filter(AuthSession.token_hash == _hash_token(token))
        .first()
    )
    now = _utcnow()
    if session is None or session.revoked_at is not None or session.expires_at <= now:
        raise Unauthorized("Your session has expired. Sign in again.")
    session.last_seen_at = now
    db.commit()
    return session.account


def revoke_session(db: Session, token: str) -> None:
    session = (
        db.query(AuthSession)
        .filter(AuthSession.token_hash == _hash_token((token or "").strip()))
        .first()
    )
    if session and session.revoked_at is None:
        session.revoked_at = _utcnow()
        db.commit()
