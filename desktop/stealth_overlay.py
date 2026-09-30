"""
Stealth Interview Assistant — Production-ready Windows overlay.

FEATURES:
- Invisible to screen capture (SetWindowDisplayAffinity)
- Real-time audio capture from Zoom/Teams/Meet
- WebSocket streaming to backend for fast transcription
- Instant answer display (STAR framework + key points)
- Click-through mode for seamless interaction
- Auto-hide answers after configurable delay
- Keyboard shortcuts: F9 (toggle visibility), F10 (toggle click-through)
"""
from __future__ import annotations

import functools
import io
import json
import math
import os
import re
import threading
import time
import wave
import sys
import queue
import urllib.request
import tkinter as tk
from tkinter import ttk, messagebox
from typing import Optional
import ctypes
import ctypes.wintypes


def _declare_dpi_awareness():
    """Declare DPI awareness BEFORE any Tk window exists.

    On a 125% display an unaware process gets virtualized coordinates, so
    SetWindowRgn receives a mismatched region -> clipped / white / ghost
    overlay regions and screenshot bounds that disagree with the window.
    """
    if sys.platform != "win32":
        return
    try:
        # PROCESS_PER_MONITOR_DPI_AWARE_V2
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except (AttributeError, OSError):
        pass
    try:
        if ctypes.windll.shcore.SetProcessDpiAwareness(2) in (0, 1):  # S_OK / already set
            return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass


_declare_dpi_awareness()

try:
    import numpy as np
except Exception:
    np = None

try:
    import sounddevice as sd
except Exception:
    sd = None

try:
    import websockets
    import asyncio
except Exception:
    websockets = None
    asyncio = None

from audio_loopback import (
    LoopbackCapture,
    MicrophoneCapture,
    _MixingChunkSink,
    bounded_put_drop_oldest,
    enumerate_microphones,
    find_wasapi_loopback_device,
    validate_wasapi_loopback,
)

from screenshot_capture import (
    ScreenshotCapture,
    ScreenshotInput,
    QuestionInput,
)


# ─────────────────────────────────────────────────────────────────────────────
# Audio Processing
# ─────────────────────────────────────────────────────────────────────────────

def pcm16_from_float32(arr: np.ndarray) -> np.ndarray:
    """Convert float32 in -1..1 to int16 PCM."""
    if arr.size == 0:
        return np.zeros(0, dtype=np.int16)
    clipped = np.clip(arr, -1.0, 1.0)
    return (clipped * 32767.0).astype(np.int16)


def resample_linear(x: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    """Simple linear resampling."""
    if src_sr == dst_sr:
        return x
    if x.size == 0:
        return np.zeros(0, dtype=x.dtype)
    duration = x.shape[0] / src_sr
    dst_n = int(math.ceil(duration * dst_sr))
    if dst_n <= 0:
        return np.zeros(0, dtype=x.dtype)
    src_times = np.linspace(0, duration, num=x.shape[0], endpoint=False)
    dst_times = np.linspace(0, duration, num=dst_n, endpoint=False)
    return np.interp(dst_times, src_times, x).astype(x.dtype)


def encode_wav_bytes(samples: np.ndarray, samplerate: int = 16000) -> bytes:
    """Encode mono int16 numpy array as WAV bytes."""
    if samples.size == 0:
        samples = np.zeros(1, dtype=np.int16)
    bio = io.BytesIO()
    with wave.open(bio, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(samplerate)
        wf.writeframes(samples.tobytes())
    return bio.getvalue()


# ─────────────────────────────────────────────────────────────────────────────
# Stealth Overlay App
# ─────────────────────────────────────────────────────────────────────────────

class StealthOverlayApp:
    def __init__(self, root: tk.Tk, api_url: str = "http://localhost:8000", session_id: int = 1):
        self.root = root
        self.api_url = api_url.rstrip("/")
        self.session_id = session_id

        # Audio params
        self.target_sr = 16000
        self.chunk_seconds = 0.25  # 250ms frames — bounded real-time chunking

        # Bounded per-device ingestion queues (drop oldest, never block callback)
        self._device_queue_size = 64

        # Device selection
        self.mic_device = None
        self.loopback_device = None

        # Internal state
        self._audio_queue: queue.Queue[bytes] = queue.Queue(maxsize=50)
        self._stop_event = threading.Event()
        self._capture_thread: Optional[threading.Thread] = None
        self._ws_thread: Optional[threading.Thread] = None
        self._ws_loop = None
        self.websocket = None
        self.ws_connected = False
        self._ws_audio_count = 0
        self._ws_audio_dropped = 0

        # UI state
        self.is_stealth_active = True
        self.is_click_through = False
        self.is_capturing = False
        self.answer_auto_hide_task = None
        # Auto-hide delay for a COMPLETED answer only (never during a stream).
        # Configurable via KIYRA_ANSWER_HIDE_MS (ms), clamped to >= 10s.
        try:
            self.answer_hide_delay_ms = max(
                10000, int(os.environ.get("KIYRA_ANSWER_HIDE_MS", "20000")))
        except (TypeError, ValueError):
            self.answer_hide_delay_ms = 20000
        self._active_request_id = None  # request_id of the in-flight answer
        # Normalized question being generated right now — duplicate manual
        # submissions of the same question while it generates are ignored.
        self._active_gen_question = None
        # THE one Answer section (frame + Text) reused across a generation:
        # created once, updated in place — never rebuilt per chunk.
        self._stable_answer_frame = None
        self._stable_answer_txt = None
        # Batched geometry: text inserts are immediate; height re-fit /
        # scrollregion / auto-scroll run once per idle pass, never per token.
        self._answer_reflow_pending = False
        # Bottom-tracking: auto-scroll only while the user sits at the bottom.
        self._answer_scroll_pinned = True
        # UI-side latency metrics (monotonic).
        self._perf_started = None            # answer_generation_started
        self._perf_first_chunk_recv = None   # ws recv time of first chunk
        self._perf_first_chunk_rendered = None  # Tk render time of first chunk
        self._perf_chunks = 0
        self._mic_list = []
        self._loop_name = None
        self._mic_choice = None
        self._loop_choice = None
        self._last_loop = None
        self._region_after = None
        self._region_applied_once = False
        self._affinity_reported = False
        self._ui_scale = 1.0
        # Screenshot input mode: true while capture/OCR is in flight so
        # repeated clicks are ignored and the overlay stays responsive.
        self._screenshot_busy = False
        self._inf_clear_task = None

        # Build UI (hidden first so the dark surface paints correctly, then
        # realize; otherwise Windows shows a blank/white base and the stealth
        # affinity gets applied before a real hwnd exists).
        self._init_window()
        self._init_styles()
        self.root.withdraw()
        self._build_ui()
        self._realize_window()
        self._bind_shortcuts()
        self._populate_devices()

        print("[Stealth] Initialized. Press F9 to toggle visibility, F10 for click-through.")

    # ─────────────────────────────────────────────────────────────────────────
    # Window & UI
    # ─────────────────────────────────────────────────────────────────────────

    def _init_window(self):
        # Design spec is in logical px; scale to physical for this display.
        try:
            dpi = ctypes.windll.user32.GetDpiForSystem() or 96
        except Exception:
            dpi = 96
        self._ui_scale = dpi / 96.0
        w, h = int(560 * self._ui_scale), int(640 * self._ui_scale)
        x, y = int(60 * self._ui_scale), int(60 * self._ui_scale)
        self.root.title("Kiyra — AI Interview Assistant")
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        # Fixed-size page: the WINDOW never stretches (not user-resizable);
        # only the answer CONTENT inside it scrolls — long answers cannot
        # resize the window because the pinned geometry wins over children.
        self.root.resizable(False, False)
        self.root.minsize(int(480 * self._ui_scale), int(540 * self._ui_scale))
        self.root.attributes("-topmost", True)
        self.root.configure(bg="#121315")

    def _realize_window(self):
        """Show + paint the dark UI, then layer alpha, rounded region and
        capture-exclusion. Order matters on Windows: mapping the window first
        lets Tk paint the charcoal surface, so we never see a white flash, and
        the affinity is set on a real, top-level hwnd (so screenshots actually
        exclude it)."""

        def _after_paint():
            self.root.deiconify()
            try:
                self.root.attributes("-alpha", 0.98)
            except Exception:
                pass
            self.root.update_idletasks()
            # Region FIRST, capture-exclusion LAST: nothing may touch the
            # window after SetWindowDisplayAffinity or it can be invalidated.
            self._apply_rounded_corners()
            self._apply_stealth_affinity()

        self.root.bind("<Configure>", self._on_win_configure)
        # Wait two event iterations so every widget paints before we touch
        # window-layered/region APIs (avoids the blank-white layered surface).
        self.root.after(60, _after_paint)

    def _on_win_configure(self, _e=None):
        """Re-apply the rounded region (+ re-assert exclusion) when resized."""
        try:
            if self._region_after is not None:
                self.root.after_cancel(self._region_after)
        except Exception:
            pass
        self._region_after = self.root.after(50, self._refresh_window_shape)

    def _refresh_window_shape(self):
        self._region_after = None
        self._apply_rounded_corners()
        self._apply_stealth_affinity()

    def _init_styles(self):
        self.font_family = "Segoe UI" if sys.platform == "win32" else "Helvetica"
        self.mono_family = "Consolas" if sys.platform == "win32" else "Menlo"
        # Kiyra dark-assistant palette (original styling; the reference is
        # used only for layout / hierarchy / density, never for branding).
        self.bg_color = "#121315"      # window charcoal
        self.panel_bg = "#1a1d21"      # top bar / panels
        self.card_bg = "#20242b"       # cards
        self.code_bg = "#14161a"       # code blocks
        self.border_color = "#2b313a"  # thin subtle border
        self.accent_color = "#7c8aff"  # Kiyra indigo accent
        self.text_primary = "#f2f4f7"
        self.text_secondary = "#9aa3b2"
        self.success_color = "#34d399"
        self.warning_color = "#fbbf24"
        self.danger_color = "#f87171"
        # Assistant UI state (not audio state — audio lives in audio_loopback).
        self._assistant_state = "offline"
        self._stream_buffer = ""
        self._last_code = ""
        self._current_question = ""
        self._latest_detected = ""
        self._manual_mode = False
        self._is_pinned = True

    def _apply_stealth_affinity(self):
        """Hide window from screenshots on Windows.

        SetWindowDisplayAffinity must target a REAL top-level window handle
        and we verify it actually stuck via GetWindowDisplayAffinity; if not,
        we retry every candidate handle (Tk client, its parent frame). Some
        capture APIs bypass it, but this covers OBS / Teams / Meet / ShareX.
        """
        try:
            if sys.platform != "win32":
                return
            WDA_EXCLUDEFROMCAPTURE = 17
            WDA_NONE = 0
            user32 = ctypes.windll.user32
            clients = [
                self.root.winfo_id(),
                user32.GetParent(self.root.winfo_id()),
                user32.GetAncestor(self.root.winfo_id(), 2),  # GA_ROOT
            ]
            applied = False
            for hwnd in dict.fromkeys(h for h in clients if h):
                ok = user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
                if not ok:
                    continue
                cur = ctypes.c_uint32(0)
                user32.GetWindowDisplayAffinity(hwnd, ctypes.byref(cur))
                if cur.value == WDA_EXCLUDEFROMCAPTURE:
                    if not self._affinity_reported:
                        print(f"[Stealth] Capture exclusion ACTIVE on hwnd={hwnd:#x}")
                        self._affinity_reported = True
                    applied = True
                    ctypes.windll.user32.InvalidateRect(hwnd, None, True)
                    break
            if not applied:
                print("[Stealth] WARNING: screen-capture exclusion could not be verified.",
                      "The overlay may be visible in screenshots.")
        except Exception as e:
            print(f"[Stealth] Could not set affinity: {e}")

    def _apply_rounded_corners(self):
        """Give the toplevel rounded 12px corners via Win32 region on Windows."""
        try:
            if sys.platform != "win32":
                return
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id()) or self.root.winfo_id()
            rect = ctypes.wintypes.RECT()
            if not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return
            w = rect.right - rect.left
            h = rect.bottom - rect.top
            if w <= 0 or h <= 0:
                return
            r = max(8, int(12 * self._ui_scale))
            rgn = ctypes.windll.gdi32.CreateRoundRectRgn(0, 0, w + 1, h + 1, r, r)
            ctypes.windll.user32.SetWindowRgn(hwnd, rgn, True)
            if not self._region_applied_once:
                print("[Stealth] Rounded corners applied")
                self._region_applied_once = True
        except Exception as e:
            print(f"[Stealth] Rounded corners skipped: {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # UI Construction — Kiyra assistant layout
    # ─────────────────────────────────────────────────────────────────────────

    def _btn(self, parent, text, command, bg=None, fg=None, font_size=9, state=tk.NORMAL,
             padx=10, pady=4, width=None):
        """Small flat ttk-like button helper (Tk classic button with flat relief)."""
        b = tk.Button(
            parent, text=text, command=command,
            bg=bg if bg else self.panel_bg, fg=fg if fg else self.text_primary,
            font=(self.font_family, font_size, "bold"), relief=tk.FLAT,
            padx=padx, pady=pady, width=width, cursor="hand2",
            activebackground=bg if bg else self.card_bg,
            activeforeground=fg if fg else self.text_primary,
            borderwidth=0, highlightthickness=0,
        )
        return b

    def _build_ui(self):
        body = tk.Frame(self.root, bg=self.bg_color)
        body.pack(fill=tk.BOTH, expand=True)
        self._body = body

        # ─── Top Bar (icon, name, status, session, controls) ───────────────────
        top = tk.Frame(body, bg=self.panel_bg, height=56)
        top.pack(fill=tk.X, side=tk.TOP)
        top.pack_propagate(False)

        # Kiyra icon block (ASCII glyph; no third-party branding).
        icon_box = tk.Frame(top, bg=self.panel_bg)
        icon_box.pack(side=tk.LEFT, padx=(14, 8), pady=8)
        self.status_dot = tk.Label(icon_box, text="◆", bg=self.panel_bg, fg=self.accent_color,
                                   font=(self.font_family, 18, "bold"))
        self.status_dot.pack()

        title_col = tk.Frame(top, bg=self.panel_bg)
        title_col.pack(side=tk.LEFT, pady=6)
        tk.Label(title_col, text="Kiyra", bg=self.panel_bg, fg=self.text_primary,
                 font=(self.font_family, 12, "bold")).pack(anchor="w")
        tk.Label(title_col, text="AI Interview Assistant", bg=self.panel_bg,
                 fg=self.text_secondary, font=(self.font_family, 8)).pack(anchor="w")

        self.lbl_status = tk.Label(
            top, text="● Offline", bg=self.panel_bg, fg=self.danger_color,
            font=(self.font_family, 9, "bold"), padx=8, pady=2, borderwidth=1,
            relief=tk.SOLID, highlightthickness=0,
        )
        self.lbl_status.pack(side=tk.LEFT, padx=(14, 0))

        self.lbl_session = tk.Label(
            top, text=f"Session {self.session_id}", bg=self.panel_bg,
            fg=self.text_secondary, font=(self.font_family, 8))
        self.lbl_session.pack(side=tk.LEFT, padx=10)

        # Top-right controls: pin, settings, close.
        ctrls = tk.Frame(top, bg=self.panel_bg)
        ctrls.pack(side=tk.RIGHT, padx=8)
        self.btn_pin = self._btn(ctrls, "📌", lambda: self.toggle_pin(), font_size=9, padx=5, pady=2)
        self.btn_pin.pack(side=tk.LEFT, padx=1)
        self.btn_settings = self._btn(ctrls, "⚙", self.open_settings, font_size=11, padx=5, pady=2)
        self.btn_settings.pack(side=tk.LEFT, padx=1)
        self.btn_close = self._btn(ctrls, "✕", self._on_close, fg=self.danger_color, font_size=11, padx=5, pady=2)
        self.btn_close.pack(side=tk.LEFT, padx=1)

        # ─── Question Header ────────────────────────────────────────────────────
        q_frame = tk.Frame(body, bg=self.card_bg)
        q_frame.pack(fill=tk.X, padx=12, pady=(12, 6))

        q_head = tk.Frame(q_frame, bg=self.card_bg)
        q_head.pack(fill=tk.X)
        tk.Label(q_head, text="DETECTED QUESTION", bg=self.card_bg, fg=self.accent_color,
                 font=(self.font_family, 8, "bold")).pack(side=tk.LEFT)
        self.lbl_q_meta = tk.Label(q_head, text="", bg=self.card_bg, fg=self.text_secondary,
                                   font=(self.font_family, 8))
        self.lbl_q_meta.pack(side=tk.RIGHT)

        self.txt_question = tk.Text(
            q_frame, height=3, bg=self.card_bg, fg=self.text_primary,
            font=(self.font_family, 10, "bold"), wrap=tk.WORD, relief=tk.FLAT,
            borderwidth=0, padx=2, pady=2, highlightthickness=0
        )
        self.txt_question.pack(fill=tk.BOTH, expand=True)
        self.txt_question.insert("1.0", "Listening for the interviewer's question…")
        self.txt_question.config(state=tk.DISABLED)

        btn_q = tk.Frame(q_frame, bg=self.card_bg)
        btn_q.pack(fill=tk.X, pady=(0, 6))
        self.btn_refresh = self._btn(btn_q, "⟳ Refresh Answer", self.generate_latest_answer,
                                     bg="#2a3040", fg=self.text_primary, font_size=8, padx=8, pady=3)
        self.btn_refresh.pack(side=tk.LEFT)
        self.lbl_inference = tk.Label(btn_q, text="", bg=self.card_bg, fg=self.text_secondary,
                                      font=(self.font_family, 8))
        self.lbl_inference.pack(side=tk.RIGHT)

        # ─── Answer Mode Tabs ───────────────────────────────────────────────────
        tabs = tk.Frame(body, bg=self.bg_color)
        tabs.pack(fill=tk.X, padx=12, pady=(6, 0))

        self.tab_latest = self._btn(tabs, "Latest", lambda: self._switch_tab(False),
                                    bg=self.card_bg, fg=self.text_primary, font_size=9,
                                    padx=14, pady=4)
        self.tab_latest.pack(side=tk.LEFT, padx=(0, 4))
        self.tab_manual = self._btn(tabs, "Ask manually", lambda: self._switch_tab(True),
                                    bg=self.panel_bg, fg=self.text_secondary, font_size=9,
                                    padx=14, pady=4)
        self.tab_manual.pack(side=tk.LEFT)

        self.btn_screenshot = self._btn(
            tabs, "📸 Screenshot", self._on_screenshot_click,
            bg="#2a3040", fg=self.text_primary, font_size=9, padx=10, pady=4)
        self.btn_screenshot.pack(side=tk.LEFT, padx=(8, 0))

        # ─── Answer Area ────────────────────────────────────────────────────────
        ans_container = tk.Frame(body, bg=self.bg_color)
        ans_container.pack(fill=tk.BOTH, expand=True, padx=12, pady=(8, 6))

        # Answer sections scrolled widget
        sec_scroll = tk.Scrollbar(ans_container)
        sec_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        self.ans_canvas = tk.Canvas(
            ans_container, bg=self.bg_color, highlightthickness=0,
            yscrollcommand=sec_scroll.set
        )
        self.ans_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sec_scroll.config(command=self.ans_canvas.yview)

        self.answer_inner = tk.Frame(self.ans_canvas, bg=self.bg_color)
        # Cached canvas-window width: <Configure> handling must be idempotent
        # (no geometry feedback loop) and is the single source of truth for the
        # usable answer width that Text wrapping follows.
        self._answer_inner_width = 0
        self.answer_win = self.ans_canvas.create_window(
            (0, 0), window=self.answer_inner, anchor="nw", width=self.ans_canvas.winfo_reqwidth()
        )
        self.answer_inner.bind("<Configure>", self._on_answer_inner_configure)
        self.ans_canvas.bind("<Configure>", self._on_answer_canvas_configure)

        self._answer_widgets = []   # list of section frames for reset
        self._answer_scroll = 0

        # ─── Manual Ask Input ───────────────────────────────────────────────────
        manual = tk.Frame(body, bg=self.bg_color)
        manual.pack(fill=tk.X, padx=12, pady=(0, 8))

        self.entry_manual = tk.Entry(
            manual, bg=self.card_bg, fg=self.text_primary,
            font=(self.font_family, 10), insertbackground=self.text_primary,
            relief=tk.FLAT, borderwidth=0, highlightthickness=1,
            highlightbackground=self.border_color, highlightcolor=self.accent_color
        )
        self.entry_manual.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=6)
        self.entry_manual.insert(0, "Ask anything…")
        self.entry_manual.bind("<Return>", lambda e: self.send_manual_question())
        self.entry_manual.bind("<FocusIn>", self._entry_focus_in)

        self.btn_send = self._btn(manual, "Send", self.send_manual_question,
                                  bg=self.accent_color, fg="#0b0d12", font_size=9,
                                  padx=12, pady=6)
        self.btn_send.pack(side=tk.RIGHT, padx=(6, 0))

        # ─── Footer ─────────────────────────────────────────────────────────────
        footer = tk.Frame(body, bg=self.panel_bg, height=30)
        footer.pack(fill=tk.X, side=tk.BOTTOM)
        footer.pack_propagate(False)

        self.lbl_capture = tk.Label(
            footer, text="Ready  •  F9 toggle  •  F10 click-through",
            bg=self.panel_bg, fg=self.text_secondary, font=(self.font_family, 8))
        self.lbl_capture.pack(side=tk.LEFT, padx=12, pady=6)

        self.btn_start = self._btn(footer, "▶ Start", self.start_capture,
                                   bg="#1f6f4f", fg="#d7f7e8", font_size=8, padx=8, pady=3)
        self.btn_start.pack(side=tk.RIGHT, padx=(4, 6))
        self.btn_stop = self._btn(footer, "⏹ Stop", self.stop_capture,
                                  bg="#712a2a", fg="#f7d7d7", font_size=8, padx=8, pady=3,
                                  state=tk.DISABLED)
        self.btn_stop.pack(side=tk.RIGHT)

        # Initial tab state
        self._set_tab_active(False)
        self.entry_manual.pack_forget()
        self.btn_send.pack_forget()

    # ─────────────────────────────────────────────────────────────────────────
    # Tab / Input Switching
    # ─────────────────────────────────────────────────────────────────────────

    def _switch_tab(self, manual: bool):
        """Flip between Latest and Ask-manually modes."""
        self._manual_mode = manual
        self._set_tab_active(manual)
        if manual:
            self.entry_manual.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=6)
            self.btn_send.pack(side=tk.RIGHT, padx=(6, 0))
        else:
            self.entry_manual.pack_forget()
            self.btn_send.pack_forget()

    def _set_tab_active(self, manual: bool):
        active = self.accent_color if not manual else self.panel_bg
        self.tab_latest.config(bg=active if not manual else self.panel_bg)
        self.tab_manual.config(bg=self.panel_bg if not manual else self.accent_color)
        self.tab_latest.config(fg=("#0b0d12" if not manual else self.text_secondary))
        self.tab_manual.config(fg=("#0b0d12" if manual else self.text_secondary))

    def _entry_focus_in(self, _e):
        if self.entry_manual.get() == "Ask anything…":
            self.entry_manual.delete(0, tk.END)
            self.entry_manual.config(fg=self.text_primary)

    # ─────────────────────────────────────────────────────────────────────────
    # Screenshot Input Mode (Phase 5)
    # ─────────────────────────────────────────────────────────────────────────
    #
    # SCREENSHOT → capture (worker thread, Kiyra hidden for the grab) → OCR
    # (backend /api/vision/extract) → extracted question → the SAME WebSocket
    # pipeline as voice/manual questions (memory → RAG → ContextBuilder →
    # Bonsai → streamed answer). The Tk thread never blocks: all heavy work
    # happens in a worker thread and UI updates come back via root.after().

    def _on_screenshot_click(self):
        """Button handler: start capture + OCR off the Tk thread."""
        if self._screenshot_busy:
            print("[UI] Screenshot already in progress — click ignored")
            return
        self._screenshot_busy = True
        self.btn_screenshot.config(state=tk.DISABLED, text="📸 Capturing...")
        threading.Thread(target=self._capture_worker, daemon=True).start()

    def _set_screenshot_state(self, label: str):
        """Update only the button caption (never the whole overlay)."""
        def cb():
            try:
                if not self._screenshot_busy and label != "Analyzing...":
                    label = "Screenshot"
                self.btn_screenshot.config(text=f"📸 {label}")
            except Exception:
                pass
        self._safe_after(cb)

    def _restore_overlay_after_capture(self):
        """Bring the overlay back after a temp-hide capture (never left hidden)."""
        def cb():
            try:
                self.root.deiconify()
                self.root.attributes("-topmost", True)
            except Exception:
                pass
        self._safe_after(cb)

    def _capture_worker(self):
        """Worker thread: temp-hide → grab screen → restore → OCR → submit.

        Restore is guaranteed in ``finally``: even if capture or OCR raises,
        the overlay is never left hidden and the button returns to READY.
        """
        was_viewable = True
        restored = False
        try:
            hide_done = threading.Event()

            def _hide():
                nonlocal was_viewable
                try:
                    was_viewable = self.root.winfo_viewable()
                except Exception:
                    was_viewable = True
                if was_viewable:
                    self.root.withdraw()
                hide_done.set()

            self._safe_after(_hide)
            hide_done.wait(timeout=2.0)
            # One UI tick lets the unmap actually hit the compositor before the
            # grab so Kiyra is genuinely absent from the screenshot.
            time.sleep(0.03)

            shot = ScreenshotCapture().capture()

            self._restore_overlay_after_capture()
            restored = True
            self._set_screenshot_state("Analyzing...")

            payload = self._ocr_request(shot)
            self._safe_after(lambda: self._on_screenshot_ocr_ready(payload, shot))
        except Exception as e:
            if not restored:
                self._restore_overlay_after_capture()
            err_msg = str(e)  # bind now: `e` is cleared when `except` ends
            self._safe_after(lambda: self._on_screenshot_error(err_msg))
        finally:
            # Always release the busy lock + re-enable the button (COMPLETE/
            # ERROR both return the button to READY).
            def _finish():
                self._screenshot_busy = False
                try:
                    self.btn_screenshot.config(state=tk.NORMAL, text="📸 Screenshot")
                except Exception:
                    pass
            self._safe_after(_finish)

    def _ocr_request(self, shot: ScreenshotInput) -> dict:
        """POST the captured image to the backend OCR-only endpoint.

        Returns the parsed ``/api/vision/extract`` payload. Runs on the worker
        thread (never the Tk thread). Emits a plain multipart body — no extra
        dependency beyond urllib.
        """
        boundary = "----kiyra-%x" % int(time.monotonic() * 1000)
        body = (
            f"--{boundary}\r\n"
            "Content-Disposition: form-data; name=\"file\"; filename=\"screenshot.png\"\r\n"
            f"Content-Type: {shot.mime}\r\n\r\n"
        ).encode("utf-8") + shot.image_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8")
        req = urllib.request.Request(
            f"{self.api_url}/api/vision/extract", data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=90) as resp:
            return json.loads(resp.read())

    def _screenshot_format_note(self, qi: QuestionInput) -> str:
        """Optional prompt note for code screenshots (empty for the rest).

        The question carries the extracted text; the note only shapes how the
        answer is formatted — short explanation, a fenced code block in the
        language the screenshot asks for, and a brief example/output."""
        if not qi.likely_code:
            return ""
        return (
            "The question came from an OCR'd screenshot of code. Answer with: "
            "(1) a short explanation, (2) a fenced code block (```language) "
            "containing correct code, (3) a brief example or expected output. "
            "Use the programming language the question asks for (Python, Java, "
            "C++, SQL, ...); if it is unspecified, prefer the candidate's "
            "established interview language and name it in one short line. "
            "If the OCR text is ambiguous or incomplete, say so briefly "
            "instead of inventing missing code."
        )

    def _on_screenshot_ocr_ready(self, payload: dict, shot: ScreenshotInput):
        """OCR done: show SOURCE: SCREENSHOT + DETECTED QUESTION, then send
        through the same pipeline as a manual question."""
        text = (payload.get("extracted_text") or "").strip()
        question = (payload.get("question") or "").strip()
        if not question:
            self._on_screenshot_error("No question text found in the screenshot")
            return
        confidence = payload.get("avg_confidence") or 0.0
        qi = QuestionInput(
            question=question,
            source="screenshot",
            raw_text=text,
            confidence=float(confidence),
            likely_code=bool(payload.get("likely_code", False)),
        )
        self._current_question = question
        self._latest_detected = question
        print(
            "[SCREENSHOT] question=%r confidence=%.2f likely_code=%s"
            % (question[:120], qi.confidence, qi.likely_code)
        )

        def show():
            self.txt_question.config(state=tk.NORMAL)
            self.txt_question.delete("1.0", tk.END)
            self.txt_question.insert("1.0", question)
            self.txt_question.config(state=tk.DISABLED)
            conf = f"• conf {qi.confidence:.0%}" if qi.confidence else ""
            self.lbl_q_meta.config(text=f"SOURCE: SCREENSHOT {conf}")
            self._switch_tab(False)

        self._safe_after(show)
        self._submit_question(
            question, note=self._screenshot_format_note(qi), source="screenshot")

    def _on_screenshot_error(self, message: str):
        """Capture/OCR failure: restore the overlay + button; surface the error
        on the inference label without touching an in-flight answer."""
        print(f"[SCREENSHOT] Error: {message}")
        def cb():
            if self._screenshot_busy:
                self._screenshot_busy = False
                try:
                    self.btn_screenshot.config(state=tk.NORMAL, text="📸 Screenshot")
                except Exception:
                    pass
            self.lbl_inference.config(text=f"⚠ Screenshot failed: {message[:48]}")
            try:
                if getattr(self, "_inf_clear_task", None):
                    self.root.after_cancel(self._inf_clear_task)
            except Exception:
                pass
            self._inf_clear_task = self.root.after(8000, lambda: self.lbl_inference.config(text=""))
        self._safe_after(cb)

    # ─────────────────────────────────────────────────────────────────────────
    # Structured Answer Rendering
    # ─────────────────────────────────────────────────────────────────────────

    # ─────────────────────────────────────────────────────────────────────────
    # Responsive Answer Layout (width follows the overlay, both directions)
    # ─────────────────────────────────────────────────────────────────────────

    def _on_answer_canvas_configure(self, event):
        """Keep the answer canvas window — and every answer widget inside it —
        at the canvas' current width so Text wrap always uses the CURRENT
        overlay width when narrowing or widening.

        A cached-width guard makes this idempotent: re-forcing the same width
        would re-emit <Configure> events forever (feedback loop).
        """
        try:
            width = max(1, int(event.width))
        except (TypeError, ValueError):
            return
        if width == self._answer_inner_width:
            return
        old = self._answer_inner_width
        self._answer_inner_width = width
        self.ans_canvas.itemconfigure(self.answer_win, width=width)
        print(f"[UI] Answer width changed old={old} new={width}")
        # Child widgets only get their new widths with the next event flush;
        # reflow after geometry settles so wrapped line counts are measured
        # at the new width (also runs for initial layout).
        try:
            self.root.after_idle(self._post_render_reflow)
        except (RuntimeError, tk.TclError):
            pass  # window torn down between resize and idle dispatch

    def _on_answer_inner_configure(self, _event=None):
        """Track content extents so the panel scrollbar reaches every line."""
        try:
            bbox = self.ans_canvas.bbox("all")
            if bbox:
                self.ans_canvas.configure(scrollregion=bbox)
        except Exception:
            pass

    def _post_render_reflow(self, _depth=0):
        """Re-fit section Text heights to their wrapped line count at the
        current width. Text widgets do not auto-grow: their height is in
        lines, so after a reflow the widget must be told the new line count
        or rewrapped lines would be clipped inside it and unreachable via
        the panel scrollbar.

        A pass is only final once every section Text actually sits at the
        forced width (child <Configure> events flush on a later event cycle
        than the one that changed the canvas) and no height changed since;
        retries go through root.after(1) so a real event cycle intervenes.
        Bounded chain — never schedules work after teardown."""
        try:
            self.root.update_idletasks()
        except Exception:
            pass
        want = self._answer_inner_width
        # A pass is final only when every section Text already sits at the
        # forced width (child configures flush a cycle after the canvas
        # change) and no height changed — then wrapped-line counts are real.
        settled = want <= 1  # before the first canvas Configure there is no target
        changed = False
        for frame in list(self._answer_widgets):
            try:
                if not frame.winfo_exists():
                    continue
            except Exception:
                continue
            for child in frame.winfo_children():
                if isinstance(child, tk.Text):
                    if want > 1 and abs(child.winfo_width() - want) > 4:
                        settled = False  # measured below would be stale
                    changed = self._resize_section_text(child) or changed
        if self._assistant_state == "generating" and self._answer_scroll_pinned:
            try:
                self.ans_canvas.yview_moveto(1.0)
            except Exception:
                pass
        if (changed or not settled) and _depth < 5:
            try:
                self.root.after(1, lambda: self._post_render_reflow(_depth + 1))
            except (RuntimeError, tk.TclError):
                pass

    def _resize_section_text(self, txt) -> bool:
        """Set one section Text's height to its actual display-line count.

        Returns True when the height changed (caller may re-check once after
        the next layout pass)."""
        try:
            n = txt.count("1.0", "end", "displaylines")
            if isinstance(n, tuple):
                n = n[0] if n else 1
            lines = max(2, int(n or 1))
            if int(txt.cget("height")) == lines:
                return False
            txt.config(height=lines)
            return True
        except Exception:
            return False

    def _render_section(self, title: str, body_txt: str, icon: str = "", accent=None,
                        code: bool = False, numbered: bool = False):
        """Render one titled answer section with optional code/numbered styling."""
        frame = tk.Frame(self.answer_inner, bg=self.card_bg)
        frame.pack(fill=tk.X, pady=(0, 6))
        head = tk.Frame(frame, bg=self.card_bg)
        head.pack(fill=tk.X, padx=10, pady=(8, 2))
        tk.Label(head, text=f"{icon} {title}".strip(), bg=self.card_bg,
                 fg=accent or self.text_primary, font=(self.font_family, 9, "bold")).pack(anchor="w")
        txt = tk.Text(
            frame, height=max(2, body_txt.count("\n") + 2),
            bg=self.code_bg if code else self.card_bg,
            fg="#d7f0e8" if code else self.text_primary,
            font=(self.mono_family, 9) if code else (self.font_family, 9),
            wrap=tk.WORD, relief=tk.FLAT, borderwidth=0,
            padx=10, pady=6, highlightthickness=0, spacing1=1, spacing3=1
        )
        if code:
            txt.config(fg="#d7f0e8", bg=self.code_bg,
                       font=(self.mono_family, 9))
        txt.pack(fill=tk.X)
        txt.insert("1.0", body_txt)
        txt.config(state=tk.DISABLED)
        if code:
            frame.config(highlightthickness=1, highlightbackground=self.border_color)
        self._answer_widgets.append(frame)
        if title == "Answer":
            # Remember the single Answer section so streaming updates it IN
            # PLACE (never destroyed/recreated per chunk).
            self._stable_answer_frame = frame
            self._stable_answer_txt = txt
        # Fit height to wrapped lines at the current width (placeholder guess
        # above), then re-measure once layout settles at the real width.
        self._resize_section_text(txt)
        try:
            self.root.after_idle(self._post_render_reflow)
        except (RuntimeError, tk.TclError):
            pass
        return frame

    def _render_answer(self, payload: dict, streaming: bool = False):
        """Finalize the answer area.

        The Answer Text used during streaming is updated IN PLACE with the
        final text (never destroyed); only the extra sections
        (Approach/Code/Key Points/Why/Follow-ups) are built once here."""
        answer = payload.get("answer", "")
        key_points = payload.get("key_points") or []
        star = payload.get("star") or {}
        follow = payload.get("follow_up_questions") or []
        confidence = payload.get("confidence")

        # Drop the previous answer's extra sections (never the stable Answer
        # frame) so repeated finalize calls cannot stack duplicates.
        for frame in list(self._answer_widgets):
            if frame is self._stable_answer_frame:
                continue
            try:
                frame.destroy()
            except Exception:
                pass
            try:
                self._answer_widgets.remove(frame)
            except ValueError:
                pass
        self._last_code = ""

        if self._assistant_state == "generating":
            self._stream_buffer = self._stream_buffer or ""

        txt = self._stable_answer_txt
        alive = False
        if txt is not None:
            try:
                alive = txt.winfo_exists()
            except Exception:
                alive = False
        if alive:
            txt.config(state=tk.NORMAL)
            txt.delete("1.0", "end")
            txt.insert("1.0", answer or (self._stream_buffer or "Generating…"))
            txt.config(state=tk.DISABLED)
            self._resize_section_text(txt)
        else:
            self._render_section("Answer", answer or (self._stream_buffer or "Generating…"),
                                 icon="◆", accent=self.accent_color)
        if star.get("Approach") or star.get("approach"):
            approach = star.get("Approach") or star.get("approach") or {}
            items = approach.get("steps") or approach.get("points") or []
            if isinstance(items, list) and items:
                n = "\n".join(f"{i}. {s}" for i, s in enumerate(items, 1))
                self._render_section("Approach", n, icon="▸", accent=self.success_color,
                                     numbered=True)

        # Explicit SQL/Code block support
        code_block = ""
        import re
        m = re.search(r"```(\w*)\n(.*?)```", answer, re.DOTALL)
        if m:
            code_block = m.group(2).strip("\n")
        if code_block:
            self._render_section("Code", code_block, icon="{}", accent=self.warning_color,
                                 code=True)

        if key_points:
            self._render_section("Key Points", "• " + "\n• ".join(key_points),
                                 icon="★", accent=self.warning_color)
        if star.get("Why") or star.get("why"):
            self._render_section("Why", str(star.get("Why") or star.get("why")),
                                 icon="★", accent=self.warning_color)
        if follow:
            self._render_section("Follow-ups", "• " + "\n• ".join(follow),
                                 icon="↪", accent=self.text_secondary)

        if confidence is not None:
            self.lbl_q_meta.config(text=f"confidence {confidence:.0%}")
        self._answer_scroll = self.ans_canvas.yview()[0] if self.ans_canvas.winfo_exists() else 0

    # ─────────────────────────────────────────────────────────────────────────
    # Hotkeys
    # ─────────────────────────────────────────────────────────────────────────

    def _bind_shortcuts(self):
        """Bind global hotkeys."""
        self.root.bind("<F9>", lambda e: self.toggle_visibility())
        self.root.bind("<F10>", lambda e: self.toggle_click_through())

    def toggle_visibility(self):
        """Toggle window visibility (F9)."""
        if self.root.winfo_viewable():
            self.root.withdraw()
            print("[Stealth] Hidden (F9 to show)")
        else:
            self.root.deiconify()
            self.root.attributes("-topmost", True)
            print("[Stealth] Visible")

    def toggle_click_through(self):
        """Toggle click-through mode (F10)."""
        self.is_click_through = not self.is_click_through
        status = "ON" if self.is_click_through else "OFF"
        self.lbl_capture.config(text=f"Click-through: {status} | F9: Toggle | F10: Click-through")
        print(f"[Stealth] Click-through: {status}")

    # ─────────────────────────────────────────────────────────────────────────
    # Device Management
    # ─────────────────────────────────────────────────────────────────────────

    def _populate_devices(self):
        """Populate device state. Loopback is discovered + validated through
        pyaudiowpatch (WASAPI system-audio capture) rather than by poking known
        driver names, so it works with headphones/speakers regardless of whether
        a 'Stereo Mix' device exists."""
        # Microphones via sounddevice.
        try:
            mics = enumerate_microphones()
            self._mic_list = [f"{d['index']}: {d['name']}" for d in mics]
        except Exception as e:
            print(f"[AUDIO_DEVICE] mic enumeration failed: {e}")
            self._mic_list = []
        # Default the mic selection to the first device (unset => mic off).
        self._mic_choice = self._mic_list[0] if self._mic_list else None
        print(f"[AUDIO_DEVICE] microphones={len(self._mic_list)}")

        # Loopback via pyaudiowpatch WASAPI loopback (single selectable source:
        # the default playback endpoint, headphones or speakers).
        self.loopback_device = None
        self._loop_name = None
        self._loop_choice = None
        try:
            info = find_wasapi_loopback_device()
        except Exception as e:
            print(f"[AUDIO_LOOPBACK] unavailable reason={e}")
            info = None
        if info is not None:
            print("[AUDIO_LOOPBACK] validating WASAPI loopback...")
            if validate_wasapi_loopback(info):
                self.loopback_device = info
                self._last_loop = info
                self._loop_name = f"WASAPI Loopback: {info['name']}"
                # Default the selection to the detected loopback, otherwise
                # start_capture() treats an unset choice as "disabled" and
                # silently captures the mic only (no interviewer audio).
                self._loop_choice = self._loop_name
                print(f"[AUDIO_LOOPBACK] operational ({info['samplerate']}Hz)")
            else:
                print("[AUDIO_CAPTURE] microphone_only_fallback (loopback unusable)")
        else:
            print("[AUDIO_CAPTURE] microphone_only_fallback (no loopback endpoint)")

        if not self._mic_list and info is None:
            print("[AUDIO_CAPTURE] no_audio_sources_available")
        elif not self._mic_list:
            print("[AUDIO_CAPTURE] loopback_only")
        elif info is None:
            print("[AUDIO_CAPTURE] microphone_only")

    # ─────────────────────────────────────────────────────────────────────────
    # Capture Lifecycle
    # ─────────────────────────────────────────────────────────────────────────

    def start_capture(self):
        """Start audio capture and WebSocket connection."""
        if sd is None or np is None or websockets is None:
            messagebox.showerror(
                "Missing Dependencies",
                "Install: numpy, sounddevice, websockets"
            )
            return

        try:
            mic_sel = getattr(self, "_mic_choice", None)
            self.mic_device = int(mic_sel.split(":", 1)[0]) if mic_sel else None
            if not (getattr(self, "_loop_choice", None)):
                self.loopback_device = None
        except Exception:
            self.mic_device = getattr(self, "mic_device", None)

        if self._capture_thread and self._capture_thread.is_alive():
            return

        self._stop_event.clear()
        self._capture_thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._capture_thread.start()

        if not (self._ws_thread and self._ws_thread.is_alive()):
            self._ws_thread = threading.Thread(target=self._ws_loop_thread, daemon=True)
            self._ws_thread.start()

        self.is_capturing = True
        self.lbl_capture.config(text=f"▶ Capturing ({self.chunk_seconds}s chunks)")
        self.btn_start.config(state=tk.DISABLED)
        self.btn_stop.config(state=tk.NORMAL)
        print("[Capture] Started")

    def stop_capture(self):
        """Stop audio capture."""
        self._stop_event.set()
        self.is_capturing = False
        self.lbl_capture.config(text="⏹ Stopped")
        self.btn_start.config(state=tk.NORMAL)
        self.btn_stop.config(state=tk.DISABLED)
        print("[Capture] Stopped")

    def _push_audio_chunk(self, wav_bytes: bytes):
        """Called from either capture source's audio callback. Never blocks:
        bounded queue, drop-oldest on overflow, rate-limited counters."""
        dropped = bounded_put_drop_oldest(self._audio_queue, wav_bytes)
        if dropped:
            self._ws_audio_dropped += 1
        self._ws_audio_count += 1
        if self._ws_audio_count == 1 or self._ws_audio_count % 48 == 0:
            print(
                f"[WS_AUDIO] sent frame={self._ws_audio_count} "
                f"bytes={len(wav_bytes)} dropped={self._ws_audio_dropped}"
            )

    def _capture_loop(self):
        """Orchestrate LoopbackCapture + MicrophoneCapture through a shared
        mixing sink.

        Both sources emit continuous 16 kHz float32 streams; the
        ``_MixingChunkSink`` pairs their 250 ms buffers and averages the
        samples, pushing ONE mixed 250ms/16k/mono PCM16 WAV stream into the
        bounded ``_audio_queue`` (drop-oldest). Without the mixer the two
        sources would interleave loud speech with near-silence every 250 ms
        and Whisper would return empty transcripts. A failure in either
        source never kills the other (single-source degrades cleanly)."""
        captures = []
        # Source 0 = mic, gated below the backend VAD start threshold (0.015):
        # room noise must not keep utterances open (12s caps / fragmentation).
        mixer = _MixingChunkSink(self._push_audio_chunk, n_sources=2,
                                 source_gates={0: 0.015},
                                 source_names=("mic", "loopback"))

        if self.mic_device is not None:
            mic = MicrophoneCapture(
                device_index=self.mic_device, on_chunk=self._push_audio_chunk,
                sink=mixer.for_source(0),
            )
            if mic.start():
                captures.append(mic)
            else:
                print("[AUDIO_CAPTURE] microphone unavailable")
                self.mic_device = None

        if self.loopback_device is not None:
            loop = LoopbackCapture(
                on_chunk=self._push_audio_chunk, device_info=self.loopback_device,
                sink=mixer.for_source(1),
            )
            if loop.start():
                captures.append(loop)
            else:
                print("[AUDIO_CAPTURE] microphone_only_fallback")
                self.loopback_device = None

        if not captures:
            print("[AUDIO_CAPTURE] no_audio_sources_available")
            try:
                self.root.after(0, lambda: self._ui_set_status(
                    "No audio source available", ok=False))
            except Exception:
                pass
            return

        print(f"[AUDIO_QUEUE] sender queue max={self._audio_queue.maxsize} frames")
        try:
            while not self._stop_event.is_set():
                time.sleep(0.5)
        except Exception as e:
            print(f"[Capture] Error: {e}")
        finally:
            for c in captures:
                try:
                    c.stop()
                except Exception:
                    pass
            print("[Capture] Streams closed")

    # ─────────────────────────────────────────────────────────────────────────
    # WebSocket
    # ─────────────────────────────────────────────────────────────────────────

    def _ws_loop_thread(self):
        """Run WebSocket event loop in thread."""
        if asyncio is None or websockets is None:
            self._ui_set_status("WebSocket unavailable", ok=False)
            return

        self._ws_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._ws_loop)
        try:
            self._ws_loop.run_until_complete(self._ws_main())
        except RuntimeError:
            pass  # loop force-stopped during shutdown
        finally:
            # Drain pending tasks (sender/receiver/keepalive) so shutdown does
            # not print "Task was destroyed but it is pending" or let a
            # cancelled _ws_main resume into "no running event loop".
            try:
                pending = [t for t in asyncio.all_tasks(self._ws_loop)
                           if not t.done()]
                for t in pending:
                    t.cancel()
                if pending:
                    self._ws_loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass
            try:
                self._ws_loop.close()
            except Exception:
                pass

    async def _ws_main(self):
        """Main WebSocket loop with reconnect."""
        url = self.api_url.replace("http://", "ws://").replace("https://", "wss://")
        url += f"/api/interviews/{self.session_id}/ws"

        backoff = 1.0
        while not self._stop_event.is_set():
            try:
                async with websockets.connect(url, max_size=None) as ws:
                    self.websocket = ws
                    self.ws_connected = True
                    self._ui_set_status("Connected ✓", ok=True)
                    backoff = 1.0

                    sender = asyncio.create_task(self._ws_sender())
                    receiver = asyncio.create_task(self._ws_receiver())
                    done, pending = await asyncio.wait(
                        [sender, receiver],
                        return_when=asyncio.FIRST_EXCEPTION
                    )
                    for t in pending:
                        t.cancel()
            except Exception as e:
                self.ws_connected = False
                if self._stop_event.is_set():
                    return  # shutting down — don't sleep on a stopping loop
                self._ui_set_status(f"Disconnected ({backoff:.0f}s)", ok=False)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
            finally:
                self.ws_connected = False

    async def _ws_sender(self):
        """Send audio frames as binary."""
        while not self._stop_event.is_set() and self.ws_connected:
            try:
                # run_in_executor only takes positional args — use partial for timeout
                wav_bytes = await self._ws_loop.run_in_executor(
                    None,
                    functools.partial(self._audio_queue.get, timeout=1)
                )
                if wav_bytes:
                    await self.websocket.send(wav_bytes)
            except queue.Empty:
                continue  # no audio yet, loop
            except Exception as e:
                print(f"[WS] Sender error: {e}")
                break

    async def _ws_receiver(self):
        """Receive and handle events."""
        while not self._stop_event.is_set() and self.ws_connected:
            try:
                msg = await self.websocket.recv()
                if isinstance(msg, bytes):
                    continue

                data = json.loads(msg)
                ev_type = data.get("type")
                payload = data.get("payload", {})

                if ev_type == "ping":
                    await self.websocket.send(json.dumps({"type": "pong"}))
                elif ev_type == "question.detected":
                    q = payload.get("question")
                    self._ui_update_question(q)
                elif ev_type == "answer.generating":
                    self._active_request_id = payload.get("request_id")
                    self._perf_started = time.monotonic()
                    self._perf_first_chunk_recv = None
                    self._perf_first_chunk_rendered = None
                    self._perf_chunks = 0
                    q = payload.get("question") or ""
                    if q:
                        self._active_gen_question = re.sub(
                            r"\s+", " ", q.strip().lower())
                    self._ui_stream_answer("", reset=True)
                elif ev_type == "answer.streamed":
                    rid = payload.get("request_id")
                    if self._is_stale_request(rid):
                        print(f"[UI] Answer chunk skipped (stale request {rid})")
                    else:
                        delta = payload.get("delta", "")
                        if delta:
                            if self._perf_first_chunk_recv is None:
                                self._perf_first_chunk_recv = time.monotonic()
                            self._perf_chunks += 1
                        self._ui_stream_answer(delta, append=True)
                elif ev_type == "answer.generated":
                    rid = payload.get("request_id")
                    if self._is_stale_request(rid):
                        print(f"[UI] Answer completion skipped (stale request {rid})")
                    else:
                        self._ui_update_answer(payload)
                elif ev_type == "answer.cancelled":
                    self._ui_cancel_answer(payload)
                elif ev_type == "session.connected":
                    self._ui_set_status("Session connected", ok=True)
                elif ev_type == "session.error":
                    msg_text = payload.get("message", "Unknown error")
                    print(f"[WS] Error: {msg_text}")
            except Exception as e:
                print(f"[WS] Receiver error: {e}")
                break

    # ─────────────────────────────────────────────────────────────────────────
    # UI Updates
    # ─────────────────────────────────────────────────────────────────────────

    def _safe_after(self, fn):
        """Schedule ``fn`` on the Tk main thread without crashing the caller.

        Called from background threads (WebSocket/Audio). ``root.after`` raises
        ``RuntimeError: main thread is not in main loop`` if the Tcl event loop
        has not started yet or is blocked; swallow that so a background thread
        never takes the whole overlay down (events are then rendered on the
        next event iteration when the loop is live).
        """
        try:
            if self.root.winfo_exists():
                self.root.after(0, fn)
        except Exception:
            pass

    def _is_stale_request(self, rid) -> bool:
        """A stream/cancel/complete of an old request must never touch the
        answer currently streaming for a newer request."""
        return bool(rid and self._active_request_id and rid != self._active_request_id)

    def _ui_set_status(self, text: str, ok: bool = True):
        """Update status label."""
        def cb():
            color = self.success_color if ok else self.danger_color
            if ok:
                self.lbl_status.config(text=f"● {text}", fg=color)
                self._assistant_state = "ready"
            else:
                self.lbl_status.config(text=f"● {text}", fg=color)
                self._assistant_state = "offline"

        self._safe_after(cb)

    def _ui_update_question(self, q: str):
        """Display detected question."""
        self._current_question = q or ""
        if self._current_question:
            self._latest_detected = self._current_question

        def cb():
            self.txt_question.config(state=tk.NORMAL)
            self.txt_question.delete("1.0", tk.END)
            self.txt_question.insert("1.0", self._current_question or "-")
            self.txt_question.config(state=tk.DISABLED)
            import time as _t
            self.lbl_q_meta.config(text=f"detected {_t.strftime('%H:%M:%S')}")

        self._safe_after(cb)

    def _cancel_answer_auto_hide(self):
        """Cancel any pending answer auto-hide timer (idempotent, Tk thread)."""
        if self.answer_auto_hide_task is not None:
            try:
                self.root.after_cancel(self.answer_auto_hide_task)
            except Exception:
                pass
            self.answer_auto_hide_task = None

    def _ui_stream_answer(self, delta: str, reset: bool = False, append: bool = False):
        """Stream answer tokens into the STABLE answer Text.

        The Answer container is created once per generation and updated in
        place: text inserts are immediate, geometry work is batched once per
        idle pass — the container is never destroyed/recreated per chunk."""
        def cb():
            if reset:
                # A new answer is beginning: any pending auto-hide from a
                # previous answer must die NOW — it must never clear a
                # mid-stream answer.
                self._cancel_answer_auto_hide()
                print("[UI] Answer generation started")
                self._assistant_state = "generating"
                self._stream_buffer = ""
                self._perf_first_chunk_rendered = None
                self._begin_generation()
            elif append and delta:
                first = not self._stream_buffer
                # Bottom state is measured BEFORE the insert: after the text
                # grows the same offset no longer reads as "at bottom".
                try:
                    self._answer_scroll_pinned = self.ans_canvas.yview()[1] >= 0.999
                except Exception:
                    self._answer_scroll_pinned = True
                self._stream_buffer += delta
                txt = self._stable_answer_txt
                if txt is not None:
                    try:
                        if txt.winfo_exists():
                            if first:
                                # Replace the "Generating…" placeholder IN
                                # PLACE — no parent/container recreation.
                                txt.config(state=tk.NORMAL)
                                txt.delete("1.0", "end")
                                self._perf_first_chunk_rendered = time.monotonic()
                                print("[UI] First chunk rendered")
                            txt.insert("end", delta)
                    except Exception:
                        pass
                self._schedule_answer_reflow()
                print(f"[UI] Answer chunk received chars={len(self._stream_buffer)}")
            self._paint_dot(True)

        self._safe_after(cb)

    def _begin_generation(self):
        """Prepare the stable answer container for a new generation.

        The Answer section (frame + Text) is created ONCE and reused across
        the whole generation; only the previous answer's extra sections
        (Approach/Code/Key Points/…) are destroyed here. The placeholder
        "Generating…" is set in place — the container itself is never
        rebuilt, so the panel geometry stays stable."""
        self._cancel_answer_auto_hide()
        for frame in list(self._answer_widgets):
            if frame is self._stable_answer_frame:
                continue
            try:
                frame.destroy()
            except Exception:
                pass
            try:
                self._answer_widgets.remove(frame)
            except ValueError:
                pass
        self._last_code = ""
        txt = self._stable_answer_txt
        alive = False
        if txt is not None:
            try:
                alive = txt.winfo_exists()
            except Exception:
                alive = False
        if alive:
            txt.config(state=tk.NORMAL)
            txt.delete("1.0", "end")
            txt.insert("1.0", "Generating…")
        else:
            # Stable container gone (cancel/auto-hide/teardown): create once.
            self._render_section("Answer", "Generating…", icon="◆",
                                 accent=self.accent_color)
        self._answer_scroll_pinned = True
        self._schedule_answer_reflow()

    def _schedule_answer_reflow(self):
        """Batch geometry work: text inserts are immediate; height re-fit,
        scrollregion and auto-scroll run ONCE per idle pass (a pending pass
        is never duplicated) — tokens cannot create a reflow loop."""
        if self._answer_reflow_pending:
            return
        self._answer_reflow_pending = True
        try:
            self.root.after_idle(self._flush_answer_reflow)
        except (RuntimeError, tk.TclError):
            self._answer_reflow_pending = False  # window torn down

    def _flush_answer_reflow(self):
        """One batched layout pass for the streaming answer (never per token)."""
        self._answer_reflow_pending = False
        t0 = time.monotonic()
        txt = self._stable_answer_txt
        if txt is not None:
            try:
                if txt.winfo_exists():
                    self._resize_section_text(txt)
            except Exception:
                pass
        try:
            bbox = self.ans_canvas.bbox("all")
            if bbox:
                self.ans_canvas.configure(scrollregion=bbox)
        except Exception:
            pass
        # Auto-scroll: follow the stream only while the user sat at the
        # bottom before this batch; a manual scroll upward is respected.
        try:
            if self._answer_scroll_pinned and self._assistant_state == "generating":
                self.ans_canvas.yview_moveto(1.0)
        except Exception:
            pass
        ms = (time.monotonic() - t0) * 1000.0
        print(f"[UI] render flush {ms:.2f}ms")

    def _clear_answer_area(self):
        """Full clear (cancel/auto-hide): destroy every section incl. the
        stable Answer container so the next generation starts fresh."""
        for w in self._answer_widgets:
            try:
                w.destroy()
            except Exception:
                pass
        self._answer_widgets = []
        self._stable_answer_frame = None
        self._stable_answer_txt = None

    def _paint_dot(self, active: bool):
        """Tiny '●' pulse while streaming (assistant is working)."""
        if active and self._assistant_state in ("generating", "ready"):
            try:
                self.status_dot.config(fg=self.warning_color)
            except Exception:
                pass

    def _ui_cancel_answer(self, payload: dict):
        """Clear the answer area when a generation was superseded.

        A cancel belonging to an OLDER request must never wipe the answer
        currently streaming for a newer request — only the active request
        (or an untagged cancel) clears the UI.
        """
        rid = payload.get("request_id")
        if self._is_stale_request(rid):
            print(f"[UI] Stale answer cancel ignored (request {rid})")
            return

        def cb():
            self._assistant_state = "ready"
            self._active_gen_question = None
            self._clear_answer_area()
            self._stream_buffer = ""
            self._cancel_answer_auto_hide()
            print(f"[WS] Answer cancelled: {payload.get('reason', 'unknown')}")

        self._safe_after(cb)

    def _ui_update_answer(self, payload: dict):
        """Display the FINAL answer. The auto-hide countdown starts ONLY here,
        after answer.generated — never while an answer is generating."""
        def cb():
            self._assistant_state = "ready"
            self._stream_buffer = payload.get("answer", "") or ""
            self._render_answer(payload)
            print(f"[UI] Answer generation completed chars={len(self._stream_buffer)}")
            self._cancel_answer_auto_hide()
            delay_ms = self.answer_hide_delay_ms
            self.answer_auto_hide_task = self.root.after(
                delay_ms, self._auto_hide_answer)
            print(f"[UI] Answer auto-hide scheduled delay={delay_ms // 1000}s")
            self._print_ui_perf()

        self._safe_after(cb)

    def _print_ui_perf(self):
        """UI-side latency metrics: distinguish a slow model from slow render.

        ws_first_chunk_delivery   = backend emit -> WS recv
        ui_first_render           = WS recv -> first token painted on screen
        model_first_token_to_screen = backend emit -> first token painted"""
        try:
            if self._perf_started is None:
                return
            now = time.monotonic()
            total = (now - self._perf_started) * 1000.0
            recv = self._perf_first_chunk_recv or self._perf_started
            ws = (recv - self._perf_started) * 1000.0
            rendered = self._perf_first_chunk_rendered
            ui_render = ((rendered - recv) * 1000.0
                         if rendered and self._perf_first_chunk_recv else 0.0)
            to_screen = ((rendered or recv) - self._perf_started) * 1000.0
            print(
                "[UI-PERF] answer: ws_first_chunk_delivery=%.1fms "
                "ui_first_render=%.1fms model_first_token_to_screen=%.1fms "
                "chunks=%d total=%.1fms"
                % (ws, ui_render, to_screen, self._perf_chunks, total))
        except Exception:
            pass

    def _auto_hide_answer(self):
        """Auto-hide a COMPLETED answer after its delay.

        If a new answer started while the timer was pending the timer was
        cancelled at generation start; this guard is the safety net: never
        hide (or claim to hide) an answer that is still generating.
        """
        self.answer_auto_hide_task = None
        if self._assistant_state == "generating":
            print("[UI] Answer auto-hide skipped (generation in progress)")
            return
        self._clear_answer_area()
        print("[UI] Answer auto-hidden")

    # ─────────────────────────────────────────────────────────────────────────
    # Additional Assistant Interactions (pin / settings / manual ask / refresh)
    # ─────────────────────────────────────────────────────────────────────────

    def toggle_pin(self):
        """Toggle window always-on-top."""
        self._is_pinned = not self._is_pinned
        self.root.attributes("-topmost", self._is_pinned)
        self.btn_pin.config(text="📌" if self._is_pinned else "📍")
        print(f"[UI] Pin: {'ON' if self._is_pinned else 'OFF'}")

    def open_settings(self):
        """Settings entry — device pickers + capture controls (compact)."""
        win = tk.Toplevel(self.root)
        win.title("Kiyra — Settings")
        win.geometry("460x320+120+140")
        win.configure(bg=self.bg_color)
        win.transient(self.root)
        win.grab_set()

        tk.Label(win, text="Audio Devices", bg=self.bg_color, fg=self.text_primary,
                 font=(self.font_family, 10, "bold")).pack(anchor="w", padx=12, pady=(12, 6))

        mic_list = getattr(self, "_mic_list", []) or ["<no microphones found>"]
        loop_vals = ([self._loop_name] if getattr(self, "_loop_name", None) else ["<unavailable>"])

        frm = tk.Frame(win, bg=self.bg_color)
        frm.pack(fill=tk.X, padx=12)
        tk.Label(frm, text="Microphone:", bg=self.bg_color, fg=self.text_secondary,
                 font=(self.font_family, 9)).grid(row=0, column=0, sticky="w", pady=4)
        cmb_mic = ttk.Combobox(frm, values=mic_list, state="readonly", width=48)
        cmb_mic.set(getattr(self, "_mic_choice", "") or (mic_list[0] if mic_list else ""))
        cmb_mic.grid(row=0, column=1, padx=8, sticky="ew", pady=4)
        tk.Label(frm, text="System Audio:", bg=self.bg_color, fg=self.text_secondary,
                 font=(self.font_family, 9)).grid(row=1, column=0, sticky="w", pady=4)
        cmb_loop = ttk.Combobox(frm, values=loop_vals, state="readonly", width=48)
        cmb_loop.set(getattr(self, "_loop_choice", "") or loop_vals[0])
        cmb_loop.grid(row=1, column=1, padx=8, sticky="ew", pady=4)
        frm.columnconfigure(1, weight=1)

        def apply():
            try:
                sel = cmb_mic.get()
                self._mic_choice = sel if "no " not in sel else None
                self.mic_device = int(sel.split(":", 1)[0]) if sel and "no " not in sel else None
            except Exception:
                self._mic_choice = None
                self.mic_device = None
            try:
                sel = cmb_loop.get()
                self._loop_choice = sel if "unavailable" not in sel else None
                if self._loop_choice:
                    self.loopback_device = self.loopback_device or getattr(self, "_last_loop", None)
            except Exception:
                self._loop_choice = None
            win.destroy()

        btn_box = tk.Frame(win, bg=self.bg_color)
        btn_box.pack(fill=tk.X, padx=12, pady=(10, 12))
        self._btn(btn_box, "Apply", apply, bg=self.accent_color, fg="#0b0d12",
                  font_size=9, padx=12).pack(side=tk.RIGHT)
        self._btn(btn_box, "Cancel", win.destroy, bg=self.card_bg, font_size=9,
                  padx=12).pack(side=tk.RIGHT, padx=(0, 6))

    def send_manual_question(self):
        """Send the manual 'Ask anything' question to the suggest-answer endpoint.

        Duplicate-submission protection: sending the exact same normalized
        question while that question is STILL generating is ignored (it would
        only cancel + restart the identical generation). Refresh Answer below
        intentionally bypasses this guard."""
        q = self.entry_manual.get().strip()
        if not q or q == "Ask anything…":
            return
        nq = re.sub(r"\s+", " ", q.lower())
        if (self._assistant_state == "generating"
                and nq == (self._active_gen_question or "")):
            print("[UI] Duplicate manual question ignored (generation in progress)")
            return
        self._manual_mode = True
        self._submit_question(q)

    def _submit_question(self, q: str, note: str = "", source: str = "manual"):
        """Shared generation start for manual + screenshot questions.

        Both enter the SAME downstream pipeline: WebSocket ``generate_answer``
        → memory resolve → RAG → ContextBuilder → Bonsai → streamed answer.
        """
        self._active_gen_question = re.sub(r"\s+", " ", q.lower())
        self._assistant_state = "generating"
        self._stream_buffer = ""
        self._perf_started = time.monotonic()
        self._perf_first_chunk_recv = None
        self._perf_first_chunk_rendered = None
        self._perf_chunks = 0
        self._begin_generation()
        try:
            self._request_manual_answer(q, note=note, source=source)
        except Exception as e:
            print(f"[UI] Submit error: {e}")

    def _send_ws_message(self, msg: dict) -> bool:
        """Thread-safe send on the primary WebSocket. Returns True if queued."""
        if not self.websocket or not self._ws_loop or not self.ws_connected:
            return False
        try:
            coro = self.websocket.send(json.dumps(msg))
            asyncio.run_coroutine_threadsafe(coro, self._ws_loop)
            return True
        except Exception:
            return False

    def _request_manual_answer(self, q: str, note: str = "", source: str = "manual"):
        """Manual/screenshot ask: prefer the live primary WS (single connection
        -> single LiveSession, so the answer flows through the same receiver
        that renders detected answers); fall back to REST suggest-answer."""
        def worker():
            try:
                if self._send_ws_message({
                        "type": "generate_answer",
                        "payload": {"question": q, "source": source, "note": note}}):
                    print(f"[UI] Manual question sent via WS: {q[:60]}")
                    return
                self._req_manual_rest(q)
            except Exception as e:
                def cb():
                    self._render_section("Answer", f"Request failed: {e}", icon="◆",
                                         accent=self.danger_color)
                self._safe_after(cb)

        threading.Thread(target=worker, daemon=True).start()

    def _req_manual_rest(self, q: str):
        """Manual ask fallback: POST /api/interviews/suggest-answer."""
        import urllib.request as _ur
        payload = json.dumps({"question": q, "question_type": "unknown",
                              "session_id": self.session_id}).encode()
        req = _ur.Request(
            f"{self.api_url}/api/interviews/suggest-answer", data=payload,
            headers={"Content-Type": "application/json"}, method="POST"
        )
        with _ur.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read())
        if data.get("answer"):
            self._safe_after(lambda: self._ui_update_answer(data))

    def generate_latest_answer(self):
        """Refresh the answer for the latest detected question (manual refresh).

        Refresh is the EXPLICIT regenerate action: it intentionally cancels
        the current request and starts a new generation (no duplicate guard).
        It does set the active question so plain duplicate SENDs while this
        refresh generates are ignored."""
        q = self._latest_detected or self._current_question
        if not q or q in ("Listening for the interviewer's question…",):
            return
        self._submit_question(q)

    # ─────────────────────────────────────────────────────────────────────────
    # Cleanup
    # ─────────────────────────────────────────────────────────────────────────

    def _on_close(self):
        """Clean shutdown."""
        self.stop_capture()
        self._stop_event.set()
        time.sleep(0.2)

        try:
            if self._ws_loop and self._ws_loop.is_running():
                self._ws_loop.call_soon_threadsafe(self._ws_loop.stop)
        except Exception:
            pass

        self.root.destroy()
        print("[App] Closed")


# ─────────────────────────────────────────────────────────────────────────────
# Entry Point
# ─────────────────────────────────────────────────────────────────────────────

def _create_live_session(api_url: str) -> int:
    """Create a live interview session via REST and return its id."""
    url = f"{api_url}/api/interviews"
    payload = json.dumps({
        "interview_type": "behavioral",
        "mode": "live",
        "difficulty": "medium",
        "question_count": 10,
        "title": "Live Stealth Session",
    }).encode()
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        data = json.loads(resp.read())
    return data["id"]


def main():
    api_url = "http://localhost:8000"
    try:
        session_id = _create_live_session(api_url)
        print(f"[Session] Created session {session_id}")
    except Exception as e:
        print(f"[Session] Could not auto-create session: {e} — defaulting to id=1")
        session_id = 1

    root = tk.Tk()
    app = StealthOverlayApp(root, api_url=api_url, session_id=session_id)
    root.mainloop()


if __name__ == "__main__":
    main()
