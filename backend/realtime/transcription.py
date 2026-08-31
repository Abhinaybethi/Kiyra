"""faster-whisper transcription service.

Loads model once and transcribes audio chunks.
Falls back gracefully if faster-whisper is unavailable.
"""
from __future__ import annotations

import io
import tempfile
import os
import wave
from dataclasses import dataclass
from typing import Optional

from config import settings


@dataclass
class TranscriptionResult:
    text: str
    language: str = "en"
    duration: float = 0.0
    segments: list = None


_whisper_model = None


def _get_model():
    global _whisper_model
    if _whisper_model is None:
        try:
            from faster_whisper import WhisperModel
            model_size = settings.transcription_model  # base, small, medium, large
            # ponytail: using cpu by default, set device="cuda" in env if GPU available
            _whisper_model = WhisperModel(
                model_size,
                device="cpu",
                compute_type="int8",  # memory efficient
            )
        except ImportError:
            raise RuntimeError("faster-whisper not installed. Run: uv add faster-whisper")
        except Exception as e:
            raise RuntimeError(f"Failed to load Whisper model '{settings.transcription_model}': {e}")
    return _whisper_model


def transcribe_audio_bytes(audio_bytes: bytes, audio_format: str = "wav") -> TranscriptionResult:
    """Transcribe raw audio bytes. audio_format: wav or webm."""
    model = _get_model()

    # Write to temp file (faster-whisper needs a file path)
    suffix = f".{audio_format}"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(audio_bytes)
        tmp_path = tmp.name

    try:
        segments, info = model.transcribe(
            tmp_path,
            beam_size=5,
            language="en",
            vad_filter=True,  # filter silence
        )
        text_parts = []
        segment_list = []
        duration = 0.0

        for seg in segments:
            text_parts.append(seg.text.strip())
            segment_list.append({
                "start": seg.start,
                "end": seg.end,
                "text": seg.text.strip(),
            })
            duration = max(duration, seg.end)

        return TranscriptionResult(
            text=" ".join(text_parts).strip(),
            language=info.language,
            duration=duration,
            segments=segment_list,
        )
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def is_available() -> bool:
    """Check if whisper model can be loaded."""
    try:
        _get_model()
        return True
    except Exception:
        return False


def detect_question(text: str) -> tuple[bool, float]:
    """
    Heuristic question detection.
    Returns (is_question, confidence).
    Not perfect — real speaker diarization requires additional models.
    """
    if not text or len(text.strip()) < 10:
        return False, 0.0

    text_lower = text.lower().strip()

    # Strong question signals
    question_words = ["what", "how", "why", "when", "where", "who", "which", "can you", "could you",
                      "tell me", "describe", "explain", "walk me through", "have you", "do you",
                      "would you", "are you", "did you", "have you ever", "give me an example"]

    score = 0.0

    # Ends with question mark
    if text.rstrip().endswith("?"):
        score += 0.5

    # Starts with question word
    for qw in question_words:
        if text_lower.startswith(qw):
            score += 0.4
            break

    # Contains question word somewhere
    for qw in question_words[:10]:
        if qw in text_lower:
            score += 0.1
            break

    # Reasonable length for a question (not too short, not a monologue)
    word_count = len(text.split())
    if 5 <= word_count <= 80:
        score += 0.1

    confidence = min(score, 1.0)
    return confidence >= 0.5, confidence
