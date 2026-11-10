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


# ─────────────────────────────────────────────────────────────────────────────
# Win32 window-shell constants (named — no magic numbers)
# ─────────────────────────────────────────────────────────────────────────────
# The taskbar button and the Alt+Tab entry are driven by the TOP-LEVEL window's
# extended styles, not Tk's. A Tk toplevel is created as an ordinary app window,
# so Windows gives it a taskbar button unless we make it a TOOL window.
GWL_EXSTYLE = -20                  # Get/SetWindowLongPtr: extended style index
WS_EX_TOPMINDOW = 0x00000008
WS_EX_TRANSPARENT = 0x00000020    # click-through (left to the existing toggle)
WS_EX_TOOLWINDOW = 0x00000080     # tool window: no taskbar button, no Alt+Tab
WS_EX_LAYERED = 0x00080000        # set by Tk for -alpha; must be preserved
WS_EX_APPWINDOW = 0x00040000      # force a taskbar button — must be CLEARED

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020

HWND_TOPMOST = -1                 # (HWND)-1
GA_ROOT = 2                       # GetAncestor: top-level (shell) window

# LONG_PTR must be POINTER-SIZED: ctypes.wintypes.LONG_PTR does not exist in
# CPython's wintypes, and c_long is only 4 bytes on Win64 — using either would
# be the unsafe 32-bit assumption. c_ssize_t is 8 bytes here, 4 on Win32.
LONG_PTR = ctypes.c_ssize_t

# Reference-recreation design size in LOGICAL px (scaled by DPI at runtime).
# The overlay is a landscape floating bar: toolbar + transcription row + manual
# row + expandable Features table. Shared with the geometry tests.
DESIGN_WIDTH = 960
DESIGN_HEIGHT = 760

# Placeholder shown in the manual "Ask" field when empty (reference wording).
ASK_PLACEHOLDER = "Ask any question manually …"

# Placeholder shown in the live-transcript field (bar 2) while nothing has been
# heard yet. Matches the reference screenshot's wording.
LIVE_PLACEHOLDER = "Speech to Text of System Audio…"

# Every font in the overlay is bumped by this many points so the answers are
# easier to read at a glance. Centralised so a single change re-sizes the whole
# UI (buttons, labels, transcript and answer sections).
FONT_BUMP = 2

# Audio-source combobox width (in Tk chars) per panel size preset. Fixed-size
# widgets (like this combobox) are what make a shrunken panel clip; keeping the
# preset -> chars map here lets set_panel_size re-fit instead of letting the
# requested width go stale when the window gets smaller.
AUDIO_COMBO_CHARS = {"compact": 16, "normal": 26, "large": 34}


def _fs(size) -> int:
    """Apply the global +2pt overlay font bump to an integer point size."""
    return int(size) + FONT_BUMP


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

# ─────────────────────────────────────────────────────────────────────────────
# 64-bit-safe extended-style access
# ─────────────────────────────────────────────────────────────────────────────
_STYLE_API = None


def _window_style_api():
    """Bind Get/SetWindowLongPtrW once, with explicit argtypes/restype.

    ctypes defaults restype to c_int (32-bit); on Win64 the LongPtr variants
    return a 64-bit LONG_PTR, so the signatures are declared explicitly rather
    than inherited. Falls back to the non-Ptr (32-bit) entry points if the
    LongPtr pair is unavailable.
    """
    global _STYLE_API
    if _STYLE_API is None:
        user32 = ctypes.windll.user32
        get_fn = getattr(user32, "GetWindowLongPtrW", None)
        set_fn = getattr(user32, "SetWindowLongPtrW", None)
        if get_fn is None or set_fn is None:
            get_fn, set_fn = user32.GetWindowLongW, user32.SetWindowLongW
        get_fn.restype = LONG_PTR
        get_fn.argtypes = [ctypes.wintypes.HWND, ctypes.c_int]
        set_fn.restype = LONG_PTR
        set_fn.argtypes = [ctypes.wintypes.HWND, ctypes.c_int, LONG_PTR]
        _STYLE_API = (get_fn, set_fn)
    return _STYLE_API


def get_window_exstyle(hwnd):
    """Read GWL_EXSTYLE as a pointer-sized integer."""
    if not hwnd:
        return 0
    get_fn, _ = _window_style_api()
    return int(get_fn(ctypes.wintypes.HWND(hwnd), GWL_EXSTYLE))


def set_window_exstyle(hwnd, exstyle):
    """Write GWL_EXSTYLE (pointer-sized) and return the previous value."""
    if not hwnd:
        return 0
    _, set_fn = _window_style_api()
    return int(set_fn(ctypes.wintypes.HWND(hwnd), GWL_EXSTYLE,
                      LONG_PTR(int(exstyle))))


def stealth_exstyle(current):
    """TOOLWINDOW on, APPWINDOW off — every other bit preserved.

    Pure bit math (unit-testable): WS_EX_LAYERED (Tk's -alpha),
    WS_EX_TRANSPARENT (click-through) and WS_EX_TOPMOST survive untouched, so
    this can never break alpha, topmost or the F10 toggle.
    """
    return (int(current) | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW


def toplevel_hwnd(client_hwnd):
    """The shell (top-level) window for a Tk client hwnd.

    Tk's ``winfo_id()`` is the CLIENT window; the taskbar and Alt+Tab are
    driven by its top-level ancestor. GA_ROOT is the real shell window, so it
    is preferred; GetParent is the Tk wrapper fallback.
    """
    if not client_hwnd:
        return 0
    user32 = ctypes.windll.user32
    root = int(user32.GetAncestor(client_hwnd, GA_ROOT) or 0)
    if root:
        return root
    return int(user32.GetParent(client_hwnd) or 0) or int(client_hwnd)


def read_window_shell_flags(hwnd):
    """Programmatic taskbar/Alt+Tab verdict for a window handle."""
    if not hwnd:
        return {"hwnd": 0, "valid": False, "exstyle": 0, "toolwindow": False,
                "appwindow": False, "topmost": False, "layered": False,
                "transparent": False, "taskbar_button": False,
                "alt_tab_entry": False}
    valid = bool(ctypes.windll.user32.IsWindow(ctypes.wintypes.HWND(hwnd)))
    ex = get_window_exstyle(hwnd) if valid else 0
    tool = bool(ex & WS_EX_TOOLWINDOW)
    app = bool(ex & WS_EX_APPWINDOW)
    # Windows shows a taskbar button / Alt+Tab entry for an APPWINDOW, and for a
    # plain top-level window that is NOT a tool window.
    return {
        "hwnd": int(hwnd), "valid": valid, "exstyle": ex,
        "toolwindow": tool, "appwindow": app,
        "topmost": bool(ex & WS_EX_TOPMINDOW),
        "layered": bool(ex & WS_EX_LAYERED),
        "transparent": bool(ex & WS_EX_TRANSPARENT),
        "taskbar_button": app or not tool,
        "alt_tab_entry": app or not tool,
    }

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

from backend_server import BackendLauncher
from llama_server import ModelServerLauncher


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
    def __init__(self, root: tk.Tk, api_url: str = "http://localhost:8000",
                 session_id: int = 1,
                 backend_launcher: "BackendLauncher | None" = None,
                 model_launcher: "ModelServerLauncher | None" = None,
                 listening_mode: bool = True,
                 auto_start_listening: bool = True):
        self.root = root
        self.api_url = api_url.rstrip("/")
        self.session_id = session_id
        # Interview Listening Mode switches (Phase 5.4).
        self._want_listening_mode = bool(listening_mode)
        self._want_auto_start = bool(auto_start_listening)
        # Only ever a backend THIS process started (see main()); None when the
        # backend was already running, so shutdown never kills a foreign one.
        self.backend_launcher = backend_launcher
        # Same contract for the model server this process started.
        self.model_launcher = model_launcher

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
        # Previous-answer navigation: the last live answer payload (so returning
        # to the latest turn can restore it) and whether the answer box is
        # currently showing a historical entry.
        self._live_answer_payload = None
        self._viewing_history = False
        self._rendering_history = False
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
        # Window-shell (taskbar / Alt+Tab) styling state.
        self._shell_styles_reported = False
        self._shell_flags = None
        self._ui_scale = 1.0
        # Screenshot input mode: true while capture/OCR is in flight so
        # repeated clicks are ignored and the overlay stays responsive.
        self._screenshot_busy = False
        self._inf_clear_task = None
        # ── Desktop chrome (Phase 5.5): timer, device selector, features ────
        # panel, collapse-to-circle, zoom, window move, transparency.
        self._timer_running = False
        self._timer_after_id = None
        self._timer_started_at = 0.0
        self._features_open = True
        self._features_panel = None
        self._recording = False
        self._large_panel = False
        self._size_preset = "normal"
        self._size_btns = {}
        self._mini_win = None
        self._mini_pos = None
        self._mini_drag_origin = None
        self._mini_drag_moved = False
        self._loopback_active = False

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
        # Reference recreation: ~960x760 landscape overlay (16:9 desktop),
        # horizontally centered.
        try:
            dpi = ctypes.windll.user32.GetDpiForSystem() or 96
        except Exception:
            dpi = 96
        self._ui_scale = dpi / 96.0
        w, h = int(DESIGN_WIDTH * self._ui_scale), int(DESIGN_HEIGHT * self._ui_scale)
        try:
            sw = self.root.winfo_screenwidth()
        except Exception:
            sw = w + int(120 * self._ui_scale)
        x = max(0, (sw - w) // 2)
        y = int(48 * self._ui_scale)
        if os.environ.get("KIYRA_OVERLAY_OFFSCREEN"):
            # Test / headless runs: keep the window fully off the visible
            # desktop while it stays MAPPED, so real widget geometry is still
            # computed but the suite never steals the developer's screen.
            x, y = 20000, 20000
        self.root.title("Kiyra — AI Interview Assistant")
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        # Fixed-size page: the WINDOW never stretches (not user-resizable);
        # only the answer CONTENT inside it scrolls — long answers cannot
        # resize the window because the pinned geometry wins over children.
        self.root.resizable(False, False)
        self.root.minsize(int(640 * self._ui_scale), int(420 * self._ui_scale))
        self.root.attributes("-topmost", True)
        self.root.configure(bg="#0c1114")
        # Tool-window style FIRST, before the window is ever deiconified: this
        # is the earliest point the shell hwnd exists, so Windows never gets the
        # chance to show a Kiyra taskbar button (even briefly).
        self._apply_stealth_window_styles(reason="init")

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
            # Styles FIRST, then region, then capture-exclusion: nothing may
            # touch the window after SetWindowDisplayAffinity or it can be
            # invalidated.
            self._apply_stealth_window_styles(reason="realize")
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
        # Re-assert the tool-window style: a SetWindowPos/resize can hand the
        # shell its default app-window styles back.
        self._apply_stealth_window_styles(reason="configure")
        self._apply_rounded_corners()
        self._apply_stealth_affinity()

    def _init_styles(self):
        self.font_family = "Segoe UI" if sys.platform == "win32" else "Helvetica"
        self.mono_family = "Consolas" if sys.platform == "win32" else "Menlo"
        # Kiyra practice-panel palette — exact tokens from the supplied
        # "Mock Interview Practice Panel" spec (dark slate panel, green accent).
        self.bg_color = "#0f1219"      # page behind the panel
        self.panel_bg = "#171b25"      # overlay + body panel
        self.card_bg = "#1e2430"       # inputs, buttons, answer box
        self.code_bg = "#0f131b"       # kbd badge background
        self.border_color = "#2b3342"  # thin border / row separators
        self.sep_color = "#2b3342"     # row separator line
        self.row_alt_bg = "#171b25"    # table rows are transparent (panel bg)
        self.badge_bg = "#0f131b"      # shortcut badge
        self.accent_color = "#3ddc84"  # green accent
        self.accent_ink = "#06210f"    # text on accent
        self.text_primary = "#e8ecf3"
        self.text_secondary = "#8d97ab"
        self.success_color = "#3ddc84"  # audio / timer green
        self.green_bg = "#3ddc84"       # timer / record fill
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
        # ── Interview Listening Mode (Phase 5.4) ──────────────────────────
        # The user owns the question boundary: VAD segments speech, the user
        # presses SEND. The live panel is built lazily on `mode.changed` so the
        # default (auto-detect) layout is untouched.
        self._listening_mode = False
        self._listening_active = False
        self._server_state = "IDLE"
        self._can_send = False
        self._live_transcript = ""
        self._live_partial = ""
        self._memory_points: list[str] = []
        self._previous_questions: list[dict] = []
        self._active_topic = ""
        self._turn_submitted = False
        self._listening_panel = None
        # Phase 5.4.1: the previous answer survives SEND and is swapped in
        # place when the first chunk of the new one arrives.
        self._answer_replace_pending = False
        self._question_history: list[dict] = []
        self._history_index = -1

    def _apply_stealth_window_styles(self, reason: str = ""):
        """Make the window a TOOL window: no taskbar button, no Alt+Tab entry.

        Runs against the REAL top-level (shell) hwnd — Tk's ``winfo_id()`` is
        the client window, and it is the shell's extended styles that the taskbar
        and the Alt+Tab switcher read.

        Idempotent and non-destructive: WS_EX_TOOLWINDOW is set,
        WS_EX_APPWINDOW is cleared, and every other bit (WS_EX_LAYERED from
        ``-alpha``, WS_EX_TRANSPARENT from click-through, WS_EX_TOPMOST) is left
        alone. Nothing moves or resizes — SWP_NOMOVE|SWP_NOSIZE keep the fixed
        560x640 geometry, SWP_NOACTIVATE avoids stealing focus, and
        SWP_FRAMECHANGED makes the shell recompute the frame for the new styles.
        HWND_TOPMOST re-asserts always-on-top so this cannot silently drop it.
        """
        try:
            if sys.platform != "win32":
                return None
            hwnd = toplevel_hwnd(self.root.winfo_id())
            if not hwnd or not ctypes.windll.user32.IsWindow(
                    ctypes.wintypes.HWND(hwnd)):
                return None    # window not realized yet — nothing to style

            before = get_window_exstyle(hwnd)
            desired = stealth_exstyle(before)
            if desired != before:
                set_window_exstyle(hwnd, desired)
                # Refresh the non-client frame WITHOUT moving/resizing, and keep
                # the window topmost (SWP_FRAMECHANGED alone can re-apply the
                # shell's default styles).
                ctypes.windll.user32.SetWindowPos(
                    ctypes.wintypes.HWND(hwnd),
                    ctypes.wintypes.HWND(HWND_TOPMOST),
                    0, 0, 0, 0,
                    SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_FRAMECHANGED)

            flags = read_window_shell_flags(hwnd)
            if not flags["toolwindow"] or flags["appwindow"]:
                print(f"[Stealth] WARNING: shell styles did not stick "
                      f"(toolwindow={flags['toolwindow']}, "
                      f"appwindow={flags['appwindow']}) — a taskbar button "
                      f"may still appear.")
            elif not self._shell_styles_reported:
                self._shell_styles_reported = True
                why = f" ({reason})" if reason else ""
                print(f"[Stealth] Shell styles ACTIVE{why}: hwnd={hwnd:#x} "
                      f"exstyle={flags['exstyle']:#x} TOOLWINDOW=on "
                      f"APPWINDOW=off topmost={flags['topmost']} "
                      f"layered={flags['layered']} "
                      f"-> no taskbar button, no Alt+Tab")
            self._shell_flags = flags
            return flags
        except Exception as e:
            print(f"[Stealth] Could not apply shell styles: {e}")
            return None

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
        """Small flat button helper matching the panel spec (1px border, radius)."""
        b = tk.Button(
            parent, text=text, command=command,
            bg=bg if bg else self.card_bg, fg=fg if fg else self.text_primary,
            font=(self.font_family, _fs(font_size), "bold"), relief=tk.FLAT,
            padx=padx, pady=pady, width=width, cursor="hand2",
            activebackground=(bg if bg else self.panel_bg),
            activeforeground=fg if fg else self.text_primary,
            borderwidth=0, highlightthickness=1,
            highlightbackground=self.border_color,
            highlightcolor=self.border_color,
        )
        return b

    def _build_ui(self):
        body = tk.Frame(self.root, bg=self.bg_color)
        body.pack(fill=tk.BOTH, expand=True)
        self._body = body

        # Outer panel shell (the ".overlay" in the spec).
        overlay = tk.Frame(body, bg=self.panel_bg, highlightthickness=1,
                           highlightbackground=self.border_color)
        overlay.pack(fill=tk.BOTH, expand=True)
        self._overlay = overlay

        # ─── Bar 1: logo · record · audio source · … · timer · controls ────────
        bar1 = tk.Frame(overlay, bg=self.panel_bg)
        bar1.pack(fill=tk.X, padx=10, pady=(10, 0))
        self._bar1 = bar1

        s = int(40 * self._ui_scale)
        logo = tk.Canvas(bar1, width=s, height=s, bg=self.panel_bg,
                         highlightthickness=0)
        logo.pack(side=tk.LEFT)
        logo.create_oval(2, 2, s - 2, s - 2, outline=self.text_primary, width=2)
        logo.create_text(s // 2, s // 2, text="K", fill=self.text_primary,
                         font=(self.font_family, _fs(int(13 * self._ui_scale)), "bold"))

        self.btn_rec = self._btn(bar1, "⏺", self.toggle_recording,
                                 bg="#12351f", fg=self.accent_color,
                                 font_size=13, padx=12, pady=4)
        self.btn_rec.pack(side=tk.LEFT, padx=(8, 8))

        self.lbl_sysled = tk.Label(bar1, text="●", bg=self.panel_bg,
                                   fg=self.text_secondary, font=(self.font_family, _fs(10)))
        self.lbl_sysled.pack(side=tk.LEFT)
        self.cmb_sys_audio = ttk.Combobox(bar1, state="readonly", width=30,
                                          font=(self.font_family, _fs(9)))
        self.cmb_sys_audio.pack(side=tk.LEFT, padx=(4, 8))
        self.cmb_sys_audio.bind("<<ComboboxSelected>>", self._on_sys_audio_selected)

        # Center-top: collapse the overlay into the floating bubble. Packed with
        # expand so it sits centered between the audio picker and the controls.
        # Order matters: the fixed-size right-hand control cluster is packed FIRST
        # (side = RIGHT) and the bubble second (side = LEFT, expand) so pack never
        # squeezes the controls out when the window gets tight (e.g. Compact).
        ctrls = tk.Frame(bar1, bg=self.panel_bg)
        ctrls.pack(side=tk.RIGHT)
        self.status_dot = tk.Label(ctrls, text="●", bg=self.panel_bg,
                                   fg=self.danger_color, font=(self.font_family, _fs(9)))
        self.status_dot.pack(side=tk.LEFT, padx=(0, 4))
        self.lbl_status = tk.Label(
            ctrls, text="Offline", bg=self.panel_bg, fg=self.text_secondary,
            font=(self.font_family, _fs(8)))
        self.lbl_status.pack(side=tk.LEFT, padx=(0, 10))
        self.lbl_timer = tk.Label(
            ctrls, text="00:00", bg=self.green_bg, fg=self.accent_ink,
            font=(self.font_family, _fs(10), "bold"), padx=14, pady=6)
        self.lbl_timer.pack(side=tk.LEFT, padx=(0, 8))
        self.btn_pin = self._btn(ctrls, "📌", lambda: self.toggle_pin(), font_size=9, padx=8, pady=4)
        self.btn_pin.pack(side=tk.LEFT, padx=1)
        self.btn_visibility = self._btn(ctrls, "▁", self.toggle_visibility, font_size=9, padx=8, pady=4)
        self.btn_visibility.pack(side=tk.LEFT, padx=1)
        self.btn_settings = self._btn(ctrls, "⚙", self.open_settings, font_size=11, padx=8, pady=4)
        self.btn_settings.pack(side=tk.LEFT, padx=1)
        self.btn_close = self._btn(ctrls, "✕", self._on_close, fg=self.danger_color, font_size=11, padx=8, pady=4)
        self.btn_close.pack(side=tk.LEFT, padx=1)
        self.lbl_session = tk.Label(bar1, text=f"Session {self.session_id}",
                                    bg=self.panel_bg, fg=self.text_secondary,
                                    font=(self.font_family, _fs(8)))

        # Bubble (collapsible into the mini window) — packed AFTER the fixed control
        # cluster, with `expand`, so it centers in the leftover space and the
        # control cluster keeps its full size at every preset (incl. Compact).
        self.bubble_wrap = tk.Frame(bar1, bg=self.panel_bg)
        self.bubble_wrap.pack(side=tk.LEFT, expand=True)
        self.btn_bubble = self._btn(
            self.bubble_wrap, "◌ Bubble", self.toggle_mini,
            font_size=9, padx=12, pady=4)
        self.btn_bubble.pack()


        # ─── Bar 2: capture · mode select · get feedback · clear ───────────────
        bar2 = tk.Frame(overlay, bg=self.panel_bg)
        bar2.pack(fill=tk.X, padx=10, pady=(8, 0))
        self._bar2 = bar2

        tk.Frame(bar2, bg=self.panel_bg, width=4).pack(side=tk.LEFT)
        self.btn_screenshot = self._btn(
            bar2, "📸 Screenshot", self._on_screenshot_click,
            font_size=9, padx=10, pady=5)
        self.btn_screenshot.pack(side=tk.LEFT)

        # SEND + clear are packed BEFORE the expanding transcript so pack never
        # squeezes them out when the row's space gets tight (this is what happens at
        # the Compact preset — the transcript used to take priority and SEND + Clear
        # were allocated 0px, making the heard-question submission impossible).
        # side=RIGHT: earlier-packed items settle further right, so Clear must be
        # packed first to keep the visual order [SEND][✕ Clear] with SEND left.
        self.btn_clear_mode = self._btn(bar2, "✕ Clear", self.clear_inputs,
                                        font_size=9, padx=10, pady=5)
        self.btn_send = self._btn(bar2, "SEND", self._send_turn,
                                  bg=self.accent_color, fg=self.accent_ink,
                                  font_size=9, padx=14, pady=5, state=tk.DISABLED)
        self.btn_clear_mode.pack(side=tk.RIGHT, padx=(8, 0))
        self.btn_send.pack(side=tk.RIGHT)
        self.btn_send.config(state=tk.DISABLED)

        # Live transcript of the interviewer's question. It is fed by the same
        # backend transcript.* events as Interview Listening Mode — a single
        # speech-recognition path, never a second one.
        # Container so we can add a vertical scrollbar without breaking bar2's
        # pack order (SEND / Clear stay pinned to the right).
        live_wrap = tk.Frame(bar2, bg=self.card_bg)
        live_wrap.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=8)

        self.txt_live = tk.Text(
            live_wrap, height=4, bg=self.card_bg, fg=self.text_primary,
            font=(self.font_family, _fs(9)), wrap=tk.WORD, relief=tk.FLAT,
            borderwidth=0, padx=10, pady=6, highlightthickness=1,
            highlightbackground=self.border_color, highlightcolor=self.border_color)
        self.txt_live.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.txt_live.insert("1.0", LIVE_PLACEHOLDER)
        self.txt_live.config(state=tk.DISABLED)

        # Vertical scrollbar for lengthy transcripts — otherwise long questions
        # get cut off at height=4 and the user can never read the tail.
        live_scroll = tk.Scrollbar(live_wrap, orient=tk.VERTICAL,
                                   command=self.txt_live.yview,
                                   bg=self.card_bg, highlightthickness=0)
        live_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.txt_live.configure(yscrollcommand=live_scroll.set)
        self._live_scroll = live_scroll

        # Mode tabs kept for compatibility (required by tests) but never shown;
        # the practice panel is a single always-visible layout.
        self.tab_latest = self._btn(bar2, "Latest", lambda: self._switch_tab(False))
        self.tab_manual = self._btn(bar2, "Ask manually", lambda: self._switch_tab(True))

        # ─── Bar 3: history nav · manual input · send · clear ──────────────────
        manual = tk.Frame(overlay, bg=self.panel_bg)
        manual.pack(fill=tk.X, padx=10, pady=(8, 0))
        self._manual_frame = manual

        self.btn_mhist_up = self._btn(manual, "←", self._history_prev,
                                      font_size=11, padx=12, pady=5)
        self.btn_mhist_up.pack(side=tk.LEFT)
        self.btn_mhist_down = self._btn(manual, "→", self._history_next,
                                        font_size=11, padx=12, pady=5)
        self.btn_mhist_down.pack(side=tk.LEFT, padx=(8, 0))

        self.entry_manual = tk.Entry(
            manual, bg=self.card_bg, fg=self.text_primary,
            font=(self.font_family, _fs(10)), insertbackground=self.text_primary,
            relief=tk.FLAT, borderwidth=0, highlightthickness=1,
            highlightbackground=self.border_color, highlightcolor=self.accent_color
        )
        self.entry_manual.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=8, ipady=8)
        self.entry_manual.insert(0, ASK_PLACEHOLDER)
        self.entry_manual.bind("<Return>", lambda e: self.send_manual_question())
        self.entry_manual.bind("<FocusIn>", self._entry_focus_in)

        self.btn_clear_manual = self._btn(manual, "✕", self.clear_inputs,
                                          font_size=11, padx=12, pady=5)
        self.btn_clear_manual.pack(side=tk.RIGHT)
        self.btn_send_manual = self._btn(manual, "➤", self.send_manual_question,
                                         bg=self.accent_color, fg=self.accent_ink,
                                         font_size=11, padx=12, pady=5)
        self.btn_send_manual.pack(side=tk.RIGHT, padx=(0, 8))

        # ─── Body panel: answer · features (no "Practice session" header) ──────
        panel = tk.Frame(overlay, bg=self.panel_bg, highlightthickness=1,
                         highlightbackground=self.border_color)
        panel.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        self._panel = panel

        # ─── Listening-mode slot (state/topic/history/memory) ─────────────────
        # A fixed row packed BEFORE the expandable answer/features panel, so it
        # is always allocated its space and can never be squeezed to 0px. Packed
        # after `panel` without `before=` it collapsed to 1px and was never
        # mapped, which hid the history / previous-answer view entirely.
        self._listening_slot = tk.Frame(overlay, bg=self.panel_bg)
        self._listening_slot.pack(fill=tk.X, before=self._panel)

        # Answer / feedback box (scrolled; answer sections render inside).
        self.lbl_inference = tk.Label(panel, text="", bg=self.panel_bg,
                                      fg=self.text_secondary, font=(self.font_family, _fs(8)),
                                      anchor="e")
        self.lbl_inference.pack(fill=tk.X, padx=14)

        ans_container = tk.Frame(panel, bg=self.card_bg, highlightthickness=1,
                                 highlightbackground=self.border_color)
        ans_container.pack(fill=tk.BOTH, expand=True, padx=14, pady=(4, 8))
        sec_scroll = tk.Scrollbar(ans_container)
        sec_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.ans_canvas = tk.Canvas(ans_container, bg=self.card_bg,
                                    highlightthickness=0, yscrollcommand=sec_scroll.set)
        self.ans_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sec_scroll.config(command=self.ans_canvas.yview)

        self.answer_inner = tk.Frame(self.ans_canvas, bg=self.card_bg)
        self._answer_inner_width = 0
        self.answer_win = self.ans_canvas.create_window(
            (0, 0), window=self.answer_inner, anchor="nw",
            width=self.ans_canvas.winfo_reqwidth())
        self.answer_inner.bind("<Configure>", self._on_answer_inner_configure)
        self.ans_canvas.bind("<Configure>", self._on_answer_canvas_configure)

        self._answer_widgets = []
        self._answer_scroll = 0

        # ─── Features cheat-sheet (real controls; toggled by btn_features) ─────
        self._features_panel = tk.Frame(panel, bg=self.panel_bg)
        self._build_features_panel()
        self._features_panel.pack(fill=tk.BOTH, expand=True, padx=14, pady=(0, 10))

        # ─── Footer: status + capture controls + features toggle ───────────────
        footer = tk.Frame(overlay, bg=self.panel_bg)
        footer.pack(fill=tk.X, padx=10, pady=(0, 8), before=self._panel)

        # Visible shortcut hint strip — reminds the user of the most important
        # global hotkeys without opening the Features panel.
        self.lbl_shortcuts = tk.Label(
            footer,
            text="⌨ F9 show/hide  •  F10 click-through  •  Shift+Alt+A ask  •  "
                 "Shift+Alt+D clear  •  Shift+Alt+S screenshot  •  Shift+Alt+H bubble",
            bg=self.panel_bg, fg=self.text_secondary, font=(self.font_family, _fs(8)))
        self.lbl_shortcuts.pack(side=tk.LEFT)

        self.lbl_capture = tk.Label(
            footer, text="Ready", bg=self.panel_bg, fg=self.text_secondary,
            font=(self.font_family, _fs(8)))
        self.lbl_capture.pack(side=tk.LEFT, padx=(20, 0))

        self.btn_start = self._btn(footer, "▶ Start", self.start_capture,
                                   bg="#12351f", fg=self.accent_color, font_size=8,
                                   padx=8, pady=3)
        self.btn_start.pack(side=tk.RIGHT)
        self.btn_stop = self._btn(footer, "⏹ Stop", self.stop_capture,
                                  bg="#3a1f1f", fg=self.danger_color, font_size=8,
                                  padx=8, pady=3, state=tk.DISABLED)
        self.btn_stop.pack(side=tk.RIGHT, padx=(4, 0))
        self.btn_features = self._btn(footer, "▤ Features", self.toggle_features,
                                      font_size=8, padx=8, pady=3)
        self.btn_features.pack(side=tk.RIGHT, padx=(4, 0))

        # Manual row is always visible in this layout.
        self._set_tab_active(False)

    def _build_features_panel(self):
        """The reference "Features" cheat-sheet: a BUTTON | PURPOSE | SHORTCUT
        table (rows separated by hairlines, shortcut rendered as a kbd badge).
        Every actionable row is a *real* control — clicking it runs the action
        named in its BUTTON column (no decorative/mock rows)."""
        p = self._features_panel
        scale = self._ui_scale

        # Scrollable region: the cheat-sheet can be taller than the answer pane
        # always offers (notably at the Compact preset), and every feature row must
        # stay reachable — so the table lives in a canvas + scrollbar instead of
        # overflowing the panel bottom.
        tbl_sc = tk.Frame(p, bg=self.panel_bg)
        tbl_sc.pack(fill=tk.BOTH, expand=True, pady=(0, 2))
        head = tk.Frame(tbl_sc, bg=self.panel_bg)
        head.pack(fill=tk.X, pady=(2, 6))
        tk.Label(head, text="Features", bg=self.panel_bg, fg=self.text_primary,
                 font=(self.font_family, _fs(11), "bold")).pack(side=tk.LEFT)
        self.lbl_syslive = tk.Label(head, text="System audio: idle", bg=self.panel_bg,
                                    fg=self.text_secondary, font=(self.font_family, _fs(8)))
        self.lbl_syslive.pack(side=tk.RIGHT)

        tbl_sb = tk.Scrollbar(tbl_sc, orient=tk.VERTICAL, bg=self.panel_bg,
                               highlightthickness=0)
        tbl_sb.pack(side=tk.RIGHT, fill=tk.Y)
        tbl_cv = tk.Canvas(tbl_sc, bg=self.panel_bg, highlightthickness=0)
        tbl_cv.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        tbl_cv.configure(yscrollcommand=tbl_sb.set)
        tbl_sb.configure(command=tbl_cv.yview)
        tbl_cv.bind("<Configure>", lambda e: (
            tbl_cv.configure(scrollregion=tbl_cv.bbox("all"))))
        tbl = tk.Frame(tbl_cv, bg=self.panel_bg)
        tbl_win = tbl_cv.create_window((0, 0), window=tbl, anchor="nw")
        tbl.bind("<Configure>", lambda e: tbl_cv.configure(scrollregion=tbl_cv.bbox("all")))
        tbl_cv.bind("<Configure>", lambda e: tbl_cv.itemconfigure(tbl_win, width=e.width))
        tbl_cv.bind("<MouseWheel>", lambda e: tbl_cv.yview_scroll(int(-1 * e.delta / 120), "units"))

        tbl.columnconfigure(0, weight=0, minsize=int(92 * scale))
        tbl.columnconfigure(1, weight=1)
        tbl.columnconfigure(2, weight=0, minsize=int(138 * scale))

        def _sep(row):
            tk.Frame(tbl, bg=self.sep_color, height=1).grid(
                row=row, column=0, columnspan=3, sticky="ew")

        def _cell(r, c, text, bold=False, fg=None, anchor="w", wrap=0):
            l = tk.Label(
                tbl, text=text, bg=self.panel_bg, anchor=anchor, justify=tk.LEFT,
                fg=fg or (self.text_primary if bold else self.text_secondary),
                font=(self.font_family, _fs(8), "bold" if bold else "normal"),
                wraplength=wrap)
            l.grid(row=r, column=c, sticky="ew", padx=(6, 6), pady=6)
            return l

        # header row (BUTTON | PURPOSE | SHORTCUT), shortcut right-aligned.
        _cell(0, 0, "BUTTON", bold=True, fg=self.text_secondary)
        _cell(0, 1, "PURPOSE", bold=True, fg=self.text_secondary)
        _cell(0, 2, "SHORTCUT", bold=True, fg=self.text_secondary, anchor="e")
        _sep(1)

        row = 2

        def _row(label, purpose, shortcut, action=None):
            nonlocal row
            lbl = _cell(row, 0, label, bold=True, fg=self.text_primary)
            pur = _cell(row, 1, purpose, wrap=int(360 * scale))
            holder = tk.Frame(tbl, bg=self.panel_bg)
            holder.grid(row=row, column=2, sticky="e", padx=(6, 6), pady=6)
            if shortcut:
                badge = tk.Label(
                    holder, text=shortcut, bg=self.code_bg, fg=self.text_secondary,
                    font=(self.mono_family, _fs(8)), padx=8, pady=2,
                    highlightthickness=1, highlightbackground=self.border_color)
                badge.pack(side=tk.RIGHT)
            if action is not None:
                for w in (lbl, pur, holder, *holder.winfo_children()):
                    w.configure(cursor="hand2")
                    w.bind("<Button-1>", lambda _e, a=action: a())
            row += 1
            _sep(row)
            return holder

        _row("Send", "Get AI feedback on my answer", "Shift+Alt+A", self.focus_ask)
        _row("Clear", "Clear input box", "Shift+Alt+D", self.clear_inputs)
        _row("▲ ▼", "Scroll the feedback up / down", "Shift+Alt+W X",
             lambda: self._scroll_answer(-3))
        _row("Capture", "Capture a screenshot of a practice question",
             "Shift+Alt+S", self._on_screenshot_click)

        # Navigate — real prev / next buttons live in the shortcut cell.
        c2 = _row("← →", "Navigate previous / next question", "Shift+Alt+[ ]",
                  self._history_prev)
        self.btn_features_hist_prev = self._btn(
            c2, "←", self._history_prev, bg=self.code_bg, fg=self.text_primary,
            font_size=8, padx=6, pady=0)
        self.btn_features_hist_prev.pack(side=tk.RIGHT, padx=(4, 0))
        self.btn_features_hist_next = self._btn(
            c2, "→", self._history_next, bg=self.code_bg, fg=self.text_primary,
            font_size=8, padx=6, pady=0)
        self.btn_features_hist_next.pack(side=tk.RIGHT, padx=(4, 0))

        _row("Move", "Move the window up, left, down, right",
             "Shift+Alt+I J K L", lambda: self._move_window(0, -24))
        _row("Resize", "Drag the bottom-right corner of the window", "")
        _row("Audio",
             "Mock question audio plays and is transcribed when you press record",
             "")

        # Transparency — not in the spec's table, but kept functional (the
        # window's Shift+Alt+-/= affordance). Rendered as a live slider.
        c2 = _row("Opacity", "Adjust window transparency", "Shift+Alt+- =")
        self.lbl_alpha = tk.Label(c2, text="100%", bg=self.panel_bg,
                                  fg=self.text_secondary, font=(self.font_family, _fs(8)),
                                  width=4)
        self.lbl_alpha.pack(side=tk.RIGHT)
        self.alpha_scale = ttk.Scale(c2, from_=0.35, to=1.0, orient=tk.HORIZONTAL,
                                     length=int(90 * scale), command=self._on_alpha)
        self.alpha_scale.pack(side=tk.RIGHT, padx=(4, 0))

        # ── resize grip (bottom-right "◢"): drag to resize the panel ──────────
        grip = tk.Label(p, text="◢", bg=self.panel_bg, fg=self.text_secondary,
                        font=(self.font_family, _fs(11)), cursor="sizing")
        grip.pack(anchor="e")
        grip.bind("<Button-1>", self._begin_resize)
        grip.bind("<B1-Motion>", self._drag_resize)
        grip.bind("<ButtonRelease-1>", self._end_resize)
        self._grip = grip

    # ─────────────────────────────────────────────────────────────────────────
    # Tab / Input Switching
    # ─────────────────────────────────────────────────────────────────────────

    def _switch_tab(self, manual: bool):
        """Set the active input mode.

        The practice panel uses a single always-visible layout (per the spec),
        so switching modes only updates the flag and the tab affordances — the
        manual row and answer area stay mounted.
        """
        self._manual_mode = manual
        self._set_tab_active(manual)

    def _set_tab_active(self, manual: bool):
        active_bg = self.accent_color
        idle_bg = self.card_bg
        self.tab_latest.config(bg=idle_bg if manual else active_bg,
                               fg=self.text_secondary if manual else self.accent_ink)
        self.tab_manual.config(bg=active_bg if manual else idle_bg,
                               fg=self.accent_ink if manual else self.text_secondary)

    def _entry_focus_in(self, _e):
        if self.entry_manual.get() == ASK_PLACEHOLDER:
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

    # ─── Manual region selection (Phase: Screenshot region picker) ────────────

    class RegionSelector:
        """Full-screen transparent overlay with a crosshair cursor.

        The user clicks and drags to draw a rectangle over the exact area they
        want captured. On release the rectangle's bounds are returned via the
        callback; pressing Escape cancels.
        """

        def __init__(self, root, on_complete):
            self.root = root
            self.on_complete = on_complete
            self.win = None
            self.canvas = None
            self.start_x = None
            self.start_y = None
            self.rect_id = None
            self.virtual_screen = None

        def start(self):
            import ctypes
            user32 = ctypes.windll.user32
            x = user32.GetSystemMetrics(76)   # SM_XVIRTUALSCREEN
            y = user32.GetSystemMetrics(77)   # SM_YVIRTUALSCREEN
            w = user32.GetSystemMetrics(78)   # SM_CXVIRTUALSCREEN
            h = user32.GetSystemMetrics(79)   # SM_CYVIRTUALSCREEN
            self.virtual_screen = (x, y, w, h)

            self.win = tk.Toplevel(self.root)
            self.win.overrideredirect(True)          # no title bar / borders
            self.win.attributes("-topmost", True)
            self.win.attributes("-alpha", 0.25)      # see the screen beneath
            self.win.geometry(f"{w}x{h}+{x}+{y}")
            self.win.configure(bg="black")
            self.win.lift()
            self.win.focus_force()

            self.canvas = tk.Canvas(
                self.win, width=w, height=h,
                bg="black", highlightthickness=0, cursor="crosshair",
            )
            self.canvas.pack(fill=tk.BOTH, expand=True)

            # Instruction banner
            self.instr = tk.Label(
                self.win,
                text="Click and drag to select the question area  •  Esc to cancel",
                bg="black", fg="#ff6b6b", font=("Segoe UI", 15, "bold"),
            )
            self.instr.place(relx=0.5, rely=0.06, anchor="center")

            self.canvas.bind("<Button-1>", self._on_press)
            self.canvas.bind("<B1-Motion>", self._on_drag)
            self.canvas.bind("<ButtonRelease-1>", self._on_release)
            self.win.bind("<Escape>", lambda e: self.cancel())

        def _on_press(self, event):
            self.start_x = self.canvas.canvasx(event.x)
            self.start_y = self.canvas.canvasy(event.y)
            if self.rect_id:
                self.canvas.delete(self.rect_id)
            self.rect_id = self.canvas.create_rectangle(
                self.start_x, self.start_y, self.start_x, self.start_y,
                outline="#ff6b6b", width=3, fill="", dash=(6, 4),
            )

        def _on_drag(self, event):
            if self.start_x is None:
                return
            cur_x = self.canvas.canvasx(event.x)
            cur_y = self.canvas.canvasy(event.y)
            self.canvas.coords(self.rect_id, self.start_x, self.start_y, cur_x, cur_y)

        def _on_release(self, event):
            if self.start_x is None or self.rect_id is None:
                self.cancel()
                return
            end_x = self.canvas.canvasx(event.x)
            end_y = self.canvas.canvasy(event.y)
            ox, oy = self.virtual_screen[0], self.virtual_screen[1]
            left = min(self.start_x, end_x) + ox
            top = min(self.start_y, end_y) + oy
            right = max(self.start_x, end_x) + ox
            bottom = max(self.start_y, end_y) + oy
            self.win.destroy()
            self.win = None
            if (right - left) > 8 and (bottom - top) > 8:
                self.on_complete((int(left), int(top), int(right), int(bottom)))
            else:
                self.on_complete(None)

        def cancel(self):
            if self.win:
                try:
                    self.win.destroy()
                except Exception:
                    pass
                self.win = None
            self.on_complete(None)

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
                # Screenshot flow: temp-hide deiconifies the window, so the
                # tool-window style is re-asserted here — the taskbar must stay
                # free of Kiyra after a capture.
                self._apply_stealth_window_styles(reason="capture-restore")
            except Exception:
                pass
        self._safe_after(cb)

    def _capture_worker(self):
        """Worker thread: temp-hide → [optional region select] → grab → restore → OCR → submit.

        Restore is guaranteed in ``finally``: even if capture or OCR raises,
        the overlay is never left hidden and the button returns to READY.

        Set KIYRA_SCREENSHOT_REGION=1 to enable manual region selection
        (click-and-drag). Defaults to full-screen capture for backward
        compatibility with automated/test environments.
        """
        was_viewable = True
        restored = False
        try:
            hide_done = threading.Event()
            self._region_bbox = None
            self._region_event = threading.Event()

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
            time.sleep(0.03)

            use_region = os.environ.get("KIYRA_SCREENSHOT_REGION", "0") == "1"

            if use_region:
                # ── Manual region selection on the Tk thread ──────────────────
                def _start_region():
                    selector = self.RegionSelector(self.root, self._on_region_complete)
                    selector.start()
                    self._region_selector = selector

                self._safe_after(_start_region)
                if not self._region_event.wait(timeout=120.0):
                    raise RuntimeError("Region selection timed out")
                bbox = self._region_bbox
                self._region_selector = None
                if bbox is None:
                    raise RuntimeError("Region selection cancelled — no area chosen")

                # Capture only the user-selected region
                shot = ScreenshotCapture().capture_region(bbox)
            else:
                # Full-screen capture (default, backward-compatible)
                shot = ScreenshotCapture().capture()

            self._restore_overlay_after_capture()
            restored = True
            self._set_screenshot_state("Analyzing...")

            payload = self._ocr_request(shot)
            self._safe_after(lambda: self._on_screenshot_ocr_ready(payload, shot))
        except Exception as e:
            if not restored:
                self._restore_overlay_after_capture()
            err_msg = str(e)
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

    def _on_region_complete(self, bbox):
        """Callback from RegionSelector when the user releases the mouse.

        Runs on the Tk thread. Stores the bbox and signals the worker thread
        to proceed with capture.
        """
        self._region_bbox = bbox
        self._region_event.set()

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
            # The OCR'd question becomes the live transcript, so the single
            # transcript display is the one source of truth (no duplicate box).
            self._set_live_text(question)
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
                 fg=accent or self.text_primary, font=(self.font_family, _fs(9), "bold")).pack(anchor="w")
        txt = tk.Text(
            frame, height=max(2, body_txt.count("\n") + 2),
            bg=self.code_bg if code else self.card_bg,
            fg="#d7f0e8" if code else self.text_primary,
            font=(self.mono_family, _fs(9)) if code else (self.font_family, _fs(9)),
            wrap=tk.WORD, relief=tk.FLAT, borderwidth=0,
            padx=10, pady=6, highlightthickness=0, spacing1=1, spacing3=1
        )
        if code:
            txt.config(fg="#d7f0e8", bg=self.code_bg,
                       font=(self.mono_family, _fs(9)))
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
        if not getattr(self, "_rendering_history", False):
            self._live_answer_payload = dict(payload)
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

        self._answer_scroll = self.ans_canvas.yview()[0] if self.ans_canvas.winfo_exists() else 0

    # ─────────────────────────────────────────────────────────────────────────
    # Hotkeys
    # ─────────────────────────────────────────────────────────────────────────

    def _bind_shortcuts(self):
        """Bind global hotkeys."""
        self.root.bind("<F9>", lambda e: self.toggle_visibility())
        self.root.bind("<F10>", lambda e: self.toggle_click_through())
        # Phase 5.5 Shift+Alt shortcuts (GirGit-style overlay chrome).
        for seq, fn in [
            ("<Shift-Alt-a>", lambda e: self.focus_ask()),
            ("<Shift-Alt-d>", lambda e: self.clear_inputs()),
            ("<Shift-Alt-w>", lambda e: self._scroll_answer(-6)),
            ("<Shift-Alt-x>", lambda e: self._scroll_answer(6)),
            ("<Shift-Alt-s>", lambda e: self._on_screenshot_click()),
            ("<Shift-Alt-h>", lambda e: self.toggle_mini()),
            ("<Shift-Alt-bracketleft>", lambda e: self._history_prev()),
            ("<Shift-Alt-bracketright>", lambda e: self._history_next()),
            ("<Shift-Alt-minus>", lambda e: self._adjust_alpha(-0.1)),
            ("<Shift-Alt-equal>", lambda e: self._adjust_alpha(0.1)),
            ("<Shift-Alt-i>", lambda e: self._move_window(0, -24)),
            ("<Shift-Alt-j>", lambda e: self._move_window(-24, 0)),
            ("<Shift-Alt-k>", lambda e: self._move_window(0, 24)),
            ("<Shift-Alt-l>", lambda e: self._move_window(24, 0)),
        ]:
            try:
                self.root.bind(seq, fn)
            except Exception as e:
                print(f"[UI] shortcut bind failed {seq}: {e}")

    def toggle_visibility(self):
        """Toggle window visibility (F9)."""
        if self.root.winfo_viewable():
            self.root.withdraw()
            print("[Stealth] Hidden (F9 to show)")
        else:
            self.root.deiconify()
            self.root.attributes("-topmost", True)
            # Re-assert after remapping so the taskbar button cannot reappear.
            self._apply_stealth_window_styles(reason="f9-show")
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

        self._sync_sys_audio_selector()

    # ─────────────────────────────────────────────────────────────────────────
    # Capture Lifecycle
    # ─────────────────────────────────────────────────────────────────────────

    def toggle_recording(self):
        """Bar-1 record button: start capture when idle, stop when recording."""
        if getattr(self, "_recording", False) or getattr(self, "is_capturing", False):
            self.stop_capture()
        else:
            self.start_capture()

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
        self._start_timer()
        self._recording = True
        if getattr(self, "btn_rec", None) is not None:
            self.btn_rec.config(text="⏺", bg=self.accent_color, fg=self.accent_ink)
        print("[Capture] Started")

    def stop_capture(self):
        """Stop audio capture."""
        self._stop_event.set()
        self.is_capturing = False
        self.lbl_capture.config(text="⏹ Stopped")
        self.btn_start.config(state=tk.NORMAL)
        self.btn_stop.config(state=tk.DISABLED)
        self._recording = False
        if getattr(self, "btn_rec", None) is not None:
            self.btn_rec.config(text="⏺", bg="#12351f", fg=self.accent_color)
        self._loopback_active = False
        self._stop_timer()
        self._render_sys_audio_state()
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
        ``_MixingChunkSink`` pairs their 250 ms buffers and mixes them per
        chunk (a solo-active source passes through at unity), pushing ONE
        mixed 250ms/16k/mono PCM16 WAV stream into the bounded ``_audio_queue``
        (drop-oldest). Without the mixer the two sources would interleave loud
        speech with near-silence every 250 ms and Whisper would return empty
        transcripts. A failure in either source never kills the other
        (single-source degrades cleanly)."""
        captures = []
        # No fixed mic gate: this mic's noise floor (~0.007) sits below the
        # old 0.015 gate, so gating zeroed the voice and fragmented utterances.
        # The backend's adaptive EnergyVAD handles room noise instead, and the
        # mixer keeps a solo-active source at unity so quiet speech stays above
        # the VAD start threshold.
        mixer = _MixingChunkSink(self._push_audio_chunk, n_sources=2,
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
                self._loopback_active = True
                self._render_sys_audio_state()
            else:
                print("[AUDIO_CAPTURE] microphone_only_fallback")
                self.loopback_device = None
                self._loopback_active = False
                self._render_sys_audio_state()

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
                    self._negotiate_listening_mode()
                elif ev_type == "mode.changed":
                    self._ui_mode_changed(payload)
                elif ev_type == "listening.started":
                    self._ui_listening_started(payload)
                elif ev_type == "listening.stopped":
                    self._ui_listening_stopped(payload)
                elif ev_type == "transcript.segment":
                    self._ui_transcript_segment(payload)
                elif ev_type == "transcript.partial":
                    self._ui_transcript_partial(payload)
                elif ev_type == "turn.submitted":
                    self._ui_turn_submitted(payload)
                elif ev_type == "memory.updated":
                    self._ui_memory_updated(payload)
                elif ev_type == "perf":
                    print(f"[PERF] {payload}")
                elif ev_type == "session.error":
                    msg_text = payload.get("message", "Unknown error")
                    print(f"[WS] Error: {msg_text}")
                    self._ui_turn_error(payload)
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

    # ── Interview Listening Mode ───────────────────────────────────────────
    def _negotiate_listening_mode(self):
        """Ask the backend to switch to Interview Listening Mode.

        Sent once the session is connected. `--auto-detect` (or the env
        override) keeps the legacy behaviour, in which case nothing is sent and
        the panel is never built.
        """
        if not self._want_listening_mode:
            return
        if self._send_ws_message({"type": "mode.set",
                                  "payload": {"mode": "interview_listening"}}):
            print("[UI] Requested Interview Listening Mode")

    def _ui_mode_changed(self, payload: dict):
        mode = payload.get("mode")
        self._listening_mode = mode == "interview_listening"
        if not self._listening_mode:
            return
        self._safe_after(self._build_listening_panel)
        if self._want_auto_start:
            self._send_ws_message({"type": "listening.start", "payload": {}})

    def _build_listening_panel(self):
        """Mark Interview Listening Mode as active.

        The inline listening strip was removed: the live transcript lives in
        bar 2 (``txt_live``) and the previous/next arrows live in the manual row
        (bar 3). ``_listening_panel`` is kept as a non-None marker so the render
        path and its guards stay valid.
        """
        if self._listening_panel is not None:
            return
        self._listening_panel = self._listening_slot

    def _toggle_listening(self):
        if not self._listening_mode:
            return
        self._send_ws_message(
            {"type": "listening.stop" if self._listening_active
             else "listening.start", "payload": {}})

    def _send_turn(self):
        """SEND is the only thing that submits a question in this mode."""
        if not self._listening_mode:
            return
        if self._assistant_state == "generating" or not self._can_send:
            # Defence in depth: the button is disabled too, but a second
            # submission must be impossible even if a click slips through.
            return
        if self._send_ws_message({"type": "turn.send", "payload": {}}):
            self._set_send_enabled(False)

    def _set_send_enabled(self, enabled: bool):
        self._can_send = bool(enabled)
        self._safe_after(self._apply_send_state)

    def _apply_send_state(self):
        if self._listening_panel is None or not hasattr(self, "btn_send"):
            return
        busy = self._assistant_state == "generating"
        self.btn_send.config(
            state=tk.NORMAL if (self._can_send and not busy) else tk.DISABLED)

    def _ui_listening_started(self, payload: dict):
        self._listening_active = bool(payload.get("listening", True))
        self._server_state = payload.get("state") or "LISTENING"
        # Auto re-arm (§5) and a fresh START both reset only the live
        # transcript — the answer, history and memory stay on screen.
        self._live_transcript = ""
        self._live_partial = ""
        self._turn_submitted = False
        self._set_send_enabled(False)
        self._start_timer()
        self._safe_after(self._render_live)

    def _ui_listening_stopped(self, payload: dict):
        self._listening_active = False
        self._server_state = payload.get("state") or "IDLE"
        current = payload.get("current") or ""
        if current:
            self._live_transcript = current
        self._set_send_enabled(bool(payload.get("can_send")))
        self._safe_after(self._render_live)

    def _ui_transcript_partial(self, payload: dict):
        self._live_partial = payload.get("text") or payload.get("partial") or ""
        self._safe_after(self._render_live)

    def _ui_transcript_segment(self, payload: dict):
        """Append one finalized VAD segment — this never submits anything."""
        text = payload.get("current")
        if text is None:
            seg = payload.get("text") or ""
            base = (self._live_transcript + " " + seg).strip()
            text = re.sub(r"\s+", " ", base) or seg
        self._live_transcript = text or ""
        self._live_partial = ""
        self._set_send_enabled(bool(payload.get("can_send")))
        self._safe_after(self._render_live)

    def _ui_turn_submitted(self, payload: dict):
        self._turn_submitted = True
        self._live_transcript = payload.get("question") or self._live_transcript
        self._set_send_enabled(False)
        # A new SEND returns the selector to the latest question (§9).
        self._history_index = len(self._question_history)
        self._viewing_history = False
        self._clamp_history_index()
        self._safe_after(self._render_live)

    def _ui_turn_error(self, payload: dict):
        code = payload.get("code") or ""
        if code == "already_generating":
            self._set_send_enabled(False)
        elif code in ("empty_turn", "too_short"):
            # Turn kept and unfrozen: the user can keep speaking or retry.
            self._safe_after(self._render_live)

    def _ui_memory_updated(self, payload: dict):
        self._memory_points = list(payload.get("memory_points") or [])[:6]
        self._previous_questions = list(payload.get("previous") or [])[-8:]
        # Topic is preserved, never blanked by a memory push (§16).
        topic = payload.get("topic") or ""
        if topic:
            self._active_topic = topic
        self._question_history = list(self._previous_questions)
        idx = payload.get("history_index")
        self._history_index = (int(idx) if isinstance(idx, int)
                               else len(self._question_history) - 1)
        self._clamp_history_index()
        self._safe_after(self._render_live)

    def _clamp_history_index(self) -> None:
        """Boundaries do not wrap (§9)."""
        count = len(self._question_history)
        if count == 0:
            self._history_index = -1
        elif self._history_index < 0:
            self._history_index = 0
        elif self._history_index >= count:
            self._history_index = count - 1

    def _history_prev(self):
        """UP: show an earlier question. Local UI only — no WS command."""
        if not self._question_history:
            return
        if self._history_index <= 0:
            print("[UI] history up ignored at first entry")
            return
        self._history_index -= 1
        print(f"[UI] history index={self._history_index}")
        self._safe_after(self._render_live)
        self._safe_after(self._show_history_entry)

    def _history_next(self):
        """DOWN: show a later question. Local UI only — no WS command."""
        if not self._question_history:
            return
        if self._history_index >= len(self._question_history) - 1:
            print("[UI] history down ignored at last entry")
            return
        self._history_index += 1
        print(f"[UI] history index={self._history_index}")
        self._safe_after(self._render_live)
        self._safe_after(self._show_history_entry)

    def _render_history(self):
        """Sync the ← → history controls (§9).

        The old inline viewer was removed — the selected Q/A is painted into
        the main answer box instead (see :meth:`_show_history_entry`).
        """
        hist = self._question_history
        try:
            has_prev = len(hist) > 0 and self._history_index > 0
            has_next = (len(hist) > 0
                        and self._history_index < len(hist) - 1)
            self._sync_hist_controls(has_prev, has_next)
        except Exception as e:
            print(f"[UI] render history failed: {e}")

    def _show_history_entry(self):
        """Paint the selected previous Q/A into the MAIN answer box (§9).

        Returning to the latest entry restores the live answer that was on
        screen before navigation started."""
        hist = self._question_history
        if not hist or self._history_index < 0:
            return
        if self._history_index >= len(hist) - 1:
            # Back at the latest turn: restore the live answer view.
            if self._viewing_history and self._live_answer_payload is not None:
                self._viewing_history = False
                payload = self._live_answer_payload
                self._rendering_history = True
                try:
                    self._render_answer(dict(payload))
                finally:
                    self._rendering_history = False
            return
        entry = hist[self._history_index]
        question = (entry.get("question") or "") if isinstance(entry, dict) \
            else str(entry)
        answer = (entry.get("answer") or "") if isinstance(entry, dict) else ""
        body = f"Q: {question}\n\nA: {answer}" if answer \
            else f"Q: {question}\n\n(no answer recorded)"
        self._viewing_history = True
        self._rendering_history = True
        try:
            self._render_answer({"answer": body})
        finally:
            self._rendering_history = False

    def _set_live_text(self, text: str):
        """Write the live-transcript field directly (Tk thread, disabled Text).

        Shared by the non-listening input paths (screenshot OCR and detected
        question) so they all update the single bar-2 transcript display."""
        if not hasattr(self, "txt_live"):
            return
        try:
            self.txt_live.config(state=tk.NORMAL)
            self.txt_live.delete("1.0", tk.END)
            self.txt_live.insert("1.0", text or LIVE_PLACEHOLDER)
            self.txt_live.see(tk.END)
            self.txt_live.config(state=tk.DISABLED, fg=self.text_primary)
        except Exception as e:
            print(f"[UI] set live text failed: {e}")

    def _render_live(self):
        """Tk thread: paint the live transcript, SEND state and history nav."""
        if self._listening_panel is None or not hasattr(self, "txt_live"):
            return
        try:
            body = self._live_transcript or self._live_partial or ""
            self.txt_live.config(state=tk.NORMAL)
            self.txt_live.delete("1.0", tk.END)
            if self._live_partial and self._listening_active:
                body = (body + " " + self._live_partial).strip()
                self.txt_live.insert("1.0", body)
                self.txt_live.config(fg=self.text_secondary)
            else:
                self.txt_live.insert("1.0", body or LIVE_PLACEHOLDER)
                self.txt_live.config(fg=self.text_primary)
            # Auto-scroll to the end so the latest (right-most) words of a long
            # transcript are visible instead of being hidden above the viewport.
            self.txt_live.see(tk.END)
            self.txt_live.config(state=tk.DISABLED)

            self._render_history()
            self._apply_send_state()
        except Exception as e:
            print(f"[UI] render live failed: {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # Desktop Chrome (Phase 5.5): timer, features panel, mini, move, zoom,
    # transparency, Shift+Alt shortcuts, clear, scroll, sys-audio state.
    # ─────────────────────────────────────────────────────────────────────────

    def _start_timer(self):
        if self._timer_running:
            return
        self._timer_running = True
        self._timer_started_at = time.monotonic()
        self._tick_timer()

    def _stop_timer(self):
        self._timer_running = False
        if self._timer_after_id is not None:
            try:
                self.root.after_cancel(self._timer_after_id)
            except Exception:
                pass
            self._timer_after_id = None
        # Leave the last value visible.

    def _tick_timer(self):
        self._timer_after_id = None
        if not self._timer_running:
            return
        secs = max(0, int(time.monotonic() - self._timer_started_at))
        mm, ss = divmod(secs, 60)
        hh, mm = divmod(mm, 60)
        text = f"⏱ {hh:02d}:{mm:02d}:{ss:02d}" if hh else f"⏱ {mm:02d}:{ss:02d}"
        try:
            if not self.lbl_timer.winfo_exists():
                self._timer_running = False
                return
            self.lbl_timer.config(text=text)
        except Exception:
            self._timer_running = False
            return
        try:
            self._timer_after_id = self.root.after(1000, self._tick_timer)
        except Exception:
            self._timer_after_id = None
            self._timer_running = False

    def toggle_features(self):
        self._features_open = not self._features_open
        if self._features_panel is None:
            return
        try:
            if self._features_open:
                self._features_panel.pack(fill=tk.BOTH, expand=True,
                                          padx=14, pady=(0, 10))
                self.btn_features.config(text="▥ Close Panel")
            else:
                self._features_panel.pack_forget()
                self.btn_features.config(text="▤ Features")
        except Exception as e:
            print(f"[UI] features toggle failed: {e}")
        self._refresh_window_shape()

    def _sync_sys_audio_selector(self):
        try:
            if not hasattr(self, "cmb_sys_audio"):
                return
            vals = ([self._loop_name] if getattr(self, "_loop_name", None)
                    else ["<unavailable>"])
            self.cmb_sys_audio["values"] = vals
            cur = getattr(self, "_loop_choice", None) or vals[0]
            if cur in vals:
                self.cmb_sys_audio.set(cur)
            if getattr(self, "_loop_name", None):
                self.cmb_sys_audio.state(["!disabled"])
            else:
                self.cmb_sys_audio.state(["disabled"])
        except Exception as e:
            print(f"[UI] sys-audio selector sync failed: {e}")

    def _on_sys_audio_selected(self, _e=None):
        sel = self.cmb_sys_audio.get()
        if sel and "unavailable" not in sel and "<none>" not in sel:
            self._loop_choice = sel
            if getattr(self, "_last_loop", None):
                self.loopback_device = self._last_loop
        else:
            self._loop_choice = None
            self.loopback_device = None

    def _render_sys_audio_state(self):
        def cb():
            try:
                active = bool(getattr(self, "_loopback_active", False))
                color = self.success_color if active else self.text_secondary
                if hasattr(self, "lbl_sysled") and self.lbl_sysled.winfo_exists():
                    self.lbl_sysled.config(fg=color)
                if hasattr(self, "lbl_syslive") and self.lbl_syslive.winfo_exists():
                    self.lbl_syslive.config(
                        text=("System audio: LISTENING"
                              if active else "System audio: idle"),
                        fg=self.success_color if active else self.text_secondary)
            except Exception:
                pass
        self._safe_after(cb)

    def clear_inputs(self, _e=None):
        try:
            if self.entry_manual.winfo_exists():
                self.entry_manual.delete(0, tk.END)
                self.entry_manual.insert(0, ASK_PLACEHOLDER)
                self.entry_manual.config(fg=self.text_secondary)
        except Exception:
            pass
        self._live_transcript = ""
        self._live_partial = ""
        try:
            if hasattr(self, "_listening_panel") and self._listening_panel is not None \
                    and self._listening_panel.winfo_exists():
                self._render_live()
        except Exception:
            pass
        print("[UI] Inputs cleared")

    def focus_ask(self, _e=None):
        if not self._manual_mode:
            self._switch_tab(True)
        try:
            if self.entry_manual.winfo_exists():
                self.entry_manual.focus_set()
        except Exception:
            pass

    def _scroll_answer(self, delta):
        try:
            if self.ans_canvas.winfo_exists():
                self.ans_canvas.yview_scroll(delta, "units")
        except Exception:
            pass

    def _move_window(self, dx, dy):
        try:
            x = max(0, self.root.winfo_x() + dx)
            y = max(0, self.root.winfo_y() + dy)
            self.root.geometry(f"+{x}+{y}")
            self.root.update_idletasks()
            # Defer the frame re-assert until AFTER the WM commits the move:
            # SetWindowPos(SWP_FRAMECHANGED) issued while a geometry change is
            # still in flight can make Windows snap the tool-window frame back
            # to its stored placement rect.
            try:
                self.root.after_idle(
                    lambda: self._apply_stealth_window_styles(reason="move"))
            except Exception:
                pass
        except Exception as e:
            print(f"[UI] move failed: {e}")

    def _begin_resize(self, e):
        self._resize_origin = (e.x_root, e.y_root,
                               self.root.winfo_width(), self.root.winfo_height())

    def _drag_resize(self, e):
        origin = getattr(self, "_resize_origin", None)
        if not origin:
            return
        x0, y0, w0, h0 = origin
        w = max(int(640 * self._ui_scale), w0 + (e.x_root - x0))
        h = max(int(420 * self._ui_scale), h0 + (e.y_root - y0))
        try:
            self.root.geometry(f"{w}x{h}")
        except Exception:
            pass

    def _end_resize(self, _e=None):
        self._resize_origin = None

    # Named size presets — the overlay stays non-user-resizable; these are the
    # only explicit resize affordances (plus the bottom-right drag grip).
    _SIZE_FACTORS = {"compact": 0.78, "normal": 1.0, "large": 1.35}

    def set_panel_size(self, preset: str):
        """Resize the overlay to a named preset (compact / normal / large).

        The window is deliberately non-user-resizable, so this is the managed
        resize path used by the Settings dialog. Keeps `_large_panel` in sync so
        the Shift+Alt zoom toggle starts from the right state.
        """
        if preset not in self._SIZE_FACTORS:
            preset = "normal"
        f = self._SIZE_FACTORS[preset]
        w = max(int(640 * self._ui_scale), int(DESIGN_WIDTH * f * self._ui_scale))
        h = max(int(420 * self._ui_scale), int(DESIGN_HEIGHT * f * self._ui_scale))
        try:
            self.root.geometry(f"{w}x{h}")
            self._size_preset = preset
            self._large_panel = (preset == "large")
            # Re-fit the fixed-size widgets to the new preset/window so nothing is
            # clipped when the panel gets smaller (Compact).
            self._fit_bars()
            # Commit the rounded region + frame styles at the NEW size now so
            # the debounced <Configure> handler never re-applies them against
            # a stale placement mid-resize.
            if self._region_after is not None:
                try:
                    self.root.after_cancel(self._region_after)
                except Exception:
                    pass
                self._region_after = None
            self.root.update_idletasks()
            self._refresh_window_shape()
        except Exception as e:
            print(f"[UI] set_panel_size failed: {e}")
        self._sync_size_controls()

    def _fit_bars(self):
        """Re-fit fixed-size row widgets to the current window size.

        Called from ``set_panel_size`` (preset changes) and the device
        population path. The live-transcript row already expands, but the
        system-audio combobox carries a fixed char width that goes stale when the
        window shrinks — without re-fitting it the row overflows and the trailing
        controls (SEND / Clear) get squeezed to zero width again.
        """
        try:
            preset = getattr(self, "_size_preset", "normal") or "normal"
            combo_chars = AUDIO_COMBO_CHARS.get(preset, AUDIO_COMBO_CHARS["normal"])
            try:
                self.cmb_sys_audio.configure(width=combo_chars)
            except Exception:
                pass
        except Exception as e:
            print(f"[UI] _fit_bars failed: {e}")

    def _sync_size_controls(self):
        """Highlight the active size-preset button in the Settings dialog."""
        for name, b in (getattr(self, "_size_btns", None) or {}).items():
            try:
                if not b.winfo_exists():
                    continue
                active = (name == self._size_preset)
                b.config(bg=self.accent_color if active else self.card_bg,
                         fg=self.accent_ink if active else self.text_primary)
            except Exception:
                pass

    def toggle_zoom(self):
        """Programmatic resize preset (window stays non-user-resizable)."""
        self.set_panel_size("normal" if self._large_panel else "large")

    def _adjust_alpha(self, delta):
        try:
            cur = float(self.root.attributes("-alpha") or 1.0)
        except Exception:
            cur = 1.0
        nv = max(0.35, min(1.0, cur + delta))
        try:
            self.root.attributes("-alpha", nv)
        except Exception:
            pass
        self._set_alpha_label(nv)

    def _reset_alpha(self):
        try:
            self.root.attributes("-alpha", 1.0)
        except Exception:
            pass
        self._set_alpha_label(1.0)

    def _set_alpha_label(self, nv):
        try:
            if hasattr(self, "alpha_scale") and self.alpha_scale.winfo_exists():
                self.alpha_scale.set(nv)
            if hasattr(self, "lbl_alpha") and self.lbl_alpha.winfo_exists():
                self.lbl_alpha.config(text=f"{int(round(nv * 100))}%")
        except Exception:
            pass

    def _on_alpha(self, v):
        try:
            nv = max(0.35, min(1.0, float(v)))
            self.root.attributes("-alpha", nv)
        except Exception:
            return
        try:
            if hasattr(self, "lbl_alpha") and self.lbl_alpha.winfo_exists():
                self.lbl_alpha.config(text=f"{int(round(nv * 100))}%")
        except Exception:
            pass

    def toggle_mini(self, _e=None):
        if self._mini_win is not None and self._mini_win.winfo_exists():
            self._restore_mini()
        else:
            self._collapse_to_mini()

    def _collapse_to_mini(self):
        try:
            self._mini_pos = (max(0, self.root.winfo_x()), max(0, self.root.winfo_y()))
        except Exception:
            self._mini_pos = (80, 80)
        try:
            self.root.withdraw()
        except Exception:
            return
        try:
            mini = tk.Toplevel(self.root)
            mini.overrideredirect(True)
            mini.geometry(f"56x56+{self._mini_pos[0]}+{self._mini_pos[1]}")
            mini.configure(bg=self.card_bg)
            mini.attributes("-topmost", True)
            btn = tk.Button(
                mini, text="K",
                bg=self.card_bg, fg=self.success_color,
                font=(self.font_family, _fs(15), "bold"), relief=tk.FLAT,
                borderwidth=0, highlightthickness=0, cursor="hand2",
                activebackground=self.card_bg, activeforeground=self.success_color,
            )
            btn.pack(fill=tk.BOTH, expand=True)
            # Click (no drag) restores the overlay; drag repositions the bubble.
            btn.bind("<Button-1>", self._mini_drag_start)
            btn.bind("<B1-Motion>", self._mini_drag_move)
            btn.bind("<ButtonRelease-1>", self._mini_drag_end)
            self._mini_win = mini
            self._mini_btn = btn
            self.root.after(20, self._finalize_mini)
        except Exception as e:
            print(f"[UI] mini collapse failed: {e}")
            try:
                self.root.deiconify()
            except Exception:
                pass

    def _finalize_mini(self):
        try:
            if self._mini_win is None or not self._mini_win.winfo_exists():
                return
            hwnd = toplevel_hwnd(self._mini_win.winfo_id()) \
                if sys.platform == "win32" else None
            if not hwnd:
                return
            rect = ctypes.wintypes.RECT()
            if not ctypes.windll.user32.GetWindowRect(
                    ctypes.wintypes.HWND(hwnd), ctypes.byref(rect)):
                return
            w = rect.right - rect.left
            h = rect.bottom - rect.top
            if w <= 0 or h <= 0:
                return
            rgn = ctypes.windll.gdi32.CreateRoundRectRgn(0, 0, w + 1, h + 1, 28, 28)
            ctypes.windll.user32.SetWindowRgn(hwnd, rgn, True)
        except Exception:
            pass

    def _restore_mini(self, _e=None):
        try:
            if self._mini_win is not None and self._mini_win.winfo_exists():
                self._mini_win.destroy()
        except Exception:
            pass
        self._mini_win = None
        try:
            self.root.deiconify()
            self.root.attributes("-topmost", True)
            if self._mini_pos is not None:
                self.root.geometry(f"+{self._mini_pos[0]}+{self._mini_pos[1]}")
            self._apply_stealth_window_styles(reason="mini-restore")
        except Exception as e:
            print(f"[UI] mini restore failed: {e}")

    def _mini_drag_start(self, e):
        """Record the grab point so a release can tell a click from a drag."""
        self._mini_drag_moved = False
        try:
            self._mini_drag_origin = (
                e.x_root, e.y_root,
                self._mini_win.winfo_x(), self._mini_win.winfo_y(),
            )
        except Exception:
            self._mini_drag_origin = None

    def _mini_drag_move(self, e):
        """Reposition the floating bubble while dragging it."""
        o = getattr(self, "_mini_drag_origin", None)
        if not o or self._mini_win is None or not self._mini_win.winfo_exists():
            return
        x0, y0, wx, wy = o
        dx, dy = e.x_root - x0, e.y_root - y0
        if abs(dx) > 3 or abs(dy) > 3:
            self._mini_drag_moved = True
        try:
            self._mini_win.geometry(f"+{max(0, wx + dx)}+{max(0, wy + dy)}")
        except Exception:
            pass

    def _mini_drag_end(self, e=None):
        """A click (no movement) restores the overlay; a drag just moves it."""
        if getattr(self, "_mini_drag_moved", False):
            try:
                self._mini_pos = (max(0, self._mini_win.winfo_x()),
                                  max(0, self._mini_win.winfo_y()))
            except Exception:
                pass
        else:
            self._restore_mini()
        self._mini_drag_origin = None

    def _sync_hist_controls(self, has_prev: bool, has_next: bool):
        """Keep manual-row + features-panel history buttons aligned."""
        try:
            for btn in ("btn_mhist_up", "btn_features_hist_prev"):
                w = getattr(self, btn, None)
                if w is not None and w.winfo_exists():
                    w.config(state=tk.NORMAL if has_prev else tk.DISABLED)
            for btn in ("btn_mhist_down", "btn_features_hist_next"):
                w = getattr(self, btn, None)
                if w is not None and w.winfo_exists():
                    w.config(state=tk.NORMAL if has_next else tk.DISABLED)
        except Exception:
            pass

    def _ui_set_status(self, text: str, ok: bool = True):
        """Update status label."""
        def cb():
            color = self.success_color if ok else self.danger_color
            self.lbl_status.config(text=text, fg=color)
            try:
                self.status_dot.config(fg=color)
            except Exception:
                pass
            self._assistant_state = "ready" if ok else "offline"

        self._safe_after(cb)

    def _ui_update_question(self, q: str):
        """Display detected question."""
        self._current_question = q or ""
        if self._current_question:
            self._latest_detected = self._current_question

        def cb():
            self._set_live_text(self._current_question or LIVE_PLACEHOLDER)

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
                self._viewing_history = False
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
                                # First non-empty chunk of the new answer:
                                # swap out the previous answer IN PLACE (no
                                # container rebuild, no blank panel).
                                txt.config(state=tk.NORMAL)
                                txt.delete("1.0", "end")
                                self._answer_replace_pending = False
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
        (Approach/Code/Key Points/…) are destroyed here. The container itself
        is never rebuilt, so the panel geometry stays stable.

        Interview Listening Mode (§12): the PREVIOUS answer stays on screen
        through SEND and retrieval — it is replaced in place when the first
        non-empty chunk of the new answer arrives, so the user never sees a
        blank panel while the model is thinking."""
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

        # Keep the old answer visible; defer the swap to the first chunk.
        self._answer_replace_pending = False
        if alive and self._listening_mode:
            try:
                if txt.get("1.0", "end-1c").strip():
                    self._answer_replace_pending = True
                    self._answer_scroll_pinned = True
                    return
            except Exception:
                pass

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
            if self._listening_mode:
                # Interview mode never blanks the Answer panel (§19): if no
                # chunk arrived the previous answer is still showing, and if
                # one did it is the best answer we have.
                self._stream_buffer = ""
                self._cancel_answer_auto_hide()
                self._answer_replace_pending = False
                self._paint_dot(False)
                return
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
            self._answer_replace_pending = False
            if self._listening_mode:
                # Interview mode: the answer must stay readable for the next
                # question — no auto-hide (§12). Legacy behaviour unchanged.
                self.answer_auto_hide_task = None
                print("[UI] Answer auto-hide suppressed (interview mode)")
                self._print_ui_perf()
                return
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
        """Settings entry — window & display + audio devices (compact)."""
        win = tk.Toplevel(self.root)
        win.title("Kiyra — Settings")
        win.geometry("480x460+120+120")
        win.configure(bg=self.bg_color)
        win.transient(self.root)
        win.grab_set()

        # ── Window & Display ────────────────────────────────────────────
        tk.Label(win, text="Window & Display", bg=self.bg_color, fg=self.text_primary,
                 font=(self.font_family, _fs(10), "bold")).pack(anchor="w", padx=12, pady=(12, 4))
        tk.Frame(win, bg=self.border_color, height=1).pack(fill=tk.X, padx=12, pady=(0, 8))

        size_row = tk.Frame(win, bg=self.bg_color)
        size_row.pack(fill=tk.X, padx=12, pady=(0, 8))
        tk.Label(size_row, text="Size:", bg=self.bg_color, fg=self.text_secondary,
                 font=(self.font_family, _fs(9)), width=11, anchor="w").pack(side=tk.LEFT)
        self._size_btns = {}
        for name, label in (("compact", "Compact"), ("normal", "Normal"), ("large", "Large")):
            b = self._btn(size_row, label, (lambda n=name: self.set_panel_size(n)),
                          bg=self.card_bg, font_size=9, padx=10, pady=3)
            b.pack(side=tk.LEFT, padx=(0, 6))
            self._size_btns[name] = b
        self._sync_size_controls()

        alpha_row = tk.Frame(win, bg=self.bg_color)
        alpha_row.pack(fill=tk.X, padx=12, pady=(0, 8))
        tk.Label(alpha_row, text="Opacity:", bg=self.bg_color, fg=self.text_secondary,
                 font=(self.font_family, _fs(9)), width=11, anchor="w").pack(side=tk.LEFT)
        try:
            cur_alpha = float(self.root.attributes("-alpha") or 1.0)
        except Exception:
            cur_alpha = 1.0
        cur_alpha = max(0.35, min(1.0, cur_alpha))
        dlg_lbl = tk.Label(alpha_row, text=f"{int(round(cur_alpha * 100))}%",
                           bg=self.bg_color, fg=self.text_secondary,
                           font=(self.font_family, _fs(9)), width=4)
        dlg_lbl.pack(side=tk.RIGHT)

        def _dlg_alpha(v):
            self._on_alpha(v)
            try:
                dlg_lbl.config(text=f"{int(round(float(v) * 100))}%")
            except Exception:
                pass

        def _dlg_reset():
            self._reset_alpha()
            try:
                dlg_lbl.config(text="100%")
            except Exception:
                pass

        scl = ttk.Scale(alpha_row, from_=0.35, to=1.0, orient=tk.HORIZONTAL,
                        length=190, command=_dlg_alpha)
        scl.set(cur_alpha)
        scl.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=8)
        self._btn(alpha_row, "Reset", _dlg_reset, bg=self.card_bg, font_size=8,
                  padx=8).pack(side=tk.RIGHT, padx=(6, 0))

        tk.Frame(win, bg=self.border_color, height=1).pack(fill=tk.X, padx=12, pady=(0, 8))

        # ── Audio Devices ───────────────────────────────────────────────
        tk.Label(win, text="Audio Devices", bg=self.bg_color, fg=self.text_primary,
                 font=(self.font_family, _fs(10), "bold")).pack(anchor="w", padx=12, pady=(0, 6))

        mic_list = getattr(self, "_mic_list", []) or ["<no microphones found>"]
        loop_vals = ([self._loop_name] if getattr(self, "_loop_name", None) else ["<unavailable>"])

        frm = tk.Frame(win, bg=self.bg_color)
        frm.pack(fill=tk.X, padx=12)
        tk.Label(frm, text="Microphone:", bg=self.bg_color, fg=self.text_secondary,
                 font=(self.font_family, _fs(9))).grid(row=0, column=0, sticky="w", pady=4)
        cmb_mic = ttk.Combobox(frm, values=mic_list, state="readonly", width=44)
        cmb_mic.set(getattr(self, "_mic_choice", "") or (mic_list[0] if mic_list else ""))
        cmb_mic.grid(row=0, column=1, padx=8, sticky="ew", pady=4)
        tk.Label(frm, text="System Audio:", bg=self.bg_color, fg=self.text_secondary,
                 font=(self.font_family, _fs(9))).grid(row=1, column=0, sticky="w", pady=4)
        cmb_loop = ttk.Combobox(frm, values=loop_vals, state="readonly", width=44)
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
        if not q or q == ASK_PLACEHOLDER:
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
        if not q or q in (LIVE_PLACEHOLDER, "Listening for the interviewer's question."):
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

        # Only a backend we launched ourselves is stopped, and only when asked
        # (default keeps it warm so the next `python stealth_overlay.py` is
        # instant).
        if self.backend_launcher is not None and _stop_backend_on_exit():
            try:
                self.backend_launcher.stop()
            except Exception:
                pass

        # Same rule for the model server we launched (releases GPU VRAM).
        if self.model_launcher is not None and _stop_backend_on_exit():
            try:
                self.model_launcher.stop()
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────────────────────
# Entry Point
# ─────────────────────────────────────────────────────────────────────────────

def _create_live_session(api_url: str,
                         job_description_id: Optional[int] = None) -> int:
    """Create a live interview session via REST and return its id."""
    url = f"{api_url}/api/interviews"
    payload = {
        "interview_type": "behavioral",
        "mode": "live",
        "difficulty": "medium",
        "question_count": 10,
        "title": "Live Stealth Session",
    }
    if job_description_id:
        payload["job_description_id"] = job_description_id
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        data = json.loads(resp.read())
    return data["id"]


def _stop_backend_on_exit() -> bool:
    """Whether to shut the auto-started backend down when the overlay closes.

    Default is False (keep it warm so the next launch connects instantly).
    """
    return os.environ.get("KIYRA_STOP_BACKEND_ON_EXIT", "").strip().lower() in (
        "1", "true", "yes", "on")


def _env_flag(name: str, default: bool) -> bool:
    """Truthy env var with an explicit default."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _ensure_backend(api_url: str, autostart: bool = True,
                    startup_timeout: float | None = None):
    """Make sure the backend is serving before the overlay opens.

    Reuses an already-running backend; otherwise launches it as a hidden child
    and waits for GET /health. Returns (launcher_or_None, ready, message).
    The launcher is returned ONLY when this process started the backend, so
    shutdown can never kill a backend the user started themselves.
    """
    launcher = BackendLauncher(
        api_url=api_url,
        **({"startup_timeout": startup_timeout} if startup_timeout else {}),
    )

    if not autostart:
        ok = launcher.is_running()
        return None, ok, (
            "backend already running" if ok
            else "--no-backend given and nothing answers on /health")

    ready, message = launcher.ensure()
    if not ready or not launcher.started_here:
        return None, ready, message
    return launcher, True, message


def _ensure_model_server(autostart: bool = True,
                         startup_timeout: float | None = None):
    """Make sure Bonsai's llama-server is serving before the session starts.

    Reuses an already-running model server; otherwise starts it (GPU-first with
    the proven CPU fallback) and waits for /v1/models. Returns
    (launcher_or_None, ready, message). The launcher is returned ONLY when this
    process started the server, so shutdown can never kill one the user started.
    """
    launcher = ModelServerLauncher(
        **({"startup_timeout": startup_timeout} if startup_timeout else {}))

    if not autostart:
        ok = launcher.is_running()
        return None, ok, (
            "model server already running" if ok
            else "--no-model given and nothing answers on /v1/models")

    ready, message = launcher.ensure()
    if not ready or not launcher.started_here:
        return None, ready, message
    return launcher, True, message


def _parse_args(argv):
    """Tiny flag parser — stdlib argparse is overkill for four switches."""
    opts = {
        "api_url": os.environ.get("KIYRA_API_URL", "http://localhost:8000"),
        "autostart": True,
        "model_autostart": True,
        "startup_timeout": None,
        "stop_on_exit": _stop_backend_on_exit(),
        # Interview Listening Mode (Phase 5.4): the user, not the VAD, decides
        # when a question ends. Opt-in so the legacy auto-detect overlay and the
        # web client keep their current behaviour.
        "listening_mode": _env_flag("KIYRA_INTERVIEW_LISTENING", True),
        "auto_start_listening": _env_flag("KIYRA_AUTO_START_LISTENING", True),
        # Start collapsed into the 56x56 bubble so the overlay can listen and
        # answer without covering the desktop (click the bubble to expand).
        "start_hidden": _env_flag("KIYRA_OVERLAY_HIDDEN", False),
        # Skip the pre-flight setup wizard (sign in → job description →
        # instructions). Hide mode always skips it so a background run never
        # pops a window over the desktop.
        "skip_setup": _env_flag("KIYRA_SKIP_SETUP", False),
        # Force the wizard open even in hide mode (explicit --setup).
        "force_setup": False,
    }
    # Drop the script path when argv came from sys.argv (argv[0] always ends
    # in .py; no real flag does).
    args = [a for a in argv if not a.lower().endswith(".py")]
    i = 0
    while i < len(args):
        flag = args[i].lower()
        nxt = args[i + 1] if i + 1 < len(args) else ""
        if flag in ("--api-url", "-a") and nxt:
            opts["api_url"] = nxt
            i += 2
        elif flag == "--no-backend":
            opts["autostart"] = False
            i += 1
        elif flag in ("--no-model", "--no-bonsai"):
            opts["model_autostart"] = False
            i += 1
        elif flag in ("--startup-timeout", "-t") and nxt:
            opts["startup_timeout"] = float(nxt)
            i += 2
        elif flag == "--stop-backend-on-exit":
            opts["stop_on_exit"] = True
            i += 1
        elif flag in ("--keep-backend", "-k"):
            opts["stop_on_exit"] = False
            i += 1
        elif flag == "--auto-detect":
            opts["listening_mode"] = False
            opts["auto_start_listening"] = False
            i += 1
        elif flag == "--no-auto-start-listening":
            opts["auto_start_listening"] = False
            i += 1
        elif flag in ("--hidden", "--bubble", "--start-hidden"):
            opts["start_hidden"] = True
            i += 1
        elif flag in ("--skip-setup", "--no-setup"):
            opts["skip_setup"] = True
            opts["force_setup"] = False
            i += 1
        elif flag == "--setup":
            opts["skip_setup"] = False
            opts["force_setup"] = True
            i += 1
        elif flag in ("-h", "--help"):
            print("usage: python stealth_overlay.py [--api-url URL] "
                  "[--no-backend] [--no-model] "
                  "[--startup-timeout SECONDS] "
                  "[--stop-backend-on-exit | --keep-backend] "
                  "[--auto-detect] [--no-auto-start-listening] "
                  "[--hidden] [--skip-setup | --setup]")
            sys.exit(0)
        else:
            print(f"[Args] ignoring unknown argument: {args[i]}")
            i += 1
    return opts


def main(argv=None):
    # Windows consoles default to the legacy code page (cp1252); printing a
    # non-ASCII transcript char then raises UnicodeEncodeError and tears down
    # whatever callback is printing. Force UTF-8 with replacement bytes so a
    # foreign-language transcript can never kill the UI loop.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    opts = _parse_args(list(sys.argv if argv is None else argv))
    api_url = opts["api_url"].rstrip("/")

    # 0. Model server: Bonsai's llama-server. Started first because session
    #    start fails fast with a 503 when it is down, which used to surface as
    #    "listening never starts". Best-effort: a missing model must not stop
    #    the overlay from opening (someone may run a different provider).
    if not opts["model_autostart"]:
        model_launcher = ModelServerLauncher()
        model_ready = model_launcher.is_running()
        print(f"[Model] autostart disabled; "
              f"{'running' if model_ready else 'NOT reachable'}")
    else:
        print("[Model] checking llama-server …")
        model_launcher, model_ready, model_message = _ensure_model_server(
            autostart=True, startup_timeout=opts["startup_timeout"])
        print(f"[Model] {model_message}")
        if not model_ready:
            print("[Model] WARNING: inference will fail until llama-server is "
                  "up. Start it manually with:\n"
                  "            python llama_server.py\n"
                  "          (or pass --no-model to silence this)")

    # 1. Backend: reuse if healthy, otherwise start it and wait for /health.
    if not opts["autostart"]:
        launcher = BackendLauncher(api_url=api_url)
        ready = launcher.is_running()
        print(f"[Backend] autostart disabled; "
              f"{'running' if ready else 'NOT reachable'} at {api_url}")
    else:
        print(f"[Backend] checking {api_url} …")
        launcher, ready, message = _ensure_backend(
            api_url, autostart=True,
            startup_timeout=opts["startup_timeout"])
        print(f"[Backend] {message}")

    if not ready:
        print(f"[Backend] startup failed — the overlay needs a live backend.\n"
              f"          Fix the cause above, or run it yourself:\n"
              f"            cd backend && .\\.venv\\Scripts\\python.exe "
              f"-m uvicorn main:app --port {launcher.port if launcher else 8000}\n"
              f"          Log: {launcher.log_path if launcher else ''}")
        return 1

    # 2. Pre-flight setup: sign in → job description → instructions. Hide mode
    #    and automation skip it so a background run never pops a window over
    #    the desktop.
    root = tk.Tk()
    root.withdraw()
    from setup_wizard import run_setup_wizard
    setup = run_setup_wizard(
        root, api_url,
        skip=opts["skip_setup"] or (opts["start_hidden"]
                                    and not opts["force_setup"]),
        force=opts["force_setup"])
    if setup is None:
        print("[Setup] Cancelled — exiting.")
        try:
            root.destroy()
        except Exception:
            pass
        return 1
    print(f"[Setup] user={setup.get('username')} "
          f"jd={setup.get('job_description_id')}")

    # 3. Live session (retry briefly: /health can go green a moment before the
    #    database accepts writes).
    session_id = 0
    last_err = None
    for attempt in range(5):
        try:
            session_id = _create_live_session(
                api_url, job_description_id=setup.get("job_description_id"))
            print(f"[Session] Created session {session_id}")
            break
        except Exception as e:
            last_err = e
            if attempt < 4:
                time.sleep(1.0)
    if not session_id:
        print(f"[Session] Could not auto-create session: {last_err} "
              f"— defaulting to id=1")
        session_id = 1

    # 4. Overlay (its WebSocket loop auto-reconnects if the link blips).
    app = StealthOverlayApp(
        root, api_url=api_url, session_id=session_id,
        backend_launcher=launcher if opts["stop_on_exit"] else None,
        model_launcher=model_launcher if opts["stop_on_exit"] else None,
        listening_mode=opts["listening_mode"],
        auto_start_listening=opts["auto_start_listening"])
    if opts.get("start_hidden"):
        # Collapse into the 56x56 bubble right after the window paints, so the
        # overlay runs (listening + answering) without covering the desktop.
        # A click on the bubble restores the full panel.
        root.after(500, app._collapse_to_mini)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
