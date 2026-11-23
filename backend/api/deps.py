"""Shared auth dependencies for every non-public API route.

The desktop wizard and the web client both receive an opaque bearer token from
``POST /api/auth/verify``. HTTP routes accept only ``Authorization: Bearer …``;
the live WebSocket also accepts the same token as ``?token=<token>`` because
browser WebSocket APIs cannot set arbitrary headers. Keeping those paths apart
stops REST tokens from leaking into URLs, access logs and Referer headers.

Tests override these dependencies (``app.dependency_overrides``) instead of
spraying fake tokens across hundreds of unit tests.
"""
from __future__ import annotations

from typing import Optional

from fastapi import Depends, Header, HTTPException, WebSocket, WebSocketException
from sqlalchemy.orm import Session

from config import settings
from db.database import get_db
from db.models import AuthAccount
from services import auth


def bearer_token(authorization: Optional[str],
                 token: Optional[str] = None,
                 *,
                 websocket: bool = False) -> str:
    """Parse ``Authorization: Bearer <token>``.

    The query fallback is permitted only for WebSocket scope. REST callers must
    use the header so the token is not written into URLs or access logs.
    """
    if websocket and not authorization and token:
        authorization = f"Bearer {token}"
    if not authorization:
        raise HTTPException(status_code=401, detail="Sign in to continue.")
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        raise HTTPException(status_code=401, detail="Sign in to continue.")
    return value.strip()


def require_account(
    authorization: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> AuthAccount:
    """Validate the bearer header and return the signed-in account (HTTP)."""
    if settings.dev_auth_bypass:
        return auth.get_or_create_dev_account(db)
    try:
        return auth.account_from_token(db, bearer_token(authorization))
    except auth.AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)


def require_account_ws(
    websocket: WebSocket,
    db: Session = Depends(get_db),
) -> AuthAccount:
    """Validate a live-session token from the header or WS query string."""
    if settings.dev_auth_bypass:
        return auth.get_or_create_dev_account(db)
    authorization = websocket.headers.get("authorization")
    token = websocket.query_params.get("token")
    try:
        return auth.account_from_token(
            db,
            bearer_token(authorization, token, websocket=True),
        )
    except auth.AuthError as exc:
        raise WebSocketException(code=4401, reason=exc.message)
    except HTTPException as exc:
        raise WebSocketException(code=4401, reason=str(exc.detail))
