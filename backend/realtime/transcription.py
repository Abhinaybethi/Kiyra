"""faster-whisper transcription service — Optimized for real-time performance."""
from __future__ import annotations

import io
import os
import threading
import time
import wave
import hashlib
from dataclasses import dataclass, field
from typing import Optional

from config import settings


@dataclass
class TranscriptionResult:
    """Transcription result with metadata."""
    text: str
    language: str = "en"
    duration: float = 0.0
    segments: list = None
    confidence: float = 0.95
    latency_ms: int = 0


_whisper_model = None
_model_lock = threading.Lock()      # serializes inference calls
_model_load_lock = threading.Lock()  # guards one-time model loading only
_transcription_cache = {}
_transcription_cache_ttl = 30.0  # seconds — evict stale cached results
_transcription_hits = 0


def _get_model():
    """Get or create Whisper model (singleton) — loaded exactly once.

    Uses a *separate* load lock from `_model_lock`: callers that already hold
    `_model_lock` (via `transcribe_with_lock`) must be able to reach this
    without deadlocking on a non-reentrant lock.
    """
    global _whisper_model
    if _whisper_model is None:
        with _model_load_lock:
            if _whisper_model is None:
                try:
                    from faster_whisper import WhisperModel
                    model_size = settings.transcription_model  # base, small, medium, large
                    device = "cuda" if _has_gpu() else "cpu"
                    compute_type = "int8"  # Fast quantized inference
                    _whisper_model = WhisperModel(
                        model_size,
                        device=device,
                        compute_type=compute_type,
                        download_root=os.path.join(settings.data_dir, "models"),
                    )
                    print(f"[Whisper] Loaded {model_size} model on {device} with {compute_type}")
                except ImportError:
                    raise RuntimeError("faster-whisper not installed. Run: uv add faster-whisper")
                except Exception as e:
                    raise RuntimeError(f"Failed to load Whisper model '{settings.transcription_model}': {e}")
    return _whisper_model


def _has_gpu() -> bool:
    """Check if CUDA is available."""
    try:
        import torch
        return torch.cuda.is_available()
    except Exception:
        return False


def _wav_duration(audio_bytes: bytes) -> float:
    """Read duration from a WAV header without decoding the payload."""
    try:
        with wave.open(io.BytesIO(audio_bytes), "rb") as wf:
            return wf.getnframes() / max(wf.getframerate(), 1)
    except Exception:
        return 0.0


def transcribe_audio_bytes(
    audio_bytes: bytes, audio_format: str = "wav", fast: bool = False
) -> TranscriptionResult:
    """
    Transcribe raw audio bytes with caching and serialized model access.

    Args:
        audio_bytes: Raw audio data (WAV by default)
        audio_format: "wav" or "webm"
        fast: Skip word timestamps (partial/speculative path)

    Returns:
        TranscriptionResult with text, duration, latency, etc.
    """
    started = time.monotonic()
    model = _get_model()

    d = _wav_duration(audio_bytes)
    if d <= 0:
        return TranscriptionResult(text="", duration=0.0)

    # Cache key: hash of audio bytes
    cache_key = hashlib.md5(audio_bytes).hexdigest()
    global _transcription_cache, _transcription_hits
    if cache_key in _transcription_cache:
        result = _transcription_cache[cache_key]
        result.latency_ms = 0
        _transcription_hits += 1
        return result

    # faster-whisper accepts seekable BinaryIO — decode fully in memory.
    try:
        segments_stream, info = _transcribe(model, io.BytesIO(audio_bytes), fast=fast)
    except Exception:
        # Fallback: old path — write to a temp file for the decoder.
        import tempfile
        suffix = f".{audio_format}"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name
        try:
            segments_stream, info = _transcribe(model, tmp_path, fast=fast)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    text_parts = []
    segment_list = []
    duration = 0.0
    confidence_sum = 0.0
    segment_count = 0

    for seg in segments_stream:
        text_parts.append(seg.text.strip())
        segment_list.append({
            "start": seg.start,
            "end": seg.end,
            "text": seg.text.strip(),
            "confidence": getattr(seg, "confidence", 0.95),
        })
        duration = max(duration, seg.end)
        confidence_sum += getattr(seg, "confidence", 0.95)
        segment_count += 1

    avg_confidence = confidence_sum / segment_count if segment_count > 0 else 0.95

    latency_ms = int((time.monotonic() - started) * 1000)

    result = TranscriptionResult(
        text=" ".join(text_parts).strip(),
        language=getattr(info, "language", "en"),
        duration=duration,
        segments=segment_list,
        confidence=min(avg_confidence, 1.0),
        latency_ms=latency_ms,
    )

    # Cache result (bounded)
    if len(_transcription_cache) >= 100:
        _transcription_cache.pop(next(iter(_transcription_cache)))
    _transcription_cache[cache_key] = result

    return result


def _transcribe(model, audio_input, fast: bool = False):
    """Greedy, low-latency transcription with silence filtering.

    `fast=True` skips per-word timestamps (`without_timestamps`) — strictly
    cheaper for the speculative/partial path where text-only is enough.
    """
    return model.transcribe(
        audio_input,
        beam_size=1,  # Greedy decoding — significantly faster than beam 5
        best_of=1,
        language="en",
        vad_filter=True,  # faster-whisper's built-in Silero VAD (filters silence in-chunk)
        vad_parameters={"min_silence_duration_ms": 350,
                        "threshold": 0.5,
                        "min_speech_duration_ms": 200},
        condition_on_previous_text=False,  # Reduce hallucination on short utterances
        no_speech_threshold=0.6,
        without_timestamps=fast,  # Skip word-timestamps on the speculative path
    )


def transcribe_with_lock(
    audio_bytes: bytes, audio_format: str = "wav", fast: bool = False
) -> TranscriptionResult:
    """Transcribe while serializing access to the shared Whisper model.

    faster-whisper is not built for truly concurrent transcribe calls on one
    model instance; serializing keeps latency predictable and prevents crashes
    when multiple sessions stream audio simultaneously.
    """
    with _model_lock:
        return transcribe_audio_bytes(audio_bytes, audio_format=audio_format, fast=fast)


def is_available() -> bool:
    """Check if Whisper model can be loaded."""
    try:
        _get_model()
        return True
    except Exception as e:
        print(f"Whisper not available: {e}")
        return False