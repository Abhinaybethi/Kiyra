"""Phase 5.5 tests: desktop overlay chrome.

Covers the additive chrome added on top of the fixed 560x640 overlay:
elapsed session timer, toolbar system-audio selector, expandable features
panel (zoom, move, transparency, mini-collapse), Shift+Alt shortcut targets,
input clearing, and history-button state sync.

These are Tk-logic tests: no audio, no backend, no WebSocket. The module-scoped
``tk_root`` fixture keeps the one-Tk-root-per-session invariant (AGENTS.md).
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

DESKTOP_DIR = Path(__file__).resolve().parents[2] / "desktop"
import sys  # noqa: E402

sys.path.insert(0, str(DESKTOP_DIR))

import tkinter as tk  # noqa: E402

import stealth_overlay as so  # noqa: E402


def _pump(root, rounds: int = 6):
    for _ in range(rounds):
        root.update()
        time.sleep(0.01)


class _App(so.StealthOverlayApp):
    """Real overlay window, device discovery stubbed out."""

    def _populate_devices(self):
        pass


@pytest.fixture(scope="module")
def app(tk_root):
    root = tk_root
    overlay = _App(root, api_url="http://127.0.0.1:9", session_id=0)
    _pump(root, rounds=10)
    yield overlay
    for child in list(root.winfo_children()):
        try:
            child.destroy()
        except Exception:
            pass


def test_timer_ticks_and_paints_over_elapsed(app):
    app._start_timer()
    try:
        assert app._timer_running
        # Simulate 65 elapsed seconds, then paint.
        app._timer_started_at = time.monotonic() - 65
        app._tick_timer()
        assert "01:05" in app.lbl_timer.cget("text")
    finally:
        app._stop_timer()
    assert not app._timer_running


def test_timer_is_idempotent_and_stop_cancels(app):
    app._start_timer()
    app._start_timer()
    assert app._timer_running
    app._stop_timer()
    assert not app._timer_running
    # A late tick after stop no longer repaints or reschedules.
    app._tick_timer()
    assert app._timer_after_id is None


def test_sys_audio_selector_flags_unavailable_when_no_loopback(app):
    app._sync_sys_audio_selector()
    assert app.cmb_sys_audio["values"][0] == "<unavailable>"
    assert "disabled" in app.cmb_sys_audio.state()


def test_sys_audio_selection_maps_to_loop_choice(app):
    class _Fake(app.__class__):
        pass

    fake = _Fake.__new__(_Fake)
    fake._loop_choice = None
    fake.loopback_device = None
    app.cmb_sys_audio["values"] = ["WASAPI Loopback: Spk"]
    app.cmb_sys_audio.set("WASAPI Loopback: Spk")
    app._last_loop = {"name": "Spk"}
    app._on_sys_audio_selected()
    assert app._loop_choice == "WASAPI Loopback: Spk"
    assert app.loopback_device == {"name": "Spk"}
    app.cmb_sys_audio.set("<none>")
    app._on_sys_audio_selected()
    assert app._loop_choice is None
    assert app.loopback_device is None


def test_features_panel_toggles_visibility(app):
    # The practice panel shows the Features table by default.
    _pump(app.root)
    assert app._features_open
    assert app._features_panel.winfo_ismapped()
    app.toggle_features()
    _pump(app.root)
    assert not app._features_open
    assert not app._features_panel.winfo_ismapped()
    app.toggle_features()
    _pump(app.root)
    assert app._features_open
    assert app.btn_features.cget("text") == "▥ Close Panel"
    assert app._features_panel.winfo_ismapped()


def test_zoom_preset_resizes_between_normal_and_large(app):
    s = app._ui_scale
    w = int(so.DESIGN_WIDTH * s)
    h_normal = int(so.DESIGN_HEIGHT * s)
    app.root.geometry(f"{w}x{h_normal}")
    _pump(app.root)
    before = int(app.root.winfo_height())
    assert before == h_normal
    app.toggle_zoom()
    _pump(app.root)
    enlarged = int(app.root.winfo_height())
    # Screen work-area may clamp the 880-scaled preset on small displays, so
    # assert strictly-larger rather than an exact target.
    assert enlarged > before
    app.toggle_zoom()
    _pump(app.root)
    assert int(app.root.winfo_height()) == h_normal


def test_window_stays_non_resizable_after_zoom(app):
    assert app.root.resizable() == (False, False)
    app.toggle_zoom()
    app.toggle_zoom()
    assert app.root.resizable() == (False, False)


def test_move_window_nudges_position(app):
    app.root.geometry("+20000+20000")
    _pump(app.root)
    x0 = app.root.winfo_x()
    app._move_window(24, 0)
    _pump(app.root)
    assert app.root.winfo_x() == x0 + 24


def test_alpha_adjusts_and_clamps(app):
    app._reset_alpha()
    assert abs(float(app.root.attributes("-alpha") or 1.0) - 1.0) < 0.01
    app._adjust_alpha(-0.1)
    assert abs(float(app.root.attributes("-alpha") or 1.0) - 0.9) < 0.01
    app._adjust_alpha(-5)
    assert abs(float(app.root.attributes("-alpha") or 1.0) - 0.35) < 0.01
    app._adjust_alpha(5)
    assert abs(float(app.root.attributes("-alpha") or 1.0) - 1.0) < 0.01


def test_alpha_scale_syncs_label(app):
    app._on_alpha("0.55")
    assert app.lbl_alpha.cget("text") == "55%"
    app._reset_alpha()
    assert app.lbl_alpha.cget("text") == "100%"


def test_clear_inputs_resets_entry_and_transcript(app):
    app._manual_mode = True
    app._switch_tab(True)
    app.entry_manual.delete(0, tk.END)
    app.entry_manual.insert(0, "What is a pivot table?")
    app._live_transcript = "some partial speech"
    app._live_partial = "still listening"
    app.clear_inputs()
    assert app.entry_manual.get() == so.ASK_PLACEHOLDER
    assert app._live_transcript == ""
    assert app._live_partial == ""


def test_focus_ask_switches_to_manual_tab(app):
    app._manual_mode = False
    app._set_tab_active(False)
    app.focus_ask()
    assert app._manual_mode is True


def test_scroll_answer_is_safe_without_content(app):
    app._scroll_answer(-6)
    app._scroll_answer(6)
    # No assertion: this path must never raise even on an empty canvas.


def test_history_buttons_boundary_state(app):
    app._question_history = []
    app._history_index = -1
    app._history_prev()
    app._history_next()
    assert app._history_index == -1
    app._sync_hist_controls(False, False)
    assert app.btn_mhist_up.cget("state") == tk.DISABLED
    assert app.btn_mhist_down.cget("state") == tk.DISABLED
    app._question_history = [{"question": "Q1"}, {"question": "Q2"}]
    app._history_index = 0
    app._sync_hist_controls(False, True)
    assert app.btn_mhist_up.cget("state") == tk.DISABLED
    assert app.btn_mhist_down.cget("state") == tk.NORMAL
    assert app.btn_features_hist_next.cget("state") == tk.NORMAL


def test_mini_collapse_and_restore(app):
    app.toggle_mini()
    _pump(app.root)
    assert app._mini_win is not None and app._mini_win.winfo_exists()
    assert not app.root.winfo_viewable()
    app._restore_mini()
    _pump(app.root)
    assert app._mini_win is None
    assert app.root.winfo_viewable()


def test_set_panel_size_presets(app):
    s = app._ui_scale
    h_normal = int(so.DESIGN_HEIGHT * s)
    app.set_panel_size("normal")
    _pump(app.root)
    assert app._size_preset == "normal"
    assert app._large_panel is False
    assert int(app.root.winfo_height()) == h_normal
    app.set_panel_size("large")
    _pump(app.root)
    assert app._large_panel is True
    assert int(app.root.winfo_height()) > h_normal
    assert app.root.resizable() == (False, False)
    app.set_panel_size("normal")
    _pump(app.root)
    assert int(app.root.winfo_height()) == h_normal
    # Unknown preset falls back to normal instead of raising.
    app.set_panel_size("nonsense")
    _pump(app.root)
    assert app._size_preset == "normal"


class _Ev:
    def __init__(self, x, y):
        self.x_root = x
        self.y_root = y


def test_mini_click_restores_overlay(app):
    if app._mini_win is not None:
        app._restore_mini()
    app.toggle_mini()
    _pump(app.root)
    assert app._mini_win is not None and app._mini_win.winfo_exists()
    # A press/release with no motion is a click -> restore.
    app._mini_drag_start(_Ev(100, 100))
    app._mini_drag_end(_Ev(100, 100))
    _pump(app.root)
    assert app._mini_win is None
    assert app.root.winfo_viewable()


def test_mini_drag_moves_and_keeps_bubble(app):
    if app._mini_win is not None:
        app._restore_mini()
    app.toggle_mini()
    _pump(app.root)
    app._mini_drag_start(_Ev(100, 100))
    app._mini_drag_move(_Ev(180, 140))
    assert app._mini_drag_moved is True
    app._mini_drag_end(_Ev(180, 140))
    _pump(app.root)
    # A real drag repositions the bubble and does NOT restore the overlay.
    assert app._mini_win is not None and app._mini_win.winfo_exists()
    app._restore_mini()
    _pump(app.root)
    assert app._mini_win is None


def test_settings_dialog_builds_with_window_section(app):
    app.open_settings()
    _pump(app.root)
    dialogs = [w for w in app.root.winfo_children() if isinstance(w, tk.Toplevel)]
    assert dialogs, "settings dialog was not created"
    try:
        assert set(app._size_btns) == {"compact", "normal", "large"}
        # The active preset button should be highlighted (accent background).
        active = app._size_btns[app._size_preset]
        assert active.cget("bg") == app.accent_color
    finally:
        for w in dialogs:
            try:
                w.grab_release()
            except Exception:
                pass
            try:
                w.destroy()
            except Exception:
                pass
        _pump(app.root)
