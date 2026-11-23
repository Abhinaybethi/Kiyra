"""Bearer-token helpers for the manual live/bench harnesses.

The backend enforces the email-OTP session token on every API and WebSocket
route. Sign in once through the wizard (or /api/auth/login → /api/auth/verify)
and export the returned token before running a harness:

    $env:KIYRA_AUTH_TOKEN = "<token from /api/auth/verify>"

No bypass or dev code is supported — these helpers only forward the real
session token the user obtained from their inbox.
"""
from __future__ import annotations

import os

_TOKEN_ENV = "KIYRA_AUTH_TOKEN"


def require_token() -> str:
    token = (os.environ.get(_TOKEN_ENV) or "").strip()
    if not token:
        raise SystemExit(
            f"Set {_TOKEN_ENV} to the bearer token returned by "
            "/api/auth/verify before running this live harness.")
    return token


def http_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {require_token()}"}


def ws_connect_headers() -> dict[str, str]:
    # websockets >=14 uses additional_headers (desktop runtime is 15.x).
    return {"Authorization": f"Bearer {require_token()}"}
