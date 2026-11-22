"""Desktop authentication routes: register → email OTP → session.

Everything here is local-first: the overlay talks to the same backend it
already uses, and email delivery is optional (console fallback). Endpoints are
synchronous on purpose so FastAPI runs the blocking SMTP call in its threadpool
instead of stalling the event loop.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from config import settings
from db.database import get_db
from db.models import AuthOtp
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
    delivery = "email" if smtp_configured() else "console"
    dev_code = issued.code if (delivery == "console" or settings.auth_dev_otp) else None
    if delivery == "email":
        message = f"We emailed a {issued.ttl_seconds // 60}-minute code to {email}."
    else:
        message = ("Email is not configured on this machine, so the code was "
                   "printed to the backend console.")
    return AuthChallengeResponse(
        purpose=purpose,
        email=email,
        delivery=delivery,
        expires_in=issued.ttl_seconds,
        resend_in=int(settings.auth_otp_resend_cooldown_seconds),
        dev_code=dev_code,
        message=message,
    )


def _require_signin_enabled() -> None:
    if not settings.auth_enabled:
        raise HTTPException(
            status_code=503,
            detail="New sign-ins are disabled on this server.")


def _issue_and_send(db: Session, email: str, purpose: str,
                    full_name: str) -> AuthChallengeResponse:
    _require_signin_enabled()
    try:
        issued = auth.issue_otp(db, email, purpose)
    except auth.AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    if smtp_configured():
        delivery_error = send_otp_email(
            email, full_name, issued.code, issued.ttl_seconds)
        if delivery_error:
            # Do not leave a code the user cannot possibly read.
            (
                db.query(AuthOtp)
                .filter(AuthOtp.email == email, AuthOtp.consumed_at.is_(None))
                .delete(synchronize_session=False)
            )
            db.commit()
            raise HTTPException(
                status_code=502,
                detail=f"Could not send the verification email: {delivery_error}")
    else:
        # Console fallback — make the code impossible to miss in the log.
        print(f"[AUTH] OTP for {email} ({purpose}): {issued.code} "
              f"(expires in {issued.ttl_seconds // 60} min)")
    return _challenge(purpose, email, issued)


def _bearer(authorization: Optional[str]) -> str:
    if not authorization:
        raise HTTPException(status_code=401, detail="Sign in to continue.")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="Sign in to continue.")
    return token.strip()


@router.get("/status")
def auth_status():
    """Capabilities for diagnostics and clients."""
    return {
        "enabled": bool(settings.auth_enabled),
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
    except auth.AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    purpose = "register" if data.purpose == "register" else "login"
    return _issue_and_send(db, account.email, purpose, account.full_name)


@router.post("/verify", response_model=AuthSessionResponse)
def verify(data: VerifyOtpRequest, db: Session = Depends(get_db)):
    _require_signin_enabled()
    try:
        email = auth.validate_email(data.email)
        account = auth.get_account_by_email(db, email)
        if account is None:
            raise auth.AccountNotFound(
                "No account found for this email. Create an account first.")
        auth.verify_otp(db, email, data.code)
        account = auth.mark_verified(db, account)
        token, expires_at = auth.create_session(db, account)
    except auth.AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
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
