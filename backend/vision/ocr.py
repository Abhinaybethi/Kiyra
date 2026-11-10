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


def _contrast_enhanced(img: np.ndarray) -> np.ndarray:
    """Histogram-equalize a capture so OCR can read faint/washed-out text.

    Dark-theme apps, HDR/tonemapped screens and low-contrast UIs can produce
    text the detector misses at raw contrast (measured: a grey-on-grey
    screenshot yields 0 boxes raw but both lines after equalization). Returns
    the boosted image in BGR, ready for the engine.
    """
    import cv2
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    equalized = cv2.equalizeHist(gray)
    return cv2.cvtColor(equalized, cv2.COLOR_GRAY2BGR)


def _parse_result(result) -> dict:
    """Normalize a RapidOCR result into text + metadata ({} when empty)."""
    if not result:
        return {}
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
        return {}
    raw = "\n".join(lines)
    return {
        "text": raw,
        "line_count": len(lines),
        "avg_confidence": round(confidence_sum / len(lines), 3),
        "likely_code": looks_like_code(raw),
    }


def extract_text(image_bytes: bytes) -> dict:
    """OCR an image. Returns extracted text + metadata.

    Tries the raw capture first; if the detector finds nothing, retries once on
    a contrast-equalized copy so low-contrast / washed-out (dark theme, HDR)
    screenshots do not fail with a false "No text detected". Raises
    RuntimeError when OCR is unavailable, the image cannot be decoded, or no
    text is found even after enhancement — callers surface that as a
    recoverable error.
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
    parsed = _parse_result(result)
    if not parsed:
        try:
            result, _elapse = engine(_contrast_enhanced(img))
        except Exception:
            result = None
        parsed = _parse_result(result)

    if not parsed:
        raise RuntimeError("No text detected in the image (OCR returned empty).")
    return parsed


def extract_question(text: str) -> str:
    """Pull the first question-like sentence from OCR'd text, else return text.

    OCR engines often return ONE box per word, so the joined text is
    newline-separated words ("Write\\nNode.js\\na\\nprogram..."). Flatten first,
    then look for the question sentence — otherwise the analyzer would receive
    a newline-smashed question that degrades Bonsai's answer format.
    """
    if not text:
        return ""
    flat = re.sub(r"\s+", " ", text.strip())
    if not flat:
        return ""
    if flat.endswith("?") and len(flat) > 4:
        return flat
    sentences = re.split(r"(?<=[.!?])\s+", flat)
    sentence = next(
        (s.strip() for s in sentences if s.strip().endswith("?") and len(s.strip()) > 4),
        None,
    )
    return sentence or flat[:400]