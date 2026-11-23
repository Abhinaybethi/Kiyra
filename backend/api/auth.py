"""Desktop authentication routes: register → email OTP → session.

There is exactly one way to sign in: request a code, receive it by email, and
redeem it here. No console fallback, no ``dev_code`` in responses, no
auto-approval. Endpoints are synchronous on purpose so FastAPI runs the
blocking SMTP call in its threadpool instead of stalling the event loop.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from config import settings
from db.database import get_db
from db.models import AuthOtp
from api.deps import bearer_token as _bearer
from api.schemas import (
    AccountUpdateRequest,
    AuthAccountResponse,
    AuthChallengeResponse,
    AuthSessionResponse,
    LoginRequest,
    RegisterRequest,
    ResendOtpRequest,
    VerifyOtpRequest,
    OKResponse,
)
from services import auth
from services.emailer import send_otp_email, smtp_configured

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _challenge(purpose: str, email: str,
               issued: auth.IssuedOtp) -> AuthChallengeResponse:
    # The code is NEVER part of this response — only the mailbox that received
    # it can supply it to /verify.
    return AuthChallengeResponse(
        purpose=purpose,
        email=email,
        delivery="email",
        otp_length=len(issued.code),
        expires_in=issued.ttl_seconds,
        resend_in=int(settings.auth_otp_resend_cooldown_seconds),
        message=(f"We emailed a {len(issued.code)}-digit code to {email}. "
                 f"It expires in {issued.ttl_seconds // 60} minutes."),
    )


def _require_signin_enabled() -> None:
    if not settings.auth_enabled:
        raise HTTPException(
            status_code=503,
            detail="New sign-ins are disabled on this server.")


def _require_email_delivery() -> None:
    """Fail closed: without SMTP there is no legitimate way to receive a code."""
    if not smtp_configured():
        raise HTTPException(
            status_code=503,
            detail=("Email verification is not configured on this server. "
                    "Set SMTP_HOST, SMTP_USERNAME, SMTP_PASSWORD and SMTP_FROM "
                    "in backend/.env, then restart the backend."))


def _issue_and_send(db: Session, email: str, purpose: str,
                    full_name: str) -> AuthChallengeResponse:
    _require_signin_enabled()
    _require_email_delivery()
    try:
        issued = auth.issue_otp(db, email, purpose)
    except auth.AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    delivery_error = send_otp_email(
        email, full_name, issued.code, issued.ttl_seconds)
    if delivery_error:
        # Do not leave a code the user cannot possibly read.
        (
            db.query(AuthOtp)
            .filter(AuthOtp.id == issued.id)
            .delete(synchronize_session=False)
        )
        db.commit()
        raise HTTPException(
            status_code=502,
            detail=f"Could not send the verification email: {delivery_error}")
    return _challenge(purpose, email, issued)


@router.get("/status")
def auth_status():
    """Capabilities for diagnostics and clients."""
    return {
        "enabled": bool(settings.auth_enabled),
        "email_delivery_required": True,
        "smtp_configured": smtp_configured(),
        "otp_length": int(settings.auth_otp_length),
        "otp_ttl_seconds": int(settings.auth_otp_ttl_seconds),
        "resend_cooldown_seconds": int(settings.auth_otp_resend_cooldown_seconds),
        "session_days": int(settings.auth_session_days),
    }


@router.post("/register", response_model=AuthChallengeResponse)
def register(data: RegisterRequest, db: Session = Depends(get_db)):
    try:
        account = auth.register_account(db, data.full_name, data.phone, data.email)
    except auth.AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    return _issue_and_send(db, account.email, "register", account.full_name)


@router.post("/login", response_model=AuthChallengeResponse)
def login(data: LoginRequest, db: Session = Depends(get_db)):
    try:
        email = auth.validate_email(data.email)
        account = auth.get_account_by_email(db, email)
        if account is None:
            raise auth.AccountNotFound(
                "No account found for this email. Create an account first.")
        if not account.is_verified:
            raise auth.Unauthorized(
                "This account is not verified yet. Finish the code sent when "
                "you registered, or create the account again.")
    except auth.AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    return _issue_and_send(db, account.email, "login", account.full_name)


@router.post("/resend", response_model=AuthChallengeResponse)
def resend(data: ResendOtpRequest, db: Session = Depends(get_db)):
    try:
        email = auth.validate_email(data.email)
        account = auth.get_account_by_email(db, email)
        if account is None:
            raise auth.AccountNotFound(
                "No account found for this email. Create an account first.")
        purpose = "register" if data.purpose == "register" else "login"
        if purpose == "login" and not account.is_verified:
            raise auth.Unauthorized("Verify your account before signing in.")
        if purpose == "register" and account.is_verified:
            # Verified accounts sign in with a login code. Allowing a fresh
            # register challenge here lets anyone invalidate that login code.
            raise auth.Unauthorized(
                "This account is already verified. Sign in instead.")
    except auth.AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    return _issue_and_send(db, account.email, purpose, account.full_name)


@router.post("/verify", response_model=AuthSessionResponse)
def verify(data: VerifyOtpRequest, db: Session = Depends(get_db)):
    _require_signin_enabled()
    _require_email_delivery()
    try:
        email = auth.validate_email(data.email)
        account = auth.get_account_by_email(db, email)
        if account is None:
            raise auth.AccountNotFound(
                "No account found for this email. Create an account first.")
        if data.purpose not in ("register", "login"):
            raise auth.ValidationError("Unknown verification purpose.")
        otp = auth.verify_otp(db, email, data.code, data.purpose)
        if otp.purpose == "login" and not account.is_verified:
            raise auth.Unauthorized("Verify your account before signing in.")
        account = auth.mark_verified(db, account)
        token, expires_at = auth.create_session(db, account)
    except auth.AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return AuthSessionResponse(
        token=token,
        expires_at=expires_at,
        account=AuthAccountResponse.model_validate(account),
    )


@router.get("/me", response_model=AuthAccountResponse)
def me(authorization: Optional[str] = Header(default=None),
       db: Session = Depends(get_db)):
    try:
        account = auth.account_from_token(db, _bearer(authorization))
    except auth.AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    return AuthAccountResponse.model_validate(account)


@router.post("/logout", response_model=OKResponse)
def logout(authorization: Optional[str] = Header(default=None),
           db: Session = Depends(get_db)):
    try:
        auth.revoke_session(db, _bearer(authorization))
    except auth.AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    return OKResponse(message="Signed out")


@router.patch("/account", response_model=AuthAccountResponse)
def update_account(
    data: AccountUpdateRequest,
    authorization: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
):
    try:
        account = auth.account_from_token(db, _bearer(authorization))
        account = auth.update_account(
            db, account,
            full_name=data.full_name,
            phone=data.phone,
        )
    except auth.AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    return AuthAccountResponse.model_validate(account)
