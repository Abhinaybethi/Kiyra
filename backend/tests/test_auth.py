"""Email-OTP authentication: register → verify → session → sign-in."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from db.models import AuthAccount, AuthOtp, AuthSession
from config import settings

EMAIL = "ada@example.com"
NAME = "Ada Lovelace"
PHONE = "+91 98765 43210"


def _register(client, **overrides):
    payload = {"full_name": NAME, "phone": PHONE, "email": EMAIL}
    payload.update(overrides)
    return client.post("/api/auth/register", json=payload)


def _login(client, email=EMAIL):
    return client.post("/api/auth/login", json={"email": email})


def _verify(client, code, email=EMAIL):
    return client.post("/api/auth/verify", json={"email": email, "code": code})


def _code_from(resp) -> str:
    body = resp.json()
    assert body["delivery"] == "console"
    return body["dev_code"]


# ── validation ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("field,value,fragment", [
    ("full_name", "", "full name"),
    ("full_name", "   ", "full name"),
    ("full_name", "12345", "full name"),
    ("phone", "abc", "Phone number"),
    ("phone", "12", "valid phone"),
    ("email", "not-an-email", "valid email"),
    ("email", "a@b", "valid email"),
])
def test_register_rejects_invalid_input(client, field, value, fragment):
    resp = _register(client, **{field: value})
    assert resp.status_code == 422
    assert fragment.lower() in resp.json()["detail"].lower()


def test_phone_is_normalized(client, db_session):
    resp = _register(client)
    assert resp.status_code == 200
    account = db_session.query(AuthAccount).filter_by(email=EMAIL).one()
    assert account.phone == "+919876543210"


def test_email_is_normalized_to_lowercase(client, db_session):
    resp = _register(client, email="  ADA@Example.COM ")
    assert resp.status_code == 200
    assert db_session.query(AuthAccount).filter_by(email=EMAIL).count() == 1


# ── registration + OTP ─────────────────────────────────────────────────────

def test_register_issues_a_hashed_otp(client, db_session):
    resp = _register(client)
    assert resp.status_code == 200
    body = resp.json()
    assert body["purpose"] == "register"
    assert body["delivery"] == "console"
    assert len(body["dev_code"]) == 6
    assert body["expires_in"] > 0

    row = db_session.query(AuthOtp).filter_by(email=EMAIL).one()
    assert row.code_hash != body["dev_code"]
    assert len(row.code_hash) == 64
    assert row.consumed_at is None


def test_duplicate_verified_account_cannot_reregister(client):
    code = _code_from(_register(client))
    assert _verify(client, code).status_code == 200
    resp = _register(client)
    assert resp.status_code == 409
    assert "already exists" in resp.json()["detail"].lower()


def test_abandoned_signup_can_be_retried(client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "auth_otp_resend_cooldown_seconds", 0)
    assert _register(client).status_code == 200
    # A second attempt before verification refreshes the pending account.
    resp = _register(client, full_name="Ada Byron", phone="9999999999")
    assert resp.status_code == 200
    account = db_session.query(AuthAccount).filter_by(email=EMAIL).one()
    assert account.full_name == "Ada Byron"
    assert account.phone == "9999999999"
    assert account.is_verified is False


# ── verification ───────────────────────────────────────────────────────────

def test_verify_creates_session_and_marks_account_verified(client, db_session):
    code = _code_from(_register(client))
    resp = _verify(client, code)
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert len(body["token"]) >= 32
    assert body["account"]["email"] == EMAIL
    assert body["account"]["full_name"] == NAME
    assert body["account"]["is_verified"] is True

    account = db_session.query(AuthAccount).filter_by(email=EMAIL).one()
    assert account.is_verified is True
    assert account.last_login_at is not None
    session = db_session.query(AuthSession).filter_by(account_id=account.id).one()
    assert session.token_hash != body["token"]
    assert session.revoked_at is None


def test_wrong_code_reports_remaining_attempts(client, db_session):
    _register(client)
    resp = _verify(client, "000000")
    assert resp.status_code == 400
    assert "attempt" in resp.json()["detail"].lower()
    row = db_session.query(AuthOtp).filter_by(email=EMAIL).one()
    assert row.attempts == 1


def test_code_locks_after_max_attempts(client):
    _register(client)
    for _ in range(4):
        assert _verify(client, "000000").status_code == 400
    resp = _verify(client, "000000")
    assert resp.status_code == 429
    assert "too many" in resp.json()["detail"].lower()


def test_expired_code_is_rejected(client, db_session):
    _register(client)
    row = db_session.query(AuthOtp).filter_by(email=EMAIL).one()
    row.expires_at = datetime.utcnow() - timedelta(seconds=1)
    db_session.commit()

    resp = _verify(client, "123456")
    assert resp.status_code == 400
    assert "expired" in resp.json()["detail"].lower()


def test_code_is_single_use(client):
    code = _code_from(_register(client))
    assert _verify(client, code).status_code == 200
    assert _verify(client, code).status_code == 400


def test_new_code_invalidates_the_previous_one(client, monkeypatch):
    monkeypatch.setattr(settings, "auth_otp_resend_cooldown_seconds", 0)
    first = _code_from(_register(client))
    second = _code_from(_register(client))
    assert first != second
    assert _verify(client, first).status_code == 400
    assert _verify(client, second).status_code == 200


def test_verify_unknown_email_is_404(client):
    resp = _verify(client, "123456", email="nobody@example.com")
    assert resp.status_code == 404


# ── sign-in ────────────────────────────────────────────────────────────────

def test_login_unknown_email_is_404(client):
    resp = _login(client, email="nobody@example.com")
    assert resp.status_code == 404
    assert "no account" in resp.json()["detail"].lower()


def test_login_sends_a_new_code_and_verifies(client):
    code = _code_from(_register(client))
    assert _verify(client, code).status_code == 200

    resp = _login(client)
    assert resp.status_code == 200
    assert resp.json()["purpose"] == "login"
    code = _code_from(resp)
    assert _verify(client, code).status_code == 200


def test_resend_respects_the_cooldown(client):
    _register(client)
    resp = client.post("/api/auth/resend", json={"email": EMAIL, "purpose": "register"})
    assert resp.status_code == 429
    assert "wait" in resp.json()["detail"].lower()


def test_resend_unknown_email_is_404(client):
    resp = client.post("/api/auth/resend", json={"email": "nobody@example.com"})
    assert resp.status_code == 404


# ── session endpoints ──────────────────────────────────────────────────────

def _signed_in(client):
    code = _code_from(_register(client))
    return _verify(client, code).json()["token"]


def test_me_returns_the_signed_in_account(client):
    token = _signed_in(client)
    resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["email"] == EMAIL


@pytest.mark.parametrize("headers", [
    {},
    {"Authorization": "Bearer nonsense"},
    {"Authorization": "Basic abc"},
    {"Authorization": "Bearer "},
])
def test_me_rejects_bad_tokens(client, headers):
    assert client.get("/api/auth/me", headers=headers).status_code == 401


def test_logout_revokes_the_session(client):
    token = _signed_in(client)
    headers = {"Authorization": f"Bearer {token}"}
    assert client.post("/api/auth/logout", headers=headers).status_code == 200
    assert client.get("/api/auth/me", headers=headers).status_code == 401


def test_expired_session_is_rejected(client, db_session):
    token = _signed_in(client)
    session = db_session.query(AuthSession).one()
    session.expires_at = datetime.utcnow() - timedelta(seconds=1)
    db_session.commit()
    resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    assert "expired" in resp.json()["detail"].lower()


def test_account_details_can_be_updated(client):
    token = _signed_in(client)
    headers = {"Authorization": f"Bearer {token}"}
    resp = client.patch("/api/auth/account", headers=headers,
                        json={"full_name": "Ada Byron", "phone": "9999999999"})
    assert resp.status_code == 200
    assert resp.json()["full_name"] == "Ada Byron"
    assert resp.json()["phone"] == "9999999999"


def test_account_update_rejects_bad_phone(client):
    token = _signed_in(client)
    resp = client.patch("/api/auth/account",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"phone": "abc"})
    assert resp.status_code == 422


# ── capabilities ───────────────────────────────────────────────────────────

def test_status_reports_console_fallback(client):
    resp = client.get("/api/auth/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True
    assert body["smtp_configured"] is False
    assert body["otp_length"] == 6
    assert body["resend_cooldown_seconds"] > 0


def test_disabled_signin_blocks_new_codes(client, monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", False)
    resp = _register(client)
    assert resp.status_code == 503
    assert "disabled" in resp.json()["detail"].lower()
