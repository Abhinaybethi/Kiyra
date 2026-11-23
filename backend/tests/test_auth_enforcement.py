"""Bearer-token enforcement on every non-public route, including WebSockets."""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from api.deps import require_account, require_account_ws
from config import settings
from db.models import AuthAccount, AuthSession
from main import app

TOKEN = "enforcement-test-token-0123456789"


@pytest.fixture
def raw_client(client):
    """Remove the unit-test overrides so real enforcement runs."""
    app.dependency_overrides.pop(require_account, None)
    app.dependency_overrides.pop(require_account_ws, None)
    yield client


@pytest.fixture
def signed_in(db_session):
    account = AuthAccount(
        full_name="Enforcement User", phone="+919999999999",
        email="enforce@example.com", is_verified=True)
    db_session.add(account)
    db_session.commit()
    db_session.add(AuthSession(
        account_id=account.id,
        token_hash=hashlib.sha256(TOKEN.encode()).hexdigest(),
        expires_at=datetime.utcnow() + timedelta(days=1),
    ))
    db_session.commit()
    return TOKEN


@pytest.mark.parametrize("path", [
    "/api/profile",
    "/api/resume",
    "/api/jobs",
    "/api/knowledge",
    "/api/interviews",
    "/api/analytics/dashboard",
    "/api/settings/models",
    "/api/transcription/health",
])
def test_protected_routes_require_a_token(raw_client, path):
    resp = raw_client.get(path)
    assert resp.status_code == 401, path
    assert "sign in" in resp.json()["detail"].lower()


def test_protected_route_accepts_a_valid_session(raw_client, signed_in):
    resp = raw_client.get(
        "/api/profile",
        headers={"Authorization": f"Bearer {signed_in}"},
    )
    assert resp.status_code == 200


def test_bad_token_is_rejected(raw_client):
    resp = raw_client.get(
        "/api/jobs",
        headers={"Authorization": "Bearer nonsense"},
    )
    assert resp.status_code == 401


def test_rest_query_token_is_rejected(raw_client, signed_in):
    """Tokens belong in headers on REST; URLs leak into logs and history."""
    resp = raw_client.get("/api/profile", params={"token": signed_in})
    assert resp.status_code == 401


def test_auth_challenge_routes_stay_public(raw_client):
    assert raw_client.get("/api/auth/status").status_code == 200
    assert raw_client.post(
        "/api/auth/login",
        json={"email": "nobody@example.com"}).status_code == 404


def test_health_stays_public(raw_client):
    assert raw_client.get("/health").status_code == 200


def test_api_docs_are_not_published(raw_client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert raw_client.get(path).status_code == 404, path


def test_live_websocket_requires_a_token(raw_client):
    """Router dependencies apply to WebSocket routes too."""
    with pytest.raises(WebSocketDisconnect) as exc:
        with raw_client.websocket_connect("/api/interviews/1/ws"):
            pass
    assert exc.value.code == 4401


def test_live_websocket_accepts_header_token(raw_client, signed_in):
    """Desktop clients send the bearer token in the Authorization header."""
    with raw_client.websocket_connect(
        "/api/interviews/999999/ws",
        headers={"Authorization": f"Bearer {signed_in}"},
    ) as websocket:
        with pytest.raises(WebSocketDisconnect) as exc:
            websocket.receive_text()
    assert exc.value.code == 4004  # auth passed; session simply does not exist


def test_live_websocket_accepts_query_token(raw_client, signed_in):
    """Browser WebSocket APIs cannot set headers, so ?token is allowed here."""
    with raw_client.websocket_connect(
        f"/api/interviews/999999/ws?token={signed_in}"
    ) as websocket:
        with pytest.raises(WebSocketDisconnect) as exc:
            websocket.receive_text()
    assert exc.value.code == 4004


# ── local development bypass (opt-in, never the default) ───────────────────

@pytest.fixture
def dev_bypass(monkeypatch):
    monkeypatch.setattr(settings, "dev_auth_bypass", True)


def test_dev_bypass_defaults_off():
    assert settings.dev_auth_bypass is False


def test_dev_bypass_reads_kiyra_prefixed_env(monkeypatch):
    """The documented env var must actually reach Settings."""
    from config import Settings
    monkeypatch.setenv("KIYRA_DEV_AUTH_BYPASS", "true")
    monkeypatch.delenv("DEV_AUTH_BYPASS", raising=False)
    assert Settings(_env_file=None).dev_auth_bypass is True


def test_dev_bypass_allows_anonymous_http(raw_client, dev_bypass):
    assert raw_client.get("/api/profile").status_code == 200
    assert raw_client.get("/api/transcription/health").status_code == 200


def test_dev_bypass_me_and_logout_without_a_token(raw_client, dev_bypass):
    me = raw_client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["email"] == "dev@localhost"
    assert raw_client.post("/api/auth/logout").status_code == 200


def test_dev_bypass_allows_anonymous_websocket(raw_client, dev_bypass):
    with raw_client.websocket_connect("/api/interviews/999999/ws") as websocket:
        with pytest.raises(WebSocketDisconnect) as exc:
            websocket.receive_text()
    assert exc.value.code == 4004  # auth passed; session simply does not exist


def test_dev_bypass_is_reported_by_status(raw_client, dev_bypass):
    assert raw_client.get("/api/auth/status").json()["dev_bypass"] is True


def test_dev_bypass_does_not_enable_email_signin(
        raw_client, dev_bypass, signed_in):
    # The bypass skips sign-in; it never becomes a code-delivery fallback.
    resp = raw_client.post(
        "/api/auth/login", json={"email": "enforce@example.com"})
    assert resp.status_code == 503
