"""Screenshot capture for the Kiyra overlay (Windows-first).

Captures the full virtual desktop across every monitor in one shot. Prefers
PIL.ImageGrab (already installed in the desktop environment); if Pillow is
unavailable it falls back to a dependency-free GDI BitBlt that emits a BMP.

The overlay hides itself (temporary-safe hide) BEFORE calling ``capture`` so
Kiyra never appears in its own screenshot; ``capture`` itself still snapshots
whatever is on screen at call time. All image I/O happens in memory — callers
decide whether to persist a temporary debug copy.
"""
from __future__ import annotations

import io
import struct
import sys
import time
from dataclasses import dataclass, field
from typing import Optional


try:
    from PIL import ImageGrab
except Exception:  # pragma: no cover - environment-dependent
    ImageGrab = None


@dataclass
class ScreenshotInput:
    """The raw capture: image bytes + metadata, before any OCR."""
    image_bytes: bytes
    mime: str = "image/png"
    image_path: Optional[str] = None
    extracted_text: str = ""
    source: str = "screenshot"
    confidence: float = 0.0
    timestamp: float = field(default_factory=time.time)


@dataclass
class QuestionInput:
    """The structured question that enters the SAME pipeline as voice/manual.

    ``raw_text`` carries the full OCR output so the caller can debug or honor
    low-confidence text without pasting it blindly into the LLM.
    """
    question: str
    source: str = "screenshot"
    raw_text: str = ""
    confidence: float = 0.0
    likely_code: bool = False
    timestamp: float = field(default_factory=time.time)


class ScreenshotCapture:
    """Full-virtual-screen capture with a PIL preference + GDI fallback.

    Not thread-bound by itself, but the overlay runs it on a worker thread so
    the Tkinter main thread never blocks on screen grab or OCR.
    """

    def __init__(self, prefer_pil: bool = True):
        self.prefer_pil = prefer_pil

    def capture(self) -> ScreenshotInput:
        if self.prefer_pil and ImageGrab is not None:
            try:
                return self._capture_pil()
            except Exception as e:
                print(f"[SCREENSHOT] PIL capture failed ({e}); using GDI fallback")
        return self._capture_gdi_bmp()

    # ── PIL path ────────────────────────────────────────────────────────────
    def _capture_pil(self) -> ScreenshotInput:
        image = ImageGrab.grab(all_screens=True)
        bio = io.BytesIO()
        image.save(bio, format="PNG")
        data = bio.getvalue()
        print(
            "[SCREENSHOT] PIL captured size=%dB %dx%d"
            % (len(data), image.width, image.height)
        )
        return ScreenshotInput(image_bytes=data, mime="image/png")

    # ── GDI fallback (dependency-free, emits a BMP) ─────────────────────────
    def _capture_gdi_bmp(self) -> ScreenshotInput:
        if sys.platform != "win32":
            raise RuntimeError("GDI screenshot capture requires Windows")
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32

        SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
        SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
        x = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
        y = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        w = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
        h = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
        if w <= 0 or h <= 0:
            raise RuntimeError("No screen area available for capture")

        SRCCOPY = 0x00CC0020
        hdc_screen = user32.GetDC(0)
        try:
            hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
            hbmp = gdi32.CreateCompatibleBitmap(hdc_screen, w, h)
            old = gdi32.SelectObject(hdc_mem, hbmp)
            try:
                ok = gdi32.BitBlt(hdc_mem, 0, 0, w, h, hdc_screen, x, y, SRCCOPY)
                if not ok:
                    raise OSError("BitBlt screen capture failed")
                # Top-down DIB (negative height) so row 0 is the first pixel.
                bmi = struct.pack(
                    "<IiiHHIIiiII",
                    40, w, -h, 1, 32, 0, 0, 0, 0, 0, 0,
                )
                raw = ctypes.create_string_buffer(w * h * 4)
                lines = gdi32.GetDIBits(
                    hdc_mem, hbmp, 0, h, raw, bmi, 0,
                )
                if lines != h:
                    raise OSError(f"GetDIBits returned {lines} of {h} rows")
                header = struct.pack(
                    "<2sIHHI", b"BM", 54 + len(raw.raw), 0, 0, 54,
                )
                data = header + bmi + raw.raw
            finally:
                gdi32.SelectObject(hdc_mem, old)
                gdi32.DeleteObject(hbmp)
                gdi32.DeleteDC(hdc_mem)
        finally:
            user32.ReleaseDC(0, hdc_screen)
        print(
            "[SCREENSHOT] GDI captured %dB %dx%d (%d,%d)" % (len(data), w, h, x, y)
        )
        return ScreenshotInput(image_bytes=data, mime="image/bmp")