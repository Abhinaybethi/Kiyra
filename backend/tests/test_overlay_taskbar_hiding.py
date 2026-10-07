"""Phase 5.2A tests: the overlay is a TOOL window — no taskbar button, no Alt+Tab.

Windows window-shell verification only. These tests assert the actual extended
styles on a REAL hwnd (via GetWindowLongPtrW), so they do not rely on visual
taskbar inspection. No audio, backend, WebSocket or screenshot is involved.

TEST 1  style bit math        — TOOLWINDOW on, APPWINDOW off, all else kept
TEST 2  64-bit safety         — LONG_PTR is pointer-sized, LongPtr API bound
TEST 3  hwnd resolution       — Tk client -> top-level shell window, valid
TEST 4  TASKBAR STYLE         — real window: TOOLWINDOW active, APPWINDOW gone
TEST 5  ALT+TAB STYLE         — shell reports no app-window / no alt-tab entry
TEST 6  topmost preserved     — always-on-top survives the restyle
TEST 7  geometry preserved    — 560x640 fixed window, SWP_NOMOVE|SWP_NOSIZE
TEST 8  alpha/LAYERED kept    — -alpha (WS_EX_LAYERED) survives the restyle
TEST 9  TRANSPARENT kept      — click-through bit is never stripped
TEST 10 affinity compatible  — WDA_EXCLUDEFROMCAPTURE still sticks afterwards
TEST 11 screenshot flow      — withdraw/deiconify keeps the tool-window style
TEST 12 non-Windows is a no-op
"""
from __future__ import annotations

import ctypes
import sys
import time
from pathlib import Path

import pytest

DESKTOP_DIR = Path(__file__).resolve().parents[2] / "desktop"
sys.path.insert(0, str(DESKTOP_DIR))

import stealth_overlay as so  # noqa: E402

IS_WIN = sys.platform == "win32"
needs_win = pytest.mark.skipif(not IS_WIN, reason="Windows window-shell only")


def _pump(root, rounds: int = 10):
    for _ in range(rounds):
        root.update()
        time.sleep(0.01)


class _ShellApp(so.StealthOverlayApp):
    """Real overlay window, audio device discovery stubbed out."""

    def _populate_devices(self):
        pass


@pytest.fixture(scope="module")
def overlay(tk_root):
    if not IS_WIN:
        pytest.skip("Windows window-shell only")
    root = tk_root
    app = _ShellApp(root, api_url="http://127.0.0.1:9", session_id=0)
    _pump(root)
    # Let the real _realize_window chain run (after(60): deiconify, alpha,
    # styles, region, capture-affinity).
    for _ in range(14):
        _pump(root, rounds=3)
        time.sleep(0.02)
    yield app
    for child in list(root.winfo_children()):
        try:
            child.destroy()
        except Exception:
            pass


def _shell_hwnd(app):
    return so.toplevel_hwnd(app.root.winfo_id())


# ── TEST 1: style bit math ───────────────────────────────────────────────────

def test_stealth_exstyle_sets_toolwindow_and_clears_appwindow():
    assert so.stealth_exstyle(0) & so.WS_EX_TOOLWINDOW
    assert not so.stealth_exstyle(so.WS_EX_APPWINDOW) & so.WS_EX_APPWINDOW


def test_stealth_exstyle_preserves_every_other_bit():
    keep = (so.WS_EX_LAYERED | so.WS_EX_TRANSPARENT | so.WS_EX_TOPMINDOW
            | 0x00000100)
    out = so.stealth_exstyle(keep | so.WS_EX_APPWINDOW)
    assert out & keep == keep                      # alpha/click-through/topmost
    assert out & so.WS_EX_TOOLWINDOW
    assert not out & so.WS_EX_APPWINDOW


def test_stealth_exstyle_is_idempotent():
    once = so.stealth_exstyle(so.WS_EX_APPWINDOW | so.WS_EX_LAYERED)
    assert so.stealth_exstyle(once) == once


def test_named_constants_are_not_magic_numbers():
    assert so.GWL_EXSTYLE == -20
    assert so.WS_EX_TOOLWINDOW == 0x00000080
    assert so.WS_EX_APPWINDOW == 0x00040000
    assert (so.SWP_NOMOVE, so.SWP_NOSIZE, so.SWP_NOACTIVATE,
            so.SWP_FRAMECHANGED) == (0x0002, 0x0001, 0x0010, 0x0020)
    assert so.HWND_TOPMOST == -1


# ── TEST 2: 64-bit safety ────────────────────────────────────────────────────

@needs_win
def test_long_ptr_is_pointer_sized():
    """c_long is 4 bytes on Win64 — using it would truncate the 64-bit
    LONG_PTR signature the LongPtr APIs require."""
    assert ctypes.sizeof(so.LONG_PTR) == ctypes.sizeof(ctypes.c_void_p)
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        assert ctypes.sizeof(ctypes.c_long) == 4      # the unsafe alternative


@needs_win
def test_window_long_ptr_api_bound_with_explicit_signature():
    """Explicit signatures matter: ctypes' default restype is c_int (32-bit),
    which would be the unsafe 32-bit assumption on Win64."""
    get_fn, set_fn = so._window_style_api()
    for fn in (get_fn, set_fn):
        assert fn.restype is so.LONG_PTR        # pointer-sized return
    assert len(get_fn.argtypes) == 2            # hwnd, index
    assert len(set_fn.argtypes) == 3            # hwnd, index, value
    assert ctypes.sizeof(so.LONG_PTR) == 8 or ctypes.sizeof(ctypes.c_void_p) == 4
    # A full 32-bit extended-style value must be representable in the declared
    # value type (GWL_EXSTYLE is a DWORD field, so its high DWORD is always 0).
    assert int(so.LONG_PTR(0xFFFFFFFF).value) == 0xFFFFFFFF
    assert so.LONG_PTR(0xFFFFFFFF).value >= 0        # not sign-extended


@needs_win
def test_get_exstyle_roundtrip_is_64bit_safe(overlay):
    hwnd = _shell_hwnd(overlay)
    before = so.get_window_exstyle(hwnd)
    so.set_window_exstyle(hwnd, before | so.WS_EX_TOOLWINDOW)
    assert so.get_window_exstyle(hwnd) & so.WS_EX_TOOLWINDOW


# ── TEST 3: hwnd resolution ──────────────────────────────────────────────────

@needs_win
def test_toplevel_hwnd_is_valid_and_differs_from_tk_client(overlay):
    client = overlay.root.winfo_id()
    shell = _shell_hwnd(overlay)
    assert shell, "no top-level hwnd resolved"
    assert ctypes.windll.user32.IsWindow(ctypes.wintypes.HWND(shell))
    # Tk hands back the CLIENT window; the shell is its top-level ancestor.
    assert shell != client


@needs_win
def test_read_window_shell_flags_reports_valid_window(overlay):
    flags = so.read_window_shell_flags(_shell_hwnd(overlay))
    assert flags["valid"] is True
    assert flags["hwnd"] == _shell_hwnd(overlay)


@needs_win
def test_read_window_shell_flags_on_null_handle():
    flags = so.read_window_shell_flags(0)
    assert flags["valid"] is False
    assert flags["taskbar_button"] is False


# ── TEST 4: TASKBAR STYLE (the actual goal) ──────────────────────────────────

@needs_win
def test_toolwindow_active_and_appwindow_removed(overlay):
    flags = so.read_window_shell_flags(_shell_hwnd(overlay))
    assert flags["toolwindow"] is True, f"WS_EX_TOOLWINDOW missing: {flags}"
    assert flags["appwindow"] is False, f"WS_EX_APPWINDOW still set: {flags}"
    assert flags["taskbar_button"] is False


@needs_win
def test_apply_returns_verified_flags(overlay):
    flags = overlay._apply_stealth_window_styles(reason="test")
    assert flags is not None
    assert flags["toolwindow"] and not flags["appwindow"]


@needs_win
def test_appwindow_bit_is_actively_removed(overlay):
    """Force the bad style on, then prove the helper clears it."""
    hwnd = _shell_hwnd(overlay)
    so.set_window_exstyle(hwnd, so.get_window_exstyle(hwnd) | so.WS_EX_APPWINDOW)
    assert so.read_window_shell_flags(hwnd)["appwindow"] is True

    overlay._apply_stealth_window_styles(reason="test-clear")
    flags = so.read_window_shell_flags(hwnd)
    assert flags["appwindow"] is False
    assert flags["toolwindow"] is True


# ── TEST 5: ALT+TAB STYLE ────────────────────────────────────────────────────

@needs_win
def test_not_an_alt_tab_entry(overlay):
    flags = so.read_window_shell_flags(_shell_hwnd(overlay))
    assert flags["alt_tab_entry"] is False
    assert flags["appwindow"] is False     # tool window, not an app window


# ── TEST 6: topmost ──────────────────────────────────────────────────────────

@needs_win
def test_topmost_remains_active(overlay):
    overlay.root.attributes("-topmost", True)
    overlay._apply_stealth_window_styles(reason="test-topmost")
    assert bool(overlay.root.attributes("-topmost")) is True   # Tk returns 1
    # WS_EX_TOPMOST lives in the exstyle too; Tk sets it via -topmost.
    _pump(overlay.root)
    overlay._apply_stealth_window_styles(reason="test-topmost-2")
    flags = so.read_window_shell_flags(_shell_hwnd(overlay))
    assert flags["topmost"] is True


# ── TEST 7: geometry preserved ───────────────────────────────────────────────

@needs_win
def test_window_geometry_unchanged_by_styling(overlay):
    overlay.root.update_idletasks()
    rect = ctypes.wintypes.RECT()
    ctypes.windll.user32.GetWindowRect(
        ctypes.wintypes.HWND(_shell_hwnd(overlay)), ctypes.byref(rect))
    geom_before = overlay.root.geometry()
    size_before = (rect.right - rect.left, rect.bottom - rect.top)

    overlay._apply_stealth_window_styles(reason="test-geometry")
    overlay.root.update_idletasks()

    rect2 = ctypes.wintypes.RECT()
    ctypes.windll.user32.GetWindowRect(
        ctypes.wintypes.HWND(_shell_hwnd(overlay)), ctypes.byref(rect2))
    assert (rect2.left, rect2.top) == (rect.left, rect.top), "window moved"
    assert (rect2.right - rect2.left, rect2.bottom - rect2.top) == size_before, \
        "window resized"
    assert overlay.root.geometry() == geom_before


@needs_win
def test_fixed_560x640_logical_size_preserved(overlay):
    """Design size is 560x640 logical px, scaled by DPI. Tk's geometry() sizes
    the CLIENT area, so the shell window is that plus frame decorations."""
    overlay.root.update_idletasks()
    expected = (int(560 * overlay._ui_scale), int(640 * overlay._ui_scale))
    assert (overlay.root.winfo_width(), overlay.root.winfo_height()) == expected

    overlay._apply_stealth_window_styles(reason="test-size")
    overlay.root.update_idletasks()
    assert (overlay.root.winfo_width(), overlay.root.winfo_height()) == expected

    # The shell rect must be identical before and after the restyle.
    shell = _shell_hwnd(overlay)
    r1 = ctypes.wintypes.RECT()
    ctypes.windll.user32.GetWindowRect(ctypes.wintypes.HWND(shell),
                                       ctypes.byref(r1))
    overlay._apply_stealth_window_styles(reason="test-size-2")
    r2 = ctypes.wintypes.RECT()
    ctypes.windll.user32.GetWindowRect(ctypes.wintypes.HWND(shell),
                                       ctypes.byref(r2))
    assert (r1.left, r1.top, r1.right, r1.bottom) == \
           (r2.left, r2.top, r2.right, r2.bottom)


@needs_win
def test_window_remains_resizable_false(overlay):
    assert overlay.root.resizable() == (False, False)


# ── TEST 8: alpha / WS_EX_LAYERED preserved ──────────────────────────────────

@needs_win
def test_layered_alpha_style_survives_styling(overlay):
    overlay.root.attributes("-alpha", 0.98)
    _pump(overlay.root)
    hwnd = _shell_hwnd(overlay)
    overlay._apply_stealth_window_styles(reason="test-alpha")
    flags = so.read_window_shell_flags(hwnd)
    # -alpha must keep working: Tk keeps WS_EX_LAYERED on the shell window.
    assert overlay.root.attributes("-alpha") == 0.98
    assert flags["layered"] is True, f"alpha style lost: {flags}"
    assert flags["toolwindow"] is True


# ── TEST 9: WS_EX_TRANSPARENT preserved ─────────────────────────────────────

@needs_win
def test_click_through_bit_is_never_stripped(overlay):
    """F10 click-through currently toggles a label; if it ever sets
    WS_EX_TRANSPARENT natively, the shell styling must not remove it."""
    hwnd = _shell_hwnd(overlay)
    so.set_window_exstyle(hwnd, so.get_window_exstyle(hwnd)
                          | so.WS_EX_TRANSPARENT)
    overlay._apply_stealth_window_styles(reason="test-transparent")
    flags = so.read_window_shell_flags(hwnd)
    assert flags["transparent"] is True
    assert flags["toolwindow"] is True
    # ...and it is never added permanently by the styling itself.
    so.set_window_exstyle(hwnd, so.get_window_exstyle(hwnd)
                          & ~so.WS_EX_TRANSPARENT)
    overlay._apply_stealth_window_styles(reason="test-transparent-2")
    assert so.read_window_shell_flags(hwnd)["transparent"] is False


@needs_win
def test_f10_click_through_behavior_unchanged(overlay):
    before = overlay.is_click_through
    overlay.toggle_click_through()
    assert overlay.is_click_through is (not before)
    assert "Click-through: ON" in overlay.lbl_capture.cget("text")
    overlay.toggle_click_through()
    assert overlay.is_click_through is before
    assert "Click-through: OFF" in overlay.lbl_capture.cget("text")


# ── TEST 10: WDA_EXCLUDEFROMCAPTURE compatibility ────────────────────────────

WDA_EXCLUDEFROMCAPTURE = 17


@needs_win
def test_capture_exclusion_still_active_after_styling(overlay):
    overlay._apply_stealth_affinity()
    _pump(overlay.root)
    overlay._apply_stealth_window_styles(reason="test-affinity")
    cur = ctypes.c_uint32(0)
    applied = False
    for hwnd in {overlay.root.winfo_id(),
                 ctypes.windll.user32.GetParent(overlay.root.winfo_id()),
                 _shell_hwnd(overlay)}:
        if not hwnd:
            continue
        ctypes.windll.user32.GetWindowDisplayAffinity(
            ctypes.wintypes.HWND(hwnd), ctypes.byref(cur))
        if cur.value == WDA_EXCLUDEFROMCAPTURE:
            applied = True
            break
    assert applied, "WDA_EXCLUDEFROMCAPTURE lost after restyle"


@needs_win
def test_affinity_and_styles_coexist_on_same_shell_window(overlay):
    overlay._apply_stealth_window_styles(reason="test-coexist")
    overlay._apply_stealth_affinity()
    flags = so.read_window_shell_flags(_shell_hwnd(overlay))
    assert flags["toolwindow"] is True
    cur = ctypes.c_uint32(0)
    ctypes.windll.user32.GetWindowDisplayAffinity(
        ctypes.wintypes.HWND(_shell_hwnd(overlay)), ctypes.byref(cur))
    assert cur.value == WDA_EXCLUDEFROMCAPTURE


# ── TEST 11: screenshot hide/restore flow ────────────────────────────────────

@needs_win
def test_screenshot_flow_keeps_toolwindow_style(overlay):
    """Screenshot temp-hides (withdraw) then deiconifies; afterwards the taskbar
    must still be free of Kiyra."""
    overlay.root.withdraw()
    _pump(overlay.root)
    overlay._restore_overlay_after_capture()
    for _ in range(8):
        _pump(overlay.root, rounds=3)

    flags = so.read_window_shell_flags(_shell_hwnd(overlay))
    assert flags["toolwindow"] is True
    assert flags["appwindow"] is False
    assert flags["taskbar_button"] is False
    assert bool(overlay.root.attributes("-topmost")) is True


@needs_win
def test_f9_hide_show_keeps_toolwindow_style(overlay):
    overlay.toggle_visibility()          # hide
    _pump(overlay.root)
    overlay.toggle_visibility()          # show
    _pump(overlay.root)
    flags = so.read_window_shell_flags(_shell_hwnd(overlay))
    assert flags["toolwindow"] is True
    assert flags["appwindow"] is False


@needs_win
def test_refresh_window_shape_reasserts_toolwindow(overlay):
    hwnd = _shell_hwnd(overlay)
    so.set_window_exstyle(hwnd, so.get_window_exstyle(hwnd)
                          | so.WS_EX_APPWINDOW)
    overlay._refresh_window_shape()
    flags = so.read_window_shell_flags(hwnd)
    assert flags["toolwindow"] is True
    assert flags["appwindow"] is False


# ── TEST 12: non-Windows guard ───────────────────────────────────────────────

def test_apply_is_noop_off_windows(monkeypatch, overlay=None):
    monkeypatch.setattr(sys, "platform", "linux")
    fake = type("A", (), {"root": type("R", (), {
        "winfo_id": lambda s: 0})()})()
    assert so.StealthOverlayApp._apply_stealth_window_styles(fake) is None
