"""Phase-4 regression tests: stable streaming answer UI.

Pure-UI tests (no audio/backend/WebSocket). They drive the REAL
StealthOverlayApp and assert the Phase-4 guarantees:

  * the answer panel is created ONCE per generation (never per chunk)
  * the answer Text widget identity is stable through the whole generation
  * hundreds of chunks do NOT produce hundreds of geometry rebuilds
  * width resize mid-stream keeps the same stable widget
  * no <Configure>/reflow feedback loop while streaming
  * duplicate manual submission while generating is ignored
  * Refresh Answer intentionally starts a new generation
  * a stale/cancelled request can never modify the current answer
  * long answers stay vertically scrollable
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest
import tkinter as tk

DESKTOP_DIR = Path(__file__).resolve().parents[2] / "desktop"
sys.path.insert(0, str(DESKTOP_DIR))

LONG_ANSWER = (
    "Streaming answers must stay visually stable: the container is created "
    "once and only its content grows. No blinking, no reset, no reflow per "
    "token, no horizontal clipping, no forced scroll while the user reads. "
    "The panel follows the window width and the text rewraps naturally. "
    "Every wrapped line must stay reachable through the panel scrollbar so "
    "the user can always scroll back through the whole answer. "
) * 6

CHUNKS = [f"chunk-{i:03d} " + "x" * (30 + i % 70) for i in range(100)]


def _pump(root, rounds: int = 10):
    for _ in range(rounds):
        root.update()
        time.sleep(0.01)


def _answer_texts(app):
    out = []
    for frame in app._answer_widgets:
        try:
            if not frame.winfo_exists():
                continue
        except Exception:
            continue
        for child in frame.winfo_children():
            if isinstance(child, tk.Text):
                out.append(child)
    return out


@pytest.fixture(scope="module")
def overlay():
    import stealth_overlay as so

    class _UIOnlyApp(so.StealthOverlayApp):
        def _populate_devices(self):
            pass  # UI-only test: no audio device discovery

        def _realize_window(self):
            self.root.deiconify()  # map the window; skip affinity/region

    root = tk.Tk()
    app = _UIOnlyApp(root, api_url="http://127.0.0.1:9", session_id=0)
    _pump(root)
    yield app
    try:
        app._cancel_answer_auto_hide()
    except Exception:
        pass
    try:
        root.destroy()
    except Exception:
        pass


def _begin(app):
    app._assistant_state = "generating"
    app._ui_stream_answer("", reset=True)
    _pump(app.root)


# ─────────────────────────────────────────────────────────────────────────────
# TEST 5 / TEST 6 — container + Text identity stable across the generation
# ─────────────────────────────────────────────────────────────────────────────
def test_answer_panel_created_once_and_not_recreated_per_chunk(overlay):
    app = overlay
    _begin(app)
    frame0 = app._stable_answer_frame
    txt0 = app._stable_answer_txt
    assert frame0 is not None and txt0 is not None

    for delta in CHUNKS[:40]:
        app._ui_stream_answer(delta, append=True)
    _pump(app.root)

    # exactly ONE section rendered during streaming (the stable Answer frame)
    assert app._answer_widgets == [frame0], (
        "extra sections appeared during streaming")
    assert app._stable_answer_frame is frame0, "Answer frame was recreated"
    assert app._stable_answer_txt is txt0, "Answer Text was recreated"
    assert app._stream_buffer == "".join(CHUNKS[:40]), "chunk text lost"


def test_text_widget_identity_stable_through_finalize(overlay):
    app = overlay
    _begin(app)
    txt0 = app._stable_answer_txt
    for delta in CHUNKS[:30]:
        app._ui_stream_answer(delta, append=True)
    _pump(app.root)

    final = "".join(CHUNKS[:30])
    app._assistant_state = "ready"
    app._render_answer({"answer": final})
    _pump(app.root)

    assert app._stable_answer_txt is txt0, (
        "finalize recreated the streaming Text widget")
    assert any(t is txt0 for t in _answer_texts(app))
    assert final in txt0.get("1.0", "end")


# ─────────────────────────────────────────────────────────────────────────────
# TEST 7 — 100 chunks -> batched geometry (not 100 rebuilds)
# ─────────────────────────────────────────────────────────────────────────────
def test_100_chunks_do_not_create_100_geometry_rebuilds(overlay, monkeypatch):
    app = overlay
    _begin(app)
    flushes = []
    orig_flush = app._flush_answer_reflow

    def counting_flush():
        flushes.append(time.monotonic())
        orig_flush()

    monkeypatch.setattr(app, "_flush_answer_reflow", counting_flush)
    for delta in CHUNKS:  # 100 chunks queued in one event pass
        app._ui_stream_answer(delta, append=True)
    _pump(app.root, rounds=8)

    assert app._stream_buffer == "".join(CHUNKS)
    assert app._answer_widgets == [app._stable_answer_frame]
    # Geometry is batched once per idle pass — nowhere near 100 rebuilds.
    assert len(flushes) < 5, f"{len(flushes)} geometry passes for 100 chunks"


# ─────────────────────────────────────────────────────────────────────────────
# TEST 8 — width resize mid-stream keeps the same stable widget + follows width
# ─────────────────────────────────────────────────────────────────────────────
def test_width_resize_during_streaming_preserves_stable_widget(overlay):
    app, root = overlay, overlay.root
    _begin(app)
    txt0 = app._stable_answer_txt
    for delta in CHUNKS[:25]:
        app._ui_stream_answer(delta, append=True)
    _pump(root)

    root.geometry(f"{root.winfo_width() - 90}x640+60+60")
    _pump(root)
    root.geometry(f"{root.winfo_width() + 140}x640+60+60")
    _pump(root)

    assert app._stable_answer_txt is txt0, "width change recreated the widget"
    canvas_w = app.ans_canvas.winfo_width()
    assert all(t.winfo_width() <= canvas_w for t in _answer_texts(app)), (
        "streaming text overflowed after mid-stream resize")


# ─────────────────────────────────────────────────────────────────────────────
# TEST 9 — no <Configure> feedback loop while streaming
# ─────────────────────────────────────────────────────────────────────────────
def test_no_configure_feedback_loop_during_stream(overlay, monkeypatch):
    """Canvas size stays constant while content streams -> the canvas-level
    Configure handler must stay silent (no self-triggering loop)."""
    app, root = overlay, overlay.root
    _begin(app)
    configure_calls = []
    orig = app._on_answer_canvas_configure
    monkeypatch.setattr(app, "_on_answer_canvas_configure",
                        lambda e: (configure_calls.append(1), orig(e)))
    for delta in CHUNKS[:20]:
        app._ui_stream_answer(delta, append=True)
    _pump(root, rounds=6)

    # Content grows INSIDE a canvas that is never resized by streaming; the
    # canvas Configure handler must therefore not fire at all.
    assert configure_calls == [], (
        "streaming resized the canvas -> Configure feedback loop")
    # Geometry converges: two more idle passes change nothing.
    bbox = app.ans_canvas.bbox("all")
    assert bbox is not None
    _pump(root, rounds=6)
    assert app.ans_canvas.bbox("all") == bbox, "geometry kept changing"


# ─────────────────────────────────────────────────────────────────────────────
# TEST 10 — duplicate manual submission while generating is ignored
# ─────────────────────────────────────────────────────────────────────────────
def test_duplicate_manual_submission_ignored_while_generating(overlay, monkeypatch):
    app = overlay
    sent = []
    monkeypatch.setattr(app, "_request_manual_answer",
                        lambda q, note="", source="manual": sent.append(q))

    def ask(q):
        app.entry_manual.delete(0, "end")
        app.entry_manual.insert(0, q)
        app.send_manual_question()

    ask("Tell about reinforcement learning")
    assert len(sent) == 1
    app._assistant_state = "generating"  # WS reset would have set this
    # Second identical send while generating — MUST be ignored.
    ask("Tell about reinforcement learning")
    assert len(sent) == 1, "duplicate manual submission started a generation"
    # A DIFFERENT question while generating is NOT a duplicate.
    ask("How does vector search work?")
    assert len(sent) == 2


# ─────────────────────────────────────────────────────────────────────────────
# TEST 11 — Refresh Answer intentionally regenerates even while generating
# ─────────────────────────────────────────────────────────────────────────────
def test_refresh_answer_intentionally_regenerates(overlay, monkeypatch):
    app = overlay
    sent = []
    monkeypatch.setattr(app, "_request_manual_answer",
                        lambda q, note="", source="manual": sent.append(q))
    app._latest_detected = "Tell me about my Friday project."
    app._assistant_state = "generating"
    app.generate_latest_answer()
    assert len(sent) == 1, "Refresh Answer must always generate"
    assert app._active_gen_question == "tell me about my friday project."


# ─────────────────────────────────────────────────────────────────────────────
# TEST 12 — a stale/cancelled request can never touch the current answer
# ─────────────────────────────────────────────────────────────────────────────
def test_stale_request_cannot_modify_current_answer(overlay):
    app = overlay
    _begin(app)
    app._active_request_id = "req-B"
    app._ui_stream_answer("current streaming text", append=True)
    _pump(app.root)

    assert app._is_stale_request("req-A") is True
    assert app._is_stale_request("req-B") is False
    assert app._is_stale_request(None) is False

    before = app._stream_buffer
    # A cancel belonging to the OLD request must not wipe the new answer.
    app._ui_cancel_answer({"request_id": "req-A", "reason": "superseded"})
    _pump(app.root)
    assert app._stream_buffer == before, \
        "stale cancel wiped the current streaming answer"


def test_cancel_of_active_request_clears_and_ready(overlay):
    app = overlay
    _begin(app)
    app._active_request_id = "req-1"
    app._ui_stream_answer("partial text", append=True)
    _pump(app.root)
    app._ui_cancel_answer({"request_id": "req-1", "reason": "superseded"})
    _pump(app.root)
    assert app._assistant_state == "ready"
    assert app._stream_buffer == ""
    assert app._answer_widgets == []


# ─────────────────────────────────────────────────────────────────────────────
# TEST 13 — long answers remain vertically scrollable
# ─────────────────────────────────────────────────────────────────────────────
def test_long_answer_remains_vertically_scrollable(overlay):
    app, root = overlay, overlay.root
    app._assistant_state = "ready"
    app._render_answer({"answer": LONG_ANSWER})
    _pump(root)

    # Shrink to a narrow height so content MUST exceed the visible area.
    target_w = 300
    root.geometry(f"{target_w}x380+60+60")
    for _ in range(60):  # wait until the window manager applies the size
        _pump(root, rounds=1)
        if root.winfo_width() <= target_w + 10 and root.winfo_height() <= 410:
            break

    _pump(root)
    canvas_h = app.ans_canvas.winfo_height()
    bbox = app.ans_canvas.bbox("all")
    assert bbox is not None and bbox[3] > canvas_h, (
        f"content {bbox and bbox[3]} should exceed visible {canvas_h}")
    # scrollbar has a real range -> user can scroll to every line
    f0, f1 = app.ans_canvas.yview()
    assert f1 < 1.0, "scrollbar should span into scrollable territory"
    app.ans_canvas.yview_moveto(1.0)
    app.root.update()
    assert app.ans_canvas.yview()[1] >= 1.0


# ─────────────────────────────────────────────────────────────────────────────
# Generating state is stable text INSIDE the existing container
# ─────────────────────────────────────────────────────────────────────────────
def test_generating_placeholder_lives_inside_stable_container(overlay):
    app = overlay
    _begin(app)
    txt = app._stable_answer_txt
    assert "Generating" in txt.get("1.0", "end")
    assert app._answer_widgets == [app._stable_answer_frame]
    # first real chunk replaces the placeholder WITHOUT a new widget
    app._ui_stream_answer("I used faster-whisper", append=True)
    _pump(app.root)
    assert app._stable_answer_txt is txt
    assert "I used faster-whisper" in txt.get("1.0", "end")
    assert "Generating" not in txt.get("1.0", "end")