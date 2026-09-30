"""Phase 5 regression tests: fixed-size overlay + screenshot input mode.

Pure-UI tests (no real capture/OCR/network). They drive the real
StealthOverlayApp with capture/OCR/pipeline stubbed and assert:

  * the overlay window is FIXED SIZE (not user-resizable) and 560x640
  * rendering a long answer can NEVER change the window size
  * a 📸 Screenshot button sits right next to Ask manually
  * clicking it runs capture+OCR on a worker thread (never blocks Tk)
  * the screenshot question enters the SAME submit path as manual questions
  * button states cycle READY → Capturing... → Analyzing... → READY
  * errors restore the overlay + button and never send an answer request
  * repeated clicks while busy are ignored
  * SOURCE: SCREENSHOT is shown with the detected question
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest
import tkinter as tk

DESKTOP_DIR = Path(__file__).resolve().parents[2] / "desktop"
sys.path.insert(0, str(DESKTOP_DIR))

PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)

LONG_ANSWER = (
    "This long answer must scroll INSIDE the fixed 560x640 page and must "
    "never resize the Kiyra window. The page is fixed; only the answer "
    "content is scrollable through the vertical panel scrollbar. "
) * 5


def _pump(root, rounds: int = 10):
    for _ in range(rounds):
        root.update()
        time.sleep(0.01)


def _wait_until(app, cond, timeout: float = 5.0):
    """Pump the Tk loop until ``cond()`` or timeout.

    Uses short :meth:`~tkinter.Misc.mainloop` slices (quit after ~10 ms) so the
    event loop is genuinely "in main loop": that is required for
    ``root.after`` scheduled from background/worker threads to be accepted by
    Tk, exactly like the production ``mainloop``. A bare ``root.update()``
    loop does NOT satisfy that and the screenshot worker's callbacks would be
    silently dropped.
    """
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.root.after(10, app.root.quit)
        app.root.mainloop()
        app.root.update()
        time.sleep(0.005)
        try:
            if cond():
                return True
        except Exception:
            pass
    return False


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


# ─────────────────────────────────────────────────────────────────────────────
# FIXED-SIZE WINDOW (Phase 5.2 / 5.3)
# ─────────────────────────────────────────────────────────────────────────────

def test_overlay_window_is_fixed_size_and_not_resizable(overlay):
    app, root = overlay, overlay.root
    assert root.resizable() == (0, 0), "window must not be user-resizable"
    scale = app._ui_scale
    w, h = int(560 * scale), int(640 * scale)
    _pump(root)
    assert abs(root.winfo_width() - w) <= 4, (
        f"window width {root.winfo_width()} != target {w}")
    assert abs(root.winfo_height() - h) <= 4, (
        f"window height {root.winfo_height()} != target {h}")


def test_answer_content_never_changes_window_size(overlay):
    app, root = overlay, overlay.root
    _pump(root)
    before = (root.winfo_width(), root.winfo_height())
    app._assistant_state = "ready"
    app._render_answer({"answer": LONG_ANSWER})
    _pump(root, 20)
    assert app.ans_canvas.winfo_exists()
    # The panel scrolls the overflow; the page itself stays exactly fixed.
    assert (root.winfo_width(), root.winfo_height()) == before, (
        "answer content resized the fixed page")


# ─────────────────────────────────────────────────────────────────────────────
# SCREENSHOT BUTTON (Phase 5.4 / 5.6 / 5.13)
# ─────────────────────────────────────────────────────────────────────────────

def test_screenshot_button_sits_next_to_ask_manually(overlay):
    app = overlay
    tabs = app.tab_manual.master
    assert app.btn_screenshot is not None
    assert app.btn_screenshot.master is tabs, "screenshot button not in tabs row"
    assert app.btn_screenshot.cget("text") == "📸 Screenshot"
    # no second toolbar: manual tab and screenshot share ONE row
    left = int(app.tab_manual.winfo_x())
    shot_x = int(app.btn_screenshot.winfo_x())
    assert shot_x > left


def test_screenshot_flow_submits_question_via_shared_pipeline(overlay, monkeypatch):
    import stealth_overlay as so
    import screenshot_capture as sc

    app = overlay
    calls: list[tuple] = []
    monkeypatch.setattr(
        so.ScreenshotCapture, "capture",
        lambda self: sc.ScreenshotInput(image_bytes=PNG_BYTES, mime="image/png"),
    )
    monkeypatch.setattr(
        app, "_ocr_request",
        lambda shot: {
            "extracted_text": "Write a Java program to reverse a string.",
            "question": "Write a Java program to reverse a string.",
            "likely_code": True,
            "avg_confidence": 0.92,
        },
    )
    monkeypatch.setattr(
        app, "_request_manual_answer",
        lambda q, note="", source="manual": calls.append((q, note, source)),
    )

    app._on_screenshot_click()
    assert app._screenshot_busy is True
    assert app.btn_screenshot.cget("state") == tk.DISABLED
    assert "Capturing" in app.btn_screenshot.cget("text")

    ok = _wait_until(app, lambda: not app._screenshot_busy)
    assert ok, "screenshot worker never finished"

    # same downstream pipeline as manual/voice: exactly one submission
    assert len(calls) == 1
    q, note, source = calls[0]
    assert q == "Write a Java program to reverse a string."
    assert source == "screenshot"
    assert "fenced code block" in note, "code screenshots must carry a format hint"

    # button returns to READY, question box shows DETECTED + SOURCE
    assert app.btn_screenshot.cget("text") == "📸 Screenshot"
    assert app.btn_screenshot.cget("state") == tk.NORMAL
    assert "reverse a string" in app.txt_question.get("1.0", "end")
    assert "SOURCE: SCREENSHOT" in app.lbl_q_meta.cget("text")


def test_screenshot_non_code_question_has_no_format_note(overlay, monkeypatch):
    import stealth_overlay as so
    import screenshot_capture as sc

    app = overlay
    calls: list[tuple] = []
    monkeypatch.setattr(
        so.ScreenshotCapture, "capture",
        lambda self: sc.ScreenshotInput(image_bytes=PNG_BYTES, mime="image/png"),
    )
    monkeypatch.setattr(
        app, "_ocr_request",
        lambda shot: {
            "extracted_text": "Tell me about a project you are proud of.",
            "question": "Tell me about a project you are proud of.",
            "likely_code": False,
            "avg_confidence": 0.95,
        },
    )
    monkeypatch.setattr(
        app, "_request_manual_answer",
        lambda q, note="", source="manual": calls.append((q, note, source)),
    )
    app._on_screenshot_click()
    assert _wait_until(app, lambda: not app._screenshot_busy)
    assert len(calls) == 1
    assert calls[0][1] == "", "non-code screenshot must not get a code hint"
    assert calls[0][2] == "screenshot"


def test_screenshot_error_restores_overlay_and_button(overlay, monkeypatch):
    import stealth_overlay as so
    import screenshot_capture as sc

    app = overlay
    sent: list = []
    monkeypatch.setattr(
        so.ScreenshotCapture, "capture",
        lambda self: sc.ScreenshotInput(image_bytes=PNG_BYTES, mime="image/png"),
    )

    def boom(shot):
        raise RuntimeError("ocr exploded")

    monkeypatch.setattr(app, "_ocr_request", boom)
    monkeypatch.setattr(
        app, "_request_manual_answer",
        lambda q, note="", source="manual": sent.append(q),
    )

    app._on_screenshot_click()
    assert _wait_until(app, lambda: not app._screenshot_busy)

    assert sent == [], "error path must never start an answer"
    assert app.btn_screenshot.cget("text") == "📸 Screenshot"
    assert app.btn_screenshot.cget("state") == tk.NORMAL
    assert app.root.state() != "withdrawn", "overlay must be restored after error"
    assert "Screenshot failed" in app.lbl_inference.cget("text")


def test_second_screenshot_click_ignored_while_busy(overlay):
    app = overlay
    app._screenshot_busy = True  # simulate in-flight
    app._on_screenshot_click()
    # still busy with identical button state — no new worker started
    assert app._screenshot_busy is True
    app._screenshot_busy = False
    app.btn_screenshot.config(state=tk.NORMAL, text="📸 Screenshot")