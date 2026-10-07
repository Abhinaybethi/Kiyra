"""Overlay side of Interview Listening Mode (Phase 5.4).

The backend owns the question boundary; the overlay must (a) never submit on a
VAD segment, (b) offer exactly one SEND affordance, (c) show the live
transcript, and (d) surface memory / previous questions / topic.
"""
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("tkinter")

DESKTOP = Path(__file__).resolve().parents[2] / "desktop"
if str(DESKTOP) not in sys.path:
    sys.path.insert(0, str(DESKTOP))

import stealth_overlay as so  # noqa: E402

IS_WIN = sys.platform == "win32"
needs_win = pytest.mark.skipif(not IS_WIN, reason="Windows window-shell only")


def _pump(root, rounds=2):
    for _ in range(rounds):
        try:
            root.update_idletasks()
            root.update()
        except Exception:
            pass
        time.sleep(0.01)


class _App(so.StealthOverlayApp):
    def _populate_devices(self):
        pass

    def _send_ws_message(self, msg):
        """Capture outgoing commands instead of touching a socket."""
        self.sent_commands.append(msg)
        return True


@pytest.fixture(scope="module")
def app(tk_root):
    root = tk_root
    a = _App(root, api_url="http://127.0.0.1:9", session_id=0)
    a.sent_commands = []
    _pump(root)
    yield a
    for child in list(root.winfo_children()):
        try:
            child.destroy()
        except Exception:
            pass


@pytest.fixture(autouse=True)
def _reset(app):
    """Clear per-test listening state; the Tk panel itself is reused."""
    app.sent_commands = []
    app._listening_mode = False
    app._listening_active = False
    app._can_send = False
    app._live_transcript = ""
    app._live_partial = ""
    app._memory_points = []
    app._previous_questions = []
    app._question_history = []
    app._history_index = -1
    app._active_topic = ""
    app._turn_submitted = False
    app._assistant_state = "ready"
    app._answer_replace_pending = False
    app._stream_buffer = ""
    app._last_code = ""
    app._want_listening_mode = True
    app._want_auto_start = True
    yield
    app.sent_commands = []


def _activate(a):
    """Simulate the backend confirming Interview Listening Mode."""
    a._ui_mode_changed({"mode": "interview_listening"})
    _pump(a.root)


# ── command wiring ───────────────────────────────────────────────────────────
def test_no_mode_request_when_listening_disabled(app):
    app._want_listening_mode = False
    try:
        app._negotiate_listening_mode()
        assert app.sent_commands == [], "legacy auto-detect must not opt in"
    finally:
        app._want_listening_mode = True


def test_session_connected_requests_listening_mode(app):
    app._negotiate_listening_mode()
    assert app.sent_commands == [
        {"type": "mode.set", "payload": {"mode": "interview_listening"}}]


def test_auto_start_is_opt_out(app):
    app._want_auto_start = False
    _activate(app)
    assert not any(c["type"] == "listening.start" for c in app.sent_commands)


def test_mode_change_builds_panel_and_starts(app):
    _activate(app)
    assert any(c["type"] == "listening.start" for c in app.sent_commands)
    assert app._listening_panel is not None


def test_unknown_mode_is_ignored(app):
    app._ui_mode_changed({"mode": "legacy"})
    assert app._listening_mode is False


# ── a VAD segment must never generate ────────────────────────────────────────
def test_transcript_segment_does_not_submit(app):
    _activate(app)
    app.sent_commands.clear()
    app._ui_listening_started({"listening": True})
    app._ui_transcript_segment({
        "text": "Can you explain my project",
        "current": "Can you explain my project",
        "can_send": True,
        "duplicate": False,
    })
    _pump(app.root)
    assert not any(c["type"] in ("turn.send", "generate_answer")
                   for c in app.sent_commands), \
        "a VAD segment must never generate"


def test_segments_accumulate_into_one_transcript(app):
    _activate(app)
    app._ui_listening_started({"listening": True})
    current = ""
    for text in ("Can you explain my project",
                 "and tell me the architecture",
                 "and why you used FastAPI"):
        current = (current + " " + text).strip()
        app._ui_transcript_segment({"text": text, "current": current,
                                    "can_send": True})
    _pump(app.root)
    assert app._live_transcript.endswith("why you used FastAPI")
    shown = app.txt_live.get("1.0", "end").strip()
    assert "FastAPI" in shown and "Can you explain my project" in shown


def test_duplicate_segment_does_not_grow_transcript(app):
    _activate(app)
    app._ui_listening_started({"listening": True})
    app._ui_transcript_segment({
        "text": "what is rag", "current": "what is rag",
        "can_send": True, "duplicate": True})
    _pump(app.root)
    assert app.txt_live.get("1.0", "end").strip() == "what is rag"


# ── SEND ─────────────────────────────────────────────────────────────────────
def test_send_is_disabled_until_can_send(app):
    _activate(app)
    app._ui_listening_started({"listening": True})
    _pump(app.root)
    assert str(app.btn_send.cget("state")) == "disabled"
    app._ui_transcript_segment({"text": "hi", "current": "hi",
                                "can_send": False})
    _pump(app.root)
    assert str(app.btn_send.cget("state")) == "disabled"


def test_send_sends_exactly_one_turn_command(app):
    _activate(app)
    app._ui_listening_started({"listening": True})
    app._ui_transcript_segment({"text": "Explain JWT expiry",
                                "current": "Explain JWT expiry",
                                "can_send": True})
    _pump(app.root)
    assert str(app.btn_send.cget("state")) == "normal"
    app.sent_commands.clear()
    app._send_turn()
    assert app.sent_commands == [{"type": "turn.send", "payload": {}}]
    # It disables itself immediately, so a second click cannot double-submit.
    _pump(app.root)
    assert str(app.btn_send.cget("state")) == "disabled"
    app._send_turn()
    assert len([c for c in app.sent_commands if c["type"] == "turn.send"]) == 1


def test_send_is_ignored_while_generating(app):
    _activate(app)
    app._assistant_state = "generating"
    app._ui_transcript_segment({"text": "Explain JWT", "current": "Explain JWT",
                                "can_send": True})
    app.sent_commands.clear()
    app._send_turn()
    assert app.sent_commands == []


def test_toggle_button_starts_then_stops(app):
    _activate(app)
    app._ui_listening_started({"listening": True})
    _pump(app.root)
    app.sent_commands.clear()

    app._toggle_listening()
    assert app.sent_commands == [{"type": "listening.stop", "payload": {}}]
    app._listening_active = False
    app._toggle_listening()
    assert app.sent_commands[-1] == {"type": "listening.start", "payload": {}}


def test_toggle_is_noop_without_listening_mode(app):
    app._listening_mode = False
    app.sent_commands.clear()
    app._toggle_listening()
    assert app.sent_commands == []


def test_stop_preserves_transcript_and_keeps_send_available(app):
    _activate(app)
    app._ui_listening_started({"listening": True})
    app._ui_transcript_segment({"text": "Explain my RAG pipeline",
                                "current": "Explain my RAG pipeline",
                                "can_send": True})
    app._ui_listening_stopped({
        "listening": False,
        "current": "Explain my RAG pipeline",
        "can_send": True,
        "generated": False,
    })
    _pump(app.root)
    assert app._live_transcript == "Explain my RAG pipeline"
    assert str(app.btn_send.cget("state")) == "normal"


# ── memory / previous questions / topic ──────────────────────────────────────
def test_memory_updated_renders_points_and_topic(app):
    _activate(app)
    app._ui_memory_updated({
        "memory_points": ["Uses FastAPI", "Postgres for persistence"],
        "previous": [{"question": "Explain JWT", "current": True}],
        "topic": "authentication",
    })
    _pump(app.root)
    text = app.lbl_memory.cget("text")
    assert "FastAPI" in text and "Postgres" in text
    assert app.lbl_topic.cget("text") == "authentication"
    assert "Explain JWT" in app.lbl_prev.cget("text")


def test_memory_panel_is_bounded(app):
    _activate(app)
    app._ui_memory_updated({"memory_points": [f"point {i}" for i in range(50)],
                            "previous": [{"question": f"q{i}"} for i in range(50)],
                            "topic": "t"})
    _pump(app.root)
    assert len(app._memory_points) <= 6
    # History is bounded (8) but wider than the old summary strip so UP/DOWN
    # navigation is useful (Phase 5.4.1 §9).
    assert len(app._previous_questions) <= 8
    assert len(app._question_history) <= 8


def test_empty_memory_renders_cleanly(app):
    _activate(app)
    app._ui_memory_updated({})
    _pump(app.root)
    assert app.lbl_memory.cget("text") == ""
    assert app.lbl_prev.cget("text") == ""


# ── partial vs final transcript ──────────────────────────────────────────────
def test_partial_is_shown_while_listening_and_replaced_by_final(app):
    _activate(app)
    app._ui_listening_started({"listening": True})
    app._ui_transcript_partial({"text": "explain my pro"})
    _pump(app.root)
    assert "explain my pro" in app.txt_live.get("1.0", "end")

    app._ui_transcript_segment({"text": "explain my project",
                                "current": "explain my project",
                                "can_send": True})
    _pump(app.root)
    body = app.txt_live.get("1.0", "end")
    assert "explain my project" in body
    assert app._live_partial == ""


def test_empty_turn_error_keeps_transcript_for_retry(app):
    _activate(app)
    app._ui_listening_started({"listening": True})
    app._ui_transcript_segment({"text": "hi", "current": "hi",
                                "can_send": False})
    app._ui_turn_error({"code": "empty_turn", "message": "too short"})
    _pump(app.root)
    assert app._live_transcript == "hi"


def test_render_live_survives_missing_panel(app):
    """Never raise if an event arrives before mode.changed."""
    app._listening_panel = None
    app._render_live()


# ── window-shell invariants must survive the new panel ───────────────────────
@needs_win
def test_panel_keeps_fixed_window_size(app):
    _activate(app)
    _pump(app.root, rounds=4)
    expected = (int(560 * app._ui_scale), int(640 * app._ui_scale))
    assert (app.root.winfo_width(), app.root.winfo_height()) == expected
    assert app.root.resizable() == (False, False)


@needs_win
def test_panel_does_not_add_a_taskbar_button(app):
    import ctypes

    _activate(app)
    _pump(app.root, rounds=4)
    hwnd = so.toplevel_hwnd(app.root.winfo_id())
    ex = ctypes.windll.user32.GetWindowLongW(hwnd, so.GWL_EXSTYLE)
    assert ex & so.WS_EX_TOOLWINDOW
    assert not ex & so.WS_EX_APPWINDOW

# ============================================================================
# Phase 5.4.1 - answer persistence and question-history navigation
# ============================================================================
def _show_answer(a, text):
    """Render an answer the way the real stream does: reset, then chunks."""
    a._ui_stream_answer("", reset=True, append=False)
    _pump(a.root)
    for word in text.split(" "):
        a._ui_stream_answer(word + " ", append=True)
    _pump(a.root)


def _begin_new_answer(a):
    """Start a new generation with no chunk yet (the SEND moment)."""
    a._ui_stream_answer("", reset=True, append=False)
    _pump(a.root)


def _answer_text(a):
    txt = getattr(a, "_stable_answer_txt", None)
    if txt is None:
        return ""
    try:
        if not txt.winfo_exists():
            return ""
        return txt.get("1.0", "end-1c")
    except Exception:
        return ""


def _history(a, count, answers=True):
    entries = []
    for i in range(count):
        entries.append({
            "turn_id": "t%d" % (i + 1),
            "sequence": i + 1,
            "question": "question number %d" % (i + 1),
            "answer": ("answer number %d" % (i + 1)) if answers else "",
            "answer_available": answers,
            "topic": "topic %d" % (i + 1),
            "ts": 1000.0 + i,
            "current": i == count - 1,
        })
    return {"previous": entries, "history_index": count - 1,
            "history_count": count, "topic": "topic %d" % count,
            "memory_points": []}


def test_previous_answer_survives_send_until_the_first_chunk(app):
    """Spec 12: the old answer stays visible through SEND and retrieval."""
    _activate(app)
    _show_answer(app, "The previous answer.")
    _pump(app.root)
    assert "previous answer" in _answer_text(app)

    # A new question is submitted: generation begins but no chunk yet.
    _begin_new_answer(app)
    _pump(app.root)
    assert "previous answer" in _answer_text(app), \
        "answer must not blank while the new one is being generated"
    assert app._answer_replace_pending is True


def test_first_chunk_replaces_the_answer_in_place(app):
    """Spec 12: replacement happens inside the same stable widget."""
    _activate(app)
    _show_answer(app, "The previous answer.")
    _pump(app.root)
    before = app._stable_answer_txt

    _begin_new_answer(app)
    _pump(app.root)
    app._ui_stream_answer("New ", append=True)
    _pump(app.root)

    text = _answer_text(app)
    assert text.strip().startswith("New"), "new answer replaced the old one"
    assert "previous answer" not in text
    assert app._stable_answer_txt is before, "widget identity is stable"
    assert app._answer_replace_pending is False


def test_answer_auto_hide_is_disabled_in_interview_mode(app):
    """Spec 12: the answer must stay readable for the next question."""
    _activate(app)
    app.answer_auto_hide_task = None
    _show_answer(app, "Answer text.")
    _pump(app.root)
    app._ui_update_answer({"answer": "Answer text."})
    _pump(app.root)

    assert app.answer_auto_hide_task is None, "no auto-hide in interview mode"
    assert "Answer text." in _answer_text(app)


def test_legacy_auto_hide_still_schedules_outside_interview_mode(app):
    """Non-interview behaviour must be untouched."""
    app._listening_mode = False
    app.answer_auto_hide_task = None
    _show_answer(app, "Legacy.")
    _pump(app.root)
    app._ui_update_answer({"answer": "Legacy."})
    _pump(app.root)
    try:
        assert app.answer_auto_hide_task is not None
    finally:
        app._cancel_answer_auto_hide()


def test_cancelled_generation_keeps_the_previous_answer(app):
    """Spec 19: a failure/cancel never leaves a blank answer panel."""
    _activate(app)
    _show_answer(app, "The previous answer.")
    _pump(app.root)
    app._ui_cancel_answer({"reason": "superseded"})
    _pump(app.root)
    assert "previous answer" in _answer_text(app)


def test_history_up_down_changes_the_selected_entry(app):
    """Spec 9: UP/DOWN move through answered questions."""
    _activate(app)
    app._ui_memory_updated(_history(app, 3))
    _pump(app.root)
    assert app._history_index == 2

    app._history_prev()
    _pump(app.root)
    assert app._history_index == 1
    app._history_prev()
    _pump(app.root)
    assert app._history_index == 0
    assert app.lbl_hist.cget("text") == "1/3"
    assert "question number 1" in app.lbl_history_view.cget("text")


def test_history_navigation_does_not_wrap_at_the_boundaries(app):
    """Spec 9: boundaries clamp, they do not wrap."""
    _activate(app)
    app._ui_memory_updated(_history(app, 3))
    _pump(app.root)

    for _ in range(5):
        app._history_next()
    _pump(app.root)
    assert app._history_index == 2, "DOWN stops at the latest entry"

    for _ in range(9):
        app._history_prev()
    _pump(app.root)
    assert app._history_index == 0, "UP stops at the first entry"


def test_history_navigation_sends_no_commands(app):
    """Spec 9: navigation is local UI only - no generation, no SEND."""
    app._want_auto_start = False
    _activate(app)
    app._ui_memory_updated(_history(app, 3))
    _pump(app.root)
    app.sent_commands.clear()

    app._history_prev()
    app._history_next()
    _pump(app.root)

    assert app.sent_commands == [], "history navigation must not talk to the backend"


def test_history_navigation_does_not_disturb_listening(app):
    """Spec 9: selecting history does not stop or alter the live turn."""
    app._want_auto_start = False
    _activate(app)
    app._ui_listening_started({"listening": True, "state": "LISTENING"})
    app._ui_transcript_segment({"text": "live question", "can_send": True})
    _pump(app.root)
    app.sent_commands.clear()

    app._ui_memory_updated(_history(app, 3))
    _pump(app.root)
    app.sent_commands.clear()
    _pump(app.root)
    app.sent_commands.clear()
    app._history_prev()
    _pump(app.root)

    assert app._listening_active is True
    assert app._can_send is True
    assert app._live_transcript == "live question"
    assert app.sent_commands == []


def test_history_latest_entry_hides_the_duplicate_viewer(app):
    """The live panel already shows the latest answer, so the viewer clears."""
    _activate(app)
    app._ui_memory_updated(_history(app, 3))
    _pump(app.root)
    assert app.lbl_history_view.cget("text") == ""

    app._history_prev()
    _pump(app.root)
    assert "question number 2" in app.lbl_history_view.cget("text")
    assert "answer number 2" in app.lbl_history_view.cget("text")


def test_history_buttons_are_disabled_at_the_boundaries(app):
    _activate(app)
    app._ui_memory_updated(_history(app, 2))
    _pump(app.root)
    assert app._history_index == 1
    assert str(app.btn_hist_up.cget("state")) == "normal"
    assert str(app.btn_hist_down.cget("state")) == "disabled"

    app._history_prev()
    _pump(app.root)
    assert str(app.btn_hist_up.cget("state")) == "disabled"
    assert str(app.btn_hist_down.cget("state")) == "normal"

    app._history_next()
    _pump(app.root)
    assert str(app.btn_hist_down.cget("state")) == "disabled"


def test_history_navigation_with_no_history_is_a_noop(app):
    app._want_auto_start = False
    _activate(app)
    app._ui_memory_updated({"previous": [], "history_index": -1,
                             "history_count": 0, "topic": ""})
    _pump(app.root)
    app._history_prev()
    app._history_next()
    _pump(app.root)
    assert app._history_index == -1
    assert app.sent_commands == []


def test_stop_shows_idle_and_start_keeps_history(app):
    """Spec 20/21: STOP -> IDLE, START keeps answer + history."""
    _activate(app)
    _show_answer(app, "Kept answer.")
    _pump(app.root)
    app._assistant_state = "ready"
    app._ui_memory_updated(_history(app, 2))
    _pump(app.root)

    app._ui_listening_stopped({"listening": False, "state": "IDLE",
                               "current": "half said", "can_send": True})
    _pump(app.root)
    assert "IDLE" in app.lbl_listen_state.cget("text")
    assert len(app._question_history) == 2
    assert "Kept answer." in _answer_text(app)

    app._ui_listening_started({"listening": True, "state": "LISTENING"})
    _pump(app.root)
    assert "Kept answer." in _answer_text(app)
    assert len(app._question_history) == 2
    assert app._live_transcript == ""


def test_send_resets_history_to_the_latest_entry(app):
    """Spec 9: a new SEND returns the selector to the latest question."""
    _activate(app)
    app._ui_memory_updated(_history(app, 3))
    _pump(app.root)
    app._history_prev()
    _history_prev = app._history_index
    assert _history_prev == 1

    app._ui_turn_submitted({"question": "brand new question"})
    _pump(app.root)
    assert app._history_index == 2, "back to the latest entry"


def test_blank_topic_does_not_erase_the_active_topic(app):
    """Spec 16: a memory push with an empty topic preserves the current one."""
    _activate(app)
    app._ui_memory_updated(_history(app, 2))
    _pump(app.root)
    assert app._active_topic == "topic 2"

    app._ui_memory_updated({"previous": [], "history_index": -1,
                            "history_count": 0, "topic": "",
                            "memory_points": []})
    _pump(app.root)
    assert app._active_topic == "topic 2", "topic must never be blanked"
