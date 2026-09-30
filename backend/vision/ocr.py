"""Visual input pipeline: OCR extraction from explicitly uploaded images.

Privacy-first: images are only processed when the user explicitly uploads /
selects them (never captured silently). RapidOCR runs fully locally via
ONNX — no cloud OCR calls.

If the OCR engine is unavailable the module reports `is_available() == False`
and callers return a clear, recoverable error rather than a fake result.
"""
from __future__ import annotations

import io
import os
import re
from typing import Optional

import numpy as np

_engine = None
_engine_checked = False


def is_available() -> bool:
    return _get_engine() is not None


def _get_engine():
    global _engine, _engine_checked
    if _engine_checked:
        return _engine
    _engine_checked = True
    try:
        from rapidocr_onnxruntime import RapidOCR  # type: ignore
        _engine = RapidOCR()
    except Exception as e:
        print(f"[OCR] Engine unavailable: {e}")
        _engine = None
    return _engine


def _decode_image(image_bytes: bytes) -> Optional[np.ndarray]:
    """Decode image bytes into a BGR numpy array (as RapidOCR/Maintains expected)."""
    if not image_bytes:
        return None
    arr = np.frombuffer(image_bytes, np.uint8)
    import cv2
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    return img if img is not None else None


# Heuristic language detection for code screenshots (fast, no model needed).
_CODE_HINTS = re.compile(
    r"(?i)\b(function|def|class|import|return|const|let|var|if\s*\(|print|"
    r"\bstd::|=>|->|\{\s*\}|null|nullptr|self\.|\bint\b|\bvoid\b)\b|"
    r"[{};()]+\s*[{};()]"
)


def looks_like_code(text: str) -> bool:
    if not text:
        return False
    code_lines = sum(1 for line in text.splitlines() if _CODE_HINTS.search(line))
    return code_lines >= 2 or code_lines / max(len(text.splitlines()), 1) > 0.4


def extract_text(image_bytes: bytes) -> dict:
    """OCR an image. Returns extracted text + metadata.

    Raises RuntimeError when OCR is unavailable or extraction fails — callers
    surface that as a recoverable error.
    """
    engine = _get_engine()
    if engine is None:
        raise RuntimeError(
            "OCR engine is not available. Install: pip install rapidocr-onnxruntime"
        )

    img = _decode_image(image_bytes)
    if img is None:
        raise RuntimeError("Could not decode image. Use PNG/JPEG/WebP/BMP.")

    # RapidOCR consumes a numpy image; run on CPU and normalize output shapes.
    result, _elapse = engine(img)
    if not result:
        raise RuntimeError("No text detected in the image (OCR returned empty).")

    lines = []
    confidence_sum = 0.0
    for item in result:
        # item: [box, text, score]
        if len(item) < 3:
            continue
        text = str(item[1]).strip()
        if not text:
            continue
        lines.append(text)
        try:
            confidence_sum += float(item[2])
        except (TypeError, ValueError):
            pass

    if not lines:
        raise RuntimeError("No text detected in the image (OCR returned empty).")

    raw = "\n".join(lines)
    return {
        "text": raw,
        "line_count": len(lines),
        "avg_confidence": round(confidence_sum / len(lines), 3),
        "likely_code": looks_like_code(raw),
    }


def extract_question(text: str) -> str:
    """Pull the first question-like sentence from OCR'd text, else return text."""
    if not text:
        return ""
    for line in text.splitlines():
        line = line.strip()
        if line.endswith("?") and len(line) > 4:
            return line
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    sentence = next((s.strip() for s in sentences if s.strip().endswith("?") and len(s.strip()) > 4), None)
    return sentence or (text.strip()[:400] if text.strip() else "")