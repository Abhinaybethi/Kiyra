"""Strict email-OTP authentication: register → real email → verify → session.

There is no console fallback and no ``dev_code``. Tests simulate *delivery* by
capturing the code passed to the SMTP sender and simulate the random generator
with deterministic values — the backend itself takes the same code path as
production, including HMAC hashing, purpose binding and session creation.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta

import pytest

import api.auth as auth_api
import services.auth as auth_service
from db.models import AuthAccount, AuthOtp, AuthSession
from config import settings

EMAIL = "ada@example.com"
NAME = "Ada Lovelace"
PHONE = "+91 98765 43210"


@pytest.fixture(autouse=True)
def email_delivery(monkeypatch):
    """Make SMTP look configured and capture every delivered code."""
    monkeypatch.setattr(settings, "smtp_host", "smtp.test")
    monkeypatch.setattr(settings, "smtp_port", 587)
    monkeypatch.setattr(settings, "smtp_username", "kiyra@test.local")
    monkeypatch.setattr(settings, "smtp_password", "not-a-real-secret")
    monkeypatch.setattr(settings, "smtp_from", "Kiyra <kiyra@test.local>")

    delivered = []

    def _send(email, full_name, code, ttl):
        delivered.append({"email": email, "name": full_name,
                          "code": code, "ttl": ttl})
        return None

    monkeypatch.setattr(auth_api, "send_otp_email", _send)
    codes = iter(["123456", "654321", "111111", "222222", "333333",
                  "444444", "555555", "666666", "777777", "888888",
                  "999000", "000999"])
    monkeypatch.setattr(auth_service, "_generate_code", lambda: next(codes))
    return delivered


def _register(client, **overrides):
    payload = {"full_name": NAME, "phone": PHONE, "email": EMAIL}
    payload.update(overrides)
    return client.post("/api/auth/register", json=payload)


def _login(client, email=EMAIL):
    return client.post("/api/auth/login", json={"email": email})


def _verify(client, code, email=EMAIL, purpose="register"):
    return client.post("/api/auth/verify", json={
        "email": email, "code": code, "purpose": purpose})


def _code_from(resp, email_delivery) -> str:
    body = resp.json()
    assert body["delivery"] == "email"
    assert "dev_code" not in body
    matches = [d for d in email_delivery if d["email"] == email_delivery[-1]["email"]]
    return matches[-1]["code"]


def _signed_in(client, email_delivery):
    code = _code_from(_register(client), email_delivery)
    return _verify(client, code).json()["token"]


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
    assert _register(client).status_code == 200
    account = db_session.query(AuthAccount).filter_by(email=EMAIL).one()
    assert account.phone == "+919876543210"


def test_email_is_normalized_to_lowercase(client, db_session):
    assert _register(client, email="  ADA@Example.COM ").status_code == 200
    assert db_session.query(AuthAccount).filter_by(email=EMAIL).count() == 1


# ── strict delivery ────────────────────────────────────────────────────────

def test_challenge_never_contains_the_code(client, email_delivery):
    resp = _register(client)
    assert resp.status_code == 200
    body = resp.json()
    assert body["delivery"] == "email"
    assert body["otp_length"] == 6
    assert "dev_code" not in body
    assert email_delivery[-1]["code"] not in json.dumps(body)


def test_missing_smtp_fails_closed_without_issuing_a_code(
        client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "smtp_host", "")
    resp = _register(client)
    assert resp.status_code == 503
    assert "email verification is not configured" in resp.json()["detail"].lower()
    assert db_session.query(AuthOtp).count() == 0
    assert db_session.query(AuthAccount).count() == 1  # account created, code not


def test_missing_smtp_password_fails_closed_before_issuing_a_code(
        client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "smtp_password", "")
    resp = _register(client)
    assert resp.status_code == 503
    assert "email verification is not configured" in resp.json()["detail"].lower()
    assert db_session.query(AuthOtp).count() == 0


def test_missing_otp_secret_fails_closed(client, monkeypatch):
    monkeypatch.setattr(settings, "auth_otp_secret", "")
    resp = _register(client)
    assert resp.status_code == 503
    assert "AUTH_OTP_SECRET" in resp.json()["detail"]


def test_register_issues_a_hashed_otp(client, db_session, email_delivery):
    resp = _register(client)
    code = _code_from(resp, email_delivery)
    row = db_session.query(AuthOtp).filter_by(email=EMAIL).one()
    assert row.code_hash != code
    assert len(row.code_hash) == 64
    assert row.purpose == "register"
    assert row.consumed_at is None


def test_duplicate_verified_account_cannot_reregister(client, email_delivery):
    code = _code_from(_register(client), email_delivery)
    assert _verify(client, code).status_code == 200
    resp = _register(client)
    assert resp.status_code == 409
    assert "already exists" in resp.json()["detail"].lower()


def test_abandoned_signup_can_be_retried(client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "auth_otp_resend_cooldown_seconds", 0)
    assert _register(client).status_code == 200
    resp = _register(client, full_name="Ada Byron", phone="9999999999")
    assert resp.status_code == 200
    account = db_session.query(AuthAccount).filter_by(email=EMAIL).one()
    assert account.full_name == "Ada Byron"
    assert account.is_verified is False


# ── verification ───────────────────────────────────────────────────────────

def test_verify_creates_session_and_marks_account_verified(
        client, db_session, email_delivery):
    code = _code_from(_register(client), email_delivery)
    resp = _verify(client, code)
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert len(body["token"]) >= 32
    assert body["account"]["email"] == EMAIL
    assert body["account"]["is_verified"] is True

    account = db_session.query(AuthAccount).filter_by(email=EMAIL).one()
    assert account.last_login_at is not None
    session = db_session.query(AuthSession).filter_by(account_id=account.id).one()
    assert session.token_hash != body["token"]
    assert session.token_hash == hashlib.sha256(body["token"].encode()).hexdigest()
    assert session.revoked_at is None


def test_code_is_bound_to_its_purpose(client, email_delivery):
    register_code = _code_from(_register(client), email_delivery)
    # A registration code must not verify a login challenge.
    assert _verify(client, register_code, purpose="login").status_code == 400
    assert _verify(client, register_code).status_code == 200

    resp = _login(client)
    assert resp.json()["purpose"] == "login"
    login_code = _code_from(resp, email_delivery)
    assert _verify(client, login_code, purpose="register").status_code == 400
    assert _verify(client, login_code, purpose="login").status_code == 200


def test_unverified_account_cannot_sign_in(client, email_delivery):
    assert _register(client).status_code == 200  # code deliberately not redeemed
    resp = _login(client)
    assert resp.status_code == 401
    assert "not verified" in resp.json()["detail"].lower()


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


def test_code_is_single_use(client, email_delivery):
    code = _code_from(_register(client), email_delivery)
    assert _verify(client, code).status_code == 200
    assert _verify(client, code).status_code == 400


def test_new_code_invalidates_the_previous_one(client, monkeypatch, email_delivery):
    monkeypatch.setattr(settings, "auth_otp_resend_cooldown_seconds", 0)
    first = _code_from(_register(client), email_delivery)
    second = _code_from(_register(client), email_delivery)
    assert first != second
    assert _verify(client, first).status_code == 400
    assert _verify(client, second).status_code == 200


def test_verify_unknown_email_is_404(client):
    assert _verify(client, "123456", email="nobody@example.com").status_code == 404


def test_verify_rejects_unknown_purpose(client):
    _register(client)
    assert _verify(client, "123456", purpose="magic").status_code == 422


# ── sign-in ────────────────────────────────────────────────────────────────

def test_login_unknown_email_is_404(client):
    resp = _login(client, email="nobody@example.com")
    assert resp.status_code == 404
    assert "no account" in resp.json()["detail"].lower()


def test_login_sends_a_new_code_and_verifies(client, email_delivery):
    code = _code_from(_register(client), email_delivery)
    assert _verify(client, code).status_code == 200

    resp = _login(client)
    assert resp.status_code == 200
    assert resp.json()["purpose"] == "login"
    code = _code_from(resp, email_delivery)
    assert _verify(client, code, purpose="login").status_code == 200


def test_resend_respects_the_cooldown(client):
    _register(client)
    resp = client.post("/api/auth/resend", json={"email": EMAIL, "purpose": "register"})
    assert resp.status_code == 429
    assert "wait" in resp.json()["detail"].lower()


def test_verified_account_cannot_resend_a_register_code(
        client, email_delivery, monkeypatch):
    code = _code_from(_register(client), email_delivery)
    assert _verify(client, code).status_code == 200
    monkeypatch.setattr(settings, "auth_otp_resend_cooldown_seconds", 0)
    login_code = _code_from(_login(client), email_delivery)

    resp = client.post(
        "/api/auth/resend", json={"email": EMAIL, "purpose": "register"})

    assert resp.status_code == 401
    # The valid login code must survive an unauthenticated register-purpose hit.
    assert _verify(client, login_code, purpose="login").status_code == 200


def test_issuing_an_otp_only_invalidates_its_own_purpose(
        client, db_session, email_delivery, monkeypatch):
    monkeypatch.setattr(settings, "auth_otp_resend_cooldown_seconds", 0)
    register = auth_service.issue_otp(db_session, EMAIL, "register")
    login = auth_service.issue_otp(db_session, EMAIL, "login")
    second_register = auth_service.issue_otp(db_session, EMAIL, "register")

    by_id = {row.id: row for row in db_session.query(AuthOtp).filter_by(email=EMAIL).all()}
    assert by_id[register.id].consumed_at is not None
    assert by_id[second_register.id].consumed_at is None
    assert by_id[login.id].consumed_at is None


def test_failed_delivery_deletes_only_the_issued_code(
        client, db_session, email_delivery, monkeypatch):
    monkeypatch.setattr(settings, "auth_otp_resend_cooldown_seconds", 0)
    other = auth_service.issue_otp(db_session, EMAIL, "login")
    monkeypatch.setattr(
        auth_api, "send_otp_email", lambda *args, **kwargs: "SMTP down")

    assert _register(client).status_code == 502

    remaining = {row.id for row in db_session.query(AuthOtp).filter_by(email=EMAIL).all()}
    assert remaining == {other.id}


def test_resend_unknown_email_is_404(client):
    assert client.post("/api/auth/resend",
                       json={"email": "nobody@example.com"}).status_code == 404


# ── session endpoints ──────────────────────────────────────────────────────

def test_me_returns_the_signed_in_account(client, email_delivery):
    token = _signed_in(client, email_delivery)
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


def test_logout_revokes_the_session(client, email_delivery):
    token = _signed_in(client, email_delivery)
    headers = {"Authorization": f"Bearer {token}"}
    assert client.post("/api/auth/logout", headers=headers).status_code == 200
    assert client.get("/api/auth/me", headers=headers).status_code == 401


def test_expired_session_is_rejected(client, db_session, email_delivery):
    token = _signed_in(client, email_delivery)
    session = db_session.query(AuthSession).one()
    session.expires_at = datetime.utcnow() - timedelta(seconds=1)
    db_session.commit()
    resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    assert "expired" in resp.json()["detail"].lower()


def test_account_details_can_be_updated(client, email_delivery):
    token = _signed_in(client, email_delivery)
    resp = client.patch("/api/auth/account",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"full_name": "Ada Byron", "phone": "9999999999"})
    assert resp.status_code == 200
    assert resp.json()["full_name"] == "Ada Byron"
    assert resp.json()["phone"] == "9999999999"


def test_account_update_rejects_bad_phone(client, email_delivery):
    token = _signed_in(client, email_delivery)
    resp = client.patch("/api/auth/account",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"phone": "abc"})
    assert resp.status_code == 422


# ── capabilities ───────────────────────────────────────────────────────────

def test_status_reports_email_only(client):
    resp = client.get("/api/auth/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True
    assert body["email_delivery_required"] is True
    assert body["smtp_configured"] is True
    assert body["otp_length"] == 6


def test_disabled_signin_blocks_new_codes(client, monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", False)
    resp = _register(client)
    assert resp.status_code == 503
    assert "disabled" in resp.json()["detail"].lower()
