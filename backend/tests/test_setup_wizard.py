"""Pre-flight setup wizard (sign in → job description → instructions).

The wizard is pure Tkinter and runs before the overlay. These tests drive it
directly on the shared session root (never creating a second Tk root) and keep
every window off-screen. Network calls are monkeypatched — nothing here talks
to a real backend or sends real email.
"""
import io
import json
import sys
import time
import urllib.error
from pathlib import Path

import pytest

pytest.importorskip("tkinter")

DESKTOP = Path(__file__).resolve().parents[2] / "desktop"
if str(DESKTOP) not in sys.path:
    sys.path.insert(0, str(DESKTOP))

import setup_wizard as sw  # noqa: E402


EMAIL = "ada@example.com"
NAME = "Ada Lovelace"
PHONE = "+91 98765 43210"
ACCOUNT = {"id": 1, "full_name": NAME, "phone": "+919876543210",
           "email": EMAIL, "is_verified": True}


def _pump(root, rounds=3):
    for _ in range(rounds):
        try:
            root.update_idletasks()
            root.update()
        except Exception:
            pass
        time.sleep(0.01)


@pytest.fixture
def wizard(tk_root, monkeypatch, tmp_path):
    """A wizard with no background network: job fetch disabled and an empty,
    throwaway session store (so a developer's real token is never touched)."""
    monkeypatch.setenv("KIYRA_AUTH_STORE", str(tmp_path / "auth.json"))
    monkeypatch.setattr(sw.SetupWizard, "_load_jobs_async", lambda self: None)
    w = sw.SetupWizard(tk_root, "http://127.0.0.1:9", offscreen=True)
    _pump(tk_root)
    yield w
    try:
        w._close()
    except Exception:
        pass


def _set(entry, value):
    entry.delete(0, "end")
    entry.insert(0, value)


def _challenge(**overrides):
    payload = {
        "ok": True,
        "purpose": "login",
        "email": EMAIL,
        "delivery": "console",
        "expires_in": 600,
        "resend_in": 30,
        "dev_code": "123456",
        "message": "console fallback",
    }
    payload.update(overrides)
    return payload


def _http_error(status=400, detail="Incorrect code. 4 attempts left."):
    body = io.BytesIO(json.dumps({"detail": detail}).encode())
    return urllib.error.HTTPError(
        "http://127.0.0.1:9/api/auth/verify", status, "error", {}, body)


def _signed_in(wizard):
    wizard._set_signed_in(dict(ACCOUNT), "test-token")


# ── mode toggle ─────────────────────────────────────────────────────────────

def test_signin_mode_hides_name_and_phone(wizard):
    assert wizard.auth_mode.get() == "signin"
    assert wizard.ent_full_name._row.winfo_manager() == ""
    assert wizard.ent_phone._row.winfo_manager() == ""
    assert wizard.ent_email._row.winfo_manager() == "pack"


def test_signup_mode_shows_name_and_phone(wizard):
    wizard.auth_mode.set("signup")
    wizard._on_auth_mode()
    _pump(wizard.root)
    assert wizard.ent_full_name._row.winfo_manager() == "pack"
    assert wizard.ent_phone._row.winfo_manager() == "pack"
    assert wizard.ent_email._row.winfo_manager() == "pack"
    assert "Create" in wizard.login_title.cget("text")


# ── requesting a code ───────────────────────────────────────────────────────

def test_request_code_requires_email(wizard):
    _set(wizard.ent_email, "")
    assert wizard._request_code(background=False) is False
    assert "email" in wizard.login_error.cget("text").lower()


def test_signup_requires_name_and_phone(wizard):
    wizard.auth_mode.set("signup")
    wizard._on_auth_mode()
    _set(wizard.ent_full_name, "")
    _set(wizard.ent_phone, PHONE)
    _set(wizard.ent_email, EMAIL)
    assert wizard._request_code(background=False) is False
    assert "full name" in wizard.login_error.cget("text").lower()

    _set(wizard.ent_full_name, NAME)
    _set(wizard.ent_phone, "")
    assert wizard._request_code(background=False) is False
    assert "phone" in wizard.login_error.cget("text").lower()


def test_login_posts_to_login_and_shows_otp_view(wizard, monkeypatch):
    captured = {}

    def fake_post(api_url, path, payload, timeout=10.0, token=None):
        captured["path"] = path
        captured["payload"] = payload
        return _challenge()

    monkeypatch.setattr(sw, "_api_post", fake_post)
    _set(wizard.ent_email, EMAIL)

    assert wizard._request_code(background=False) is True
    assert captured["path"] == "/api/auth/login"
    assert captured["payload"] == {"email": EMAIL}
    assert wizard._pending_email == EMAIL
    assert wizard._pending_purpose == "login"
    assert wizard._dev_code == "123456"
    assert wizard.login_otp.winfo_manager() == "pack"
    assert wizard.login_credentials.winfo_manager() == ""
    assert "123456" in wizard.otp_hint.cget("text")


def test_signup_posts_to_register_with_all_fields(wizard, monkeypatch):
    captured = {}

    def fake_post(api_url, path, payload, timeout=10.0, token=None):
        captured["path"] = path
        captured["payload"] = payload
        return _challenge(purpose="register")

    monkeypatch.setattr(sw, "_api_post", fake_post)
    wizard.auth_mode.set("signup")
    wizard._on_auth_mode()
    _set(wizard.ent_full_name, NAME)
    _set(wizard.ent_phone, PHONE)
    _set(wizard.ent_email, EMAIL)

    assert wizard._request_code(background=False) is True
    assert captured["path"] == "/api/auth/register"
    assert captured["payload"] == {
        "full_name": NAME, "phone": PHONE, "email": EMAIL}


def test_network_failure_is_reported(wizard, monkeypatch):
    def fake_post(*args, **kwargs):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(sw, "_api_post", fake_post)
    _set(wizard.ent_email, EMAIL)

    assert wizard._request_code(background=False) is False
    assert "backend" in wizard.login_error.cget("text").lower()


def test_email_delivery_hides_the_code(wizard, monkeypatch):
    def fake_post(*args, **kwargs):
        return _challenge(delivery="email", dev_code=None)

    monkeypatch.setattr(sw, "_api_post", fake_post)
    _set(wizard.ent_email, EMAIL)
    wizard._request_code(background=False)

    assert wizard._dev_code is None
    assert "spam" in wizard.otp_hint.cget("text").lower()


# ── verifying a code ────────────────────────────────────────────────────────

def test_verify_requires_a_code(wizard):
    wizard._pending_email = EMAIL
    _set(wizard.ent_otp, "")
    assert wizard._verify_code(background=False) is False
    assert "enter the code" in wizard.otp_error.cget("text").lower()


def test_verify_creates_session_and_advances(wizard, monkeypatch):
    def fake_post(api_url, path, payload, timeout=10.0, token=None):
        assert path == "/api/auth/verify"
        assert payload == {"email": EMAIL, "code": "123456"}
        return {"token": "session-token", "token_type": "bearer",
                "expires_at": "2026-12-12T00:00:00", "account": dict(ACCOUNT)}

    monkeypatch.setattr(sw, "_api_post", fake_post)
    wizard._pending_email = EMAIL
    wizard._show_otp_view()
    _set(wizard.ent_otp, "123456")

    assert wizard._verify_code(background=False) is True
    assert wizard._page == 1
    assert wizard.account["email"] == EMAIL
    assert wizard.auth_token == "session-token"
    assert wizard.username == NAME
    assert str(wizard.btn_next.cget("state")) == "normal"

    stored = sw.load_auth_session()
    assert stored is not None
    assert stored["token"] == "session-token"
    assert stored["account"]["email"] == EMAIL


def test_wrong_code_shows_backend_message(wizard, monkeypatch):
    def fake_post(*args, **kwargs):
        raise _http_error()

    monkeypatch.setattr(sw, "_api_post", fake_post)
    wizard._pending_email = EMAIL
    wizard._show_otp_view()
    _set(wizard.ent_otp, "000000")

    assert wizard._verify_code(background=False) is False
    assert wizard._page == 0
    assert "incorrect code" in wizard.otp_error.cget("text").lower()


def test_missing_token_is_treated_as_failure(wizard, monkeypatch):
    def fake_post(*args, **kwargs):
        return {"account": dict(ACCOUNT)}

    monkeypatch.setattr(sw, "_api_post", fake_post)
    wizard._pending_email = EMAIL
    wizard._show_otp_view()
    _set(wizard.ent_otp, "123456")

    assert wizard._verify_code(background=False) is True
    assert wizard.account is None
    assert "no session" in wizard.otp_error.cget("text").lower()


# ── resend + navigation between auth views ──────────────────────────────────

def test_resend_uses_the_pending_email(wizard, monkeypatch):
    captured = {}

    def fake_post(api_url, path, payload, timeout=10.0, token=None):
        captured["path"] = path
        captured["payload"] = payload
        return _challenge()

    monkeypatch.setattr(sw, "_api_post", fake_post)
    wizard._pending_email = EMAIL
    wizard._pending_purpose = "register"

    assert wizard._resend_code(background=False) is True
    assert captured["path"] == "/api/auth/resend"
    assert captured["payload"] == {"email": EMAIL, "purpose": "register"}


def test_resend_countdown_disables_the_button(wizard):
    wizard._start_resend_countdown(30)
    assert str(wizard.btn_resend.cget("state")) == "disabled"
    assert "Resend in" in wizard.btn_resend.cget("text")
    wizard._cancel_resend_countdown()
    assert str(wizard.btn_resend.cget("state")) == "normal"
    assert wizard.btn_resend.cget("text") == "Resend code"


def test_use_different_email_returns_to_credentials(wizard):
    wizard._pending_email = EMAIL
    wizard._dev_code = "123456"
    wizard._show_otp_view()
    wizard._use_different_email()
    _pump(wizard.root)
    assert wizard._pending_email == ""
    assert wizard.login_credentials.winfo_manager() == "pack"
    assert wizard.login_otp.winfo_manager() == ""


# ── stored session ──────────────────────────────────────────────────────────

def test_stored_session_is_restored(tk_root, monkeypatch, tmp_path):
    monkeypatch.setenv("KIYRA_AUTH_STORE", str(tmp_path / "auth.json"))
    monkeypatch.setattr(sw.SetupWizard, "_load_jobs_async", lambda self: None)
    monkeypatch.setattr(sw, "_api_get_auth",
                        lambda url, path, token: dict(ACCOUNT))
    sw.save_auth_session("stored-token", dict(ACCOUNT))

    w = sw.SetupWizard(tk_root, "http://127.0.0.1:9", offscreen=True)
    try:
        _pump(tk_root, rounds=40)
        assert w.account is not None
        assert w.account["email"] == EMAIL
        assert w.auth_token == "stored-token"
        assert w.login_signed.winfo_manager() == "grid"
        assert str(w.btn_next.cget("state")) == "normal"
    finally:
        w._close()


def test_stale_stored_session_is_cleared(tk_root, monkeypatch, tmp_path):
    store = tmp_path / "auth.json"
    monkeypatch.setenv("KIYRA_AUTH_STORE", str(store))
    monkeypatch.setattr(sw.SetupWizard, "_load_jobs_async", lambda self: None)

    def reject(url, path, token):
        raise _http_error(status=401, detail="Session expired.")

    monkeypatch.setattr(sw, "_api_get_auth", reject)
    sw.save_auth_session("stale-token", dict(ACCOUNT))

    w = sw.SetupWizard(tk_root, "http://127.0.0.1:9", offscreen=True)
    try:
        _pump(tk_root, rounds=40)
        assert w.account is None
        assert w.auth_token is None
        assert not store.exists()
    finally:
        w._close()


def test_sign_out_clears_session(wizard, monkeypatch):
    calls = []

    def fake_post(api_url, path, payload, timeout=10.0, token=None):
        calls.append((path, token))
        return {"ok": True}

    monkeypatch.setattr(sw, "_api_post", fake_post)
    _signed_in(wizard)
    assert sw.load_auth_session() is not None

    wizard._sign_out()
    _pump(wizard.root)

    assert wizard.account is None
    assert wizard.auth_token is None
    assert wizard.username == "local"
    assert sw.load_auth_session() is None
    assert calls == [("/api/auth/logout", "test-token")]
    assert wizard.login_form.winfo_manager() == "grid"
    assert str(wizard.btn_next.cget("state")) == "disabled"


# ── navigation / finish ─────────────────────────────────────────────────────

def test_continue_is_blocked_until_signed_in(wizard):
    wizard._next()
    assert wizard._page == 0
    assert "sign in" in wizard.login_error.cget("text").lower()


def test_three_step_navigation_and_finish(wizard):
    _signed_in(wizard)

    assert wizard._page == 0
    wizard._next()                      # sign in -> job description
    assert wizard._page == 1

    wizard.jd_mode.set("none")
    wizard._next()                      # job description -> instructions
    assert wizard._page == 2
    assert wizard.btn_next.cget("text") == "Start Kiyra"

    wizard._finish()
    assert wizard.result["username"] == NAME
    assert wizard.result["job_description_id"] is None
    assert wizard.result["account"]["email"] == EMAIL
    assert wizard.result["auth_token"] == "test-token"


def test_back_button_steps_back(wizard):
    _signed_in(wizard)
    wizard._next()
    assert wizard._page == 1
    wizard._back()
    assert wizard._page == 0


def test_cancel_returns_none(wizard):
    wizard._cancel()
    assert wizard.result is None


# ── job description page ────────────────────────────────────────────────────

def test_skip_sets_no_job_description(wizard):
    wizard.jd_mode.set("none")
    assert wizard._save_jd() is True
    assert wizard.job_description_id is None


def test_existing_requires_a_selection(wizard):
    wizard.jd_mode.set("existing")
    wizard._jobs = []
    wizard.jd_list.delete(0, "end")
    assert wizard._save_jd() is False
    assert "Select" in wizard.status.cget("text")


def test_existing_selection_activates_it(wizard, monkeypatch):
    calls = []

    def fake_post(api_url, path, payload, timeout=10.0, token=None):
        calls.append(path)
        return {}

    monkeypatch.setattr(sw, "_api_post", fake_post)
    wizard.jd_mode.set("existing")
    wizard._jobs = [{"id": 7, "title": "Data Eng", "company": "X",
                     "is_active": False}]
    wizard.jd_list.delete(0, "end")
    wizard.jd_list.insert("end", "Data Eng — X")
    wizard.jd_list.selection_set(0)

    assert wizard._save_jd() is True
    assert wizard.job_description_id == 7
    assert calls == ["/api/jobs/7/activate"]


def test_new_description_posts_and_records_id(wizard, monkeypatch):
    captured = {}

    def fake_post(api_url, path, payload, timeout=10.0, token=None):
        captured["path"] = path
        captured["payload"] = payload
        return {"id": 42}

    monkeypatch.setattr(sw, "_api_post", fake_post)
    wizard.jd_mode.set("new")
    _set(wizard.ent_jd_title, "Backend Engineer")
    _set(wizard.ent_jd_company, "Acme")
    wizard.txt_jd.delete("1.0", "end")
    wizard.txt_jd.insert("1.0", "Python, FastAPI, PostgreSQL")

    assert wizard._save_jd() is True
    assert wizard.job_description_id == 42
    assert captured["path"] == "/api/jobs"
    assert captured["payload"]["title"] == "Backend Engineer"
    assert captured["payload"]["company"] == "Acme"
    assert "FastAPI" in captured["payload"]["raw_text"]


def test_new_description_requires_title_and_text(wizard):
    wizard.jd_mode.set("new")
    _set(wizard.ent_jd_title, "")
    wizard.txt_jd.delete("1.0", "end")
    assert wizard._save_jd() is False
    assert "title" in wizard.status.cget("text").lower()

    _set(wizard.ent_jd_title, "Backend Engineer")
    assert wizard._save_jd() is False
    assert "description" in wizard.status.cget("text").lower()


def test_render_jobs_marks_the_active_entry(wizard):
    wizard._render_jobs([
        {"id": 1, "title": "Alpha", "company": "C1", "is_active": True},
        {"id": 2, "title": "Beta", "company": None, "is_active": False},
    ], None)
    assert wizard.jd_list.size() == 2
    assert "Alpha" in wizard.jd_list.get(0)
    assert "active" in wizard.jd_list.get(0)
    assert wizard.jd_list.get(1) == "Beta"


def test_render_jobs_surfaces_load_errors(wizard):
    wizard._render_jobs([], "connection refused")
    assert wizard.jd_list.size() == 0
    assert "Could not load" in wizard.jd_hint.cget("text")


# ── error helper ────────────────────────────────────────────────────────────

def test_api_error_extracts_backend_detail():
    assert "4 attempts" in sw._api_error(_http_error())


def test_api_error_handles_unreachable_backend():
    assert "backend" in sw._api_error(
        urllib.error.URLError("nope")).lower()


# ── skip / off-screen behaviour ─────────────────────────────────────────────

def test_should_skip_setup_env(monkeypatch):
    monkeypatch.delenv("KIYRA_SKIP_SETUP", raising=False)
    monkeypatch.delenv("KIYRA_OVERLAY_HIDDEN", raising=False)
    assert sw.should_skip_setup() is False

    monkeypatch.setenv("KIYRA_SKIP_SETUP", "1")
    assert sw.should_skip_setup() is True


def test_hidden_mode_skips_setup(monkeypatch):
    monkeypatch.delenv("KIYRA_SKIP_SETUP", raising=False)
    monkeypatch.setenv("KIYRA_OVERLAY_HIDDEN", "1")
    assert sw.should_skip_setup() is True


def test_run_setup_wizard_skip_returns_defaults(tk_root, monkeypatch):
    monkeypatch.setenv("KIYRA_SKIP_SETUP", "1")
    assert sw.run_setup_wizard(tk_root, "http://127.0.0.1:9") == {
        "username": "local", "job_description_id": None,
        "account": None, "auth_token": None}


def test_run_setup_wizard_explicit_skip(tk_root):
    assert sw.run_setup_wizard(tk_root, "http://127.0.0.1:9", skip=True) == {
        "username": "local", "job_description_id": None,
        "account": None, "auth_token": None}


def test_offscreen_position(wizard):
    assert wizard.offscreen is True
    assert "+20000+20000" in wizard.win.geometry()
