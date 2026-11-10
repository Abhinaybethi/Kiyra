"""Pre-flight setup wizard (sign in → job description → instructions).

The wizard is pure Tkinter and runs before the overlay. These tests drive it
directly on the shared session root (never creating a second Tk root) and keep
every window off-screen.
"""
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("tkinter")

DESKTOP = Path(__file__).resolve().parents[2] / "desktop"
if str(DESKTOP) not in sys.path:
    sys.path.insert(0, str(DESKTOP))

import setup_wizard as sw  # noqa: E402


def _pump(root, rounds=3):
    for _ in range(rounds):
        try:
            root.update_idletasks()
            root.update()
        except Exception:
            pass
        time.sleep(0.01)


@pytest.fixture
def wizard(tk_root, monkeypatch):
    """A wizard with the background job fetch disabled (no network in tests)."""
    monkeypatch.setattr(sw.SetupWizard, "_load_jobs_async", lambda self: None)
    w = sw.SetupWizard(tk_root, "http://127.0.0.1:9", offscreen=True)
    _pump(tk_root)
    yield w
    try:
        w.win.destroy()
    except Exception:
        pass


def _set(entry, value):
    entry.delete(0, "end")
    entry.insert(0, value)


# ── login gate ──────────────────────────────────────────────────────────────

def test_default_access_code_is_accepted(wizard):
    _set(wizard.ent_user, "Ada")
    _set(wizard.ent_code, "kiyra")
    assert wizard._validate_login() is True
    assert wizard.username == "Ada"


def test_wrong_access_code_is_rejected(wizard):
    _set(wizard.ent_user, "Ada")
    _set(wizard.ent_code, "nope")
    assert wizard._validate_login() is False
    assert "Incorrect" in wizard.login_error.cget("text")


def test_blank_name_is_rejected(wizard):
    _set(wizard.ent_user, "")
    _set(wizard.ent_code, "kiyra")
    assert wizard._validate_login() is False
    assert "name" in wizard.login_error.cget("text").lower()


def test_env_access_code_override(wizard, monkeypatch):
    monkeypatch.setenv("KIYRA_ACCESS_CODE", "s3cret")
    _set(wizard.ent_user, "Ada")
    _set(wizard.ent_code, "s3cret")
    assert wizard._validate_login() is True


def test_empty_env_access_code_disables_check(wizard, monkeypatch):
    monkeypatch.setenv("KIYRA_ACCESS_CODE", "")
    _set(wizard.ent_user, "Ada")
    _set(wizard.ent_code, "anything")
    assert wizard._validate_login() is True


# ── navigation / finish ─────────────────────────────────────────────────────

def test_three_step_navigation_and_finish(wizard):
    _set(wizard.ent_user, "Ada")
    _set(wizard.ent_code, "kiyra")

    assert wizard._page == 0
    wizard._next()                      # sign in -> job description
    assert wizard._page == 1

    wizard.jd_mode.set("none")
    wizard._next()                      # job description -> instructions
    assert wizard._page == 2
    assert wizard.btn_next.cget("text") == "Start Kiyra"

    wizard._finish()
    assert wizard.result == {"username": "Ada", "job_description_id": None}


def test_back_button_steps_back(wizard):
    _set(wizard.ent_user, "Ada")
    _set(wizard.ent_code, "kiyra")
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

    def fake_post(api_url, path, payload, timeout=10.0):
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

    def fake_post(api_url, path, payload, timeout=10.0):
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
        "username": "local", "job_description_id": None}


def test_run_setup_wizard_explicit_skip(tk_root):
    assert sw.run_setup_wizard(tk_root, "http://127.0.0.1:9", skip=True) == {
        "username": "local", "job_description_id": None}


def test_offscreen_position(wizard):
    assert wizard.offscreen is True
    assert "+20000+20000" in wizard.win.geometry()
