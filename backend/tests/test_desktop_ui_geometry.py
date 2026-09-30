"""Regression tests: the answer panel is fully responsive to overlay width.

Pure-UI geometry tests — no audio, no backend, no WebSocket. They build the
real StealthOverlayApp with device discovery stubbed out, render answers,
resize the root window, and assert:

  * the answer canvas window always equals the answer container width
  * answer Text widgets never exceed their container (no horizontal clipping)
  * wrapped text strictly follows width changes in BOTH directions
    (no stale/hardcoded wrap width)
  * section Text heights cover their wrapped line count, so rewrapped lines
    stay reachable through the panel's vertical scrollbar
  * streaming answers reflow on resize without content loss or duplication
  * <Configure> handling is idempotent (no geometry feedback loop)
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
    "The SOLID principles are five design guidelines: Single Responsibility, "
    "Open-Closed, Liskov Substitution, Interface Segregation, and Dependency "
    "Inversion. Each principle keeps code easier to refactor, test, and reuse. "
) * 3


def _pump(root, rounds: int = 8):
    """Dispatch pending Tk events (real event processing, not just idletasks,
    so <Configure> bindings and after_idle callbacks actually run)."""
    for _ in range(rounds):
        root.update()
        time.sleep(0.01)


def _min_width(root) -> int:
    res = root.tk.call("wm", "minsize", root._w)
    if isinstance(res, (tuple, list)):
        return int(res[0])
    return int(str(res).split()[0])


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


def _displaylines(txt) -> int:
    n = txt.count("1.0", "end", "displaylines")
    if isinstance(n, tuple):
        n = n[0] if n else 0
    return int(n or 0)


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


def test_answer_width_follows_overlay_both_directions(overlay):
    app, root = overlay, overlay.root
    app._assistant_state = "ready"
    app._render_answer({"answer": LONG_ANSWER})
    _pump(root)

    base = root.winfo_width()
    mw = _min_width(root)
    narrow, wide = max(base - 80, mw), base + 120

    samples = []
    # includes the exact minimum width (compact state) plus both directions
    for w in (narrow, wide, mw, narrow, wide):
        root.geometry(f"{w}x640+60+60")
        _pump(root)
        canvas_w = app.ans_canvas.winfo_width()
        win_w = int(app.ans_canvas.itemcget(app.answer_win, "width"))
        texts = _answer_texts(app)
        assert texts, "answer section missing"
        txt_widths = [t.winfo_width() for t in texts]

        # canvas window follows the container exactly (single source of truth)
        assert win_w == canvas_w, f"canvas window {win_w} != canvas {canvas_w}"
        assert app._answer_inner_width == canvas_w

        # no answer widget may exceed its container -> no horizontal clipping
        assert all(tw <= canvas_w for tw in txt_widths), (
            f"text widths {txt_widths} overflow container {canvas_w}")
        samples.append((w, max(txt_widths)))

    # strictly follows width changes in both directions (no stale wrap width)
    for (w0, t0), (w1, t1) in zip(samples, samples[1:]):
        if w1 < w0:
            assert t1 < t0, (
                f"narrowing {w0}->{w1} left stale wrap (text {t0}->{t1})")
        elif w1 > w0:
            assert t1 > t0, (
                f"widening {w0}->{w1} did not widen text ({t0}->{t1})")

    # question widget stays responsive too
    assert app.txt_question.winfo_width() <= root.winfo_width()


def test_reflow_height_covers_wrapped_lines(overlay):
    app, root = overlay, overlay.root
    app._assistant_state = "ready"
    app._render_answer({"answer": LONG_ANSWER})
    _pump(root)
    base = root.winfo_width()
    mw = _min_width(root)
    narrow, wide = max(base - 80, mw), base + 120

    root.geometry(f"{wide}x640+60+60")
    _pump(root)
    wide_texts = _answer_texts(app)
    assert wide_texts
    wide_lines = sum(_displaylines(t) for t in wide_texts)

    root.geometry(f"{narrow}x640+60+60")
    _pump(root)
    narrow_texts = _answer_texts(app)
    assert narrow_texts

    narrow_lines = sum(_displaylines(t) for t in narrow_texts)
    # narrowing rewraps into MORE lines…
    assert narrow_lines > wide_lines, (
        f"narrowing did not rewrap: wide={wide_lines} narrow={narrow_lines}")
    # …and every wrapped line is inside its Text widget (reachable via the
    # panel scrollbar), not clipped behind a stale fixed height.
    for t in narrow_texts:
        lines = _displaylines(t)
        assert int(t.cget("height")) >= lines, (
            f"section height {t.cget('height')} < wrapped lines {lines}")


def test_streaming_answer_reflows_without_content_loss(overlay):
    app, root = overlay, overlay.root
    mw = _min_width(root)
    app._assistant_state = "generating"
    app._ui_stream_answer("", reset=True)
    _pump(root)

    pieces = []
    for i in range(6):
        delta = f" sentence-{i} " + LONG_ANSWER[:80]
        pieces.append(delta)
        app._ui_stream_answer(delta, append=True)
        _pump(root)
        if i == 2:  # resize MID-stream
            target = max(root.winfo_width() - 80, mw)
            root.geometry(f"{target}x640+60+60")
            _pump(root)

    # lifecycle untouched: buffer is exactly the concatenation of chunks
    assert app._stream_buffer == "".join(pieces)

    canvas_w = app.ans_canvas.winfo_width()
    texts = _answer_texts(app)
    assert texts, "streaming answer section missing"
    assert all(t.winfo_width() <= canvas_w for t in texts), (
        "streaming text overflowed the container after resize")

    # finish the stream: final render also responsive
    final = "".join(pieces)
    app._assistant_state = "ready"
    app._render_answer({"answer": final})
    _pump(root)
    canvas_w = app.ans_canvas.winfo_width()
    assert all(t.winfo_width() <= canvas_w for t in _answer_texts(app))
    assert any(final[:40] in t.get("1.0", "end")
               for t in _answer_texts(app)), "final answer text lost"


def test_configure_handler_is_idempotent_no_feedback_loop(overlay, monkeypatch):
    """One forced-width per real width change; repeat events are no-ops."""
    app, root = overlay, overlay.root
    app._assistant_state = "ready"
    app._render_answer({"answer": LONG_ANSWER})
    _pump(root)

    calls: list = []
    orig = app.ans_canvas.itemconfigure

    def counting(*args, **kwargs):
        calls.append(args)
        return orig(*args, **kwargs)

    monkeypatch.setattr(app.ans_canvas, "itemconfigure", counting)

    mw = _min_width(root)
    current = root.winfo_width()
    target = current - 60 if current - 60 >= mw else current + 60
    assert target != current
    root.geometry(f"{target}x640+60+60")
    _pump(root)
    assert len(calls) == 1, (
        f"expected exactly 1 forced width per resize, got {len(calls)}")

    # same width again -> zero further work (cached-width guard)
    root.geometry(f"{target}x640+60+60")
    _pump(root)
    assert len(calls) == 1, "repeat Configure re-forced the same width"
