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

# Dedicated lane for the low-priority partial/speculative STT path.
#
# The encoder cost of a `base` model is a *fixed* ~1.7s per call regardless of
# clip length, and the partial loop fires every ~1.2s of speech — so a single
# shared model is saturated during speech and the authoritative *final*
# transcription ends up queued behind an in-flight partial (measured ~1.9s).
# A separate model instance with its own inference lock means the final never
# blocks on a preview call; the two only ever share CPU (12 cores here), never
# a lock. Fewer threads on this lane keeps it from starving the final.
_partial_model = None
_partial_model_load_lock = threading.Lock()
_partial_infer_lock = threading.Lock()  # serializes partial-lane inference

_transcription_cache = {}
_transcription_cache_ttl = 30.0  # seconds — evict stale cached results
_transcription_hits = 0


def _build_model(cpu_threads: int):
    """Construct a WhisperModel from the local cache, falling back to a fetch.

    faster-whisper contacts the Hugging Face Hub on construction; when the
    machine is offline that check retries with backoff for MINUTES, stalling
    the warmup thread and every STT call queued behind the model lock (the
    "listening latency" bug). local_files_only loads the cached snapshot
    instantly; we only fall back to a network fetch on a first-ever run.
    """
    from faster_whisper import WhisperModel
    model_size = settings.transcription_model  # base, small, medium, large
    device = "cuda" if _has_gpu() else "cpu"
    common = dict(
        device=device,
        compute_type="int8",  # Fast quantized inference
        download_root=os.path.join(settings.data_dir, "models"),
        cpu_threads=cpu_threads,
    )
    try:
        model = WhisperModel(model_size, local_files_only=True, **common)
        print(f"[Whisper] Loaded {model_size} model on {device} with int8 "
              f"(cached, cpu_threads={cpu_threads})")
    except Exception:
        model = WhisperModel(model_size, **common)
        print(f"[Whisper] Loaded {model_size} model on {device} with int8 "
              f"(downloaded, cpu_threads={cpu_threads})")
    return model


def _get_model():
    """Get or create the authoritative (final) Whisper model.

    Uses a *separate* load lock from `_model_lock`: callers that already hold
    `_model_lock` (via `transcribe_with_lock`) must be able to reach this
    without deadlocking on a non-reentrant lock.
    """
    global _whisper_model
    if _whisper_model is None:
        with _model_load_lock:
            if _whisper_model is None:
                try:
                    # Pin CPU threads: faster-whisper's default (0) leaves the
                    # encoder severely under-parallelised on Windows — measurably
                    # ~2.6s vs ~1.8s per call for the base model. 4-8 threads is
                    # the sweet spot; never oversubscribe beyond 8. The top of
                    # that range also keeps the final fast when a partial is
                    # sharing the CPU.
                    _whisper_model = _build_model(
                        min(8, max(4, os.cpu_count() or 8)))
                except ImportError:
                    raise RuntimeError("faster-whisper not installed. Run: uv add faster-whisper")
                except Exception as e:
                    raise RuntimeError(f"Failed to load Whisper model '{settings.transcription_model}': {e}")
    return _whisper_model


def _get_partial_model():
    """Get or create the low-priority partial/speculative Whisper model.

    A second instance of the same (cached) model with its own inference lock so
    a preview call can never delay the authoritative final transcription. Uses
    far fewer CPU threads so that when both lanes are busy the final keeps the
    bulk of the cores (measured: final 8 / partial 2 keeps the final within
    ~0.5s of its solo latency while a partial runs)."""
    global _partial_model
    if _partial_model is None:
        with _partial_model_load_lock:
            if _partial_model is None:
                threads = max(1, (os.cpu_count() or 8) // 6)
                _partial_model = _build_model(threads)
    return _partial_model


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
    audio_bytes: bytes, audio_format: str = "wav", fast: bool = False,
    model=None,
) -> TranscriptionResult:
    """
    Transcribe raw audio bytes with caching and serialized model access.

    Args:
        audio_bytes: Raw audio data (WAV by default)
        audio_format: "wav" or "webm"
        fast: Skip word timestamps (partial/speculative path)
        model: Model instance to use; defaults to the authoritative model. The
            partial lane passes its dedicated instance so the two never share
            a lock.

    Returns:
        TranscriptionResult with text, duration, latency, etc.
    """
    started = time.monotonic()
    if model is None:
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
    """Greedy, low-latency transcription.

    `fast=True` skips per-word timestamps (`without_timestamps`) — strictly
    cheaper for the speculative/partial path where text-only is enough.

    faster-whisper's own Silero ``vad_filter`` is deliberately OFF. The backend
    EnergyVAD (``realtime/audio.py``) already segments audio into
    speech-bounded utterances, so a second VAD inside Whisper is redundant —
    and actively harmful on real mic audio. A typical headset/webcam mic has a
    ~0.007 noise floor with speech peaks ~0.05-0.1; Silero scores such a clip
    on its decision boundary, marks the whole utterance "no speech" and returns
    ``''`` with no text at all — the "listening hears me but nothing is
    transcribed" bug (reproduced on a real 2.0s capture: rms 0.05, peaks 0.09).
    ``no_speech_threshold`` is kept to drop genuinely empty segments and
    ``condition_on_previous_text=False`` limits hallucination on short clips.
    """
    return model.transcribe(
        audio_input,
        beam_size=1,  # Greedy decoding — significantly faster than beam 5
        best_of=1,
        language="en",
        vad_filter=False,
        condition_on_previous_text=False,  # Reduce hallucination on short utterances
        no_speech_threshold=0.6,
        without_timestamps=fast,  # Skip word-timestamps on the speculative path
    )


def transcribe_with_lock(
    audio_bytes: bytes, audio_format: str = "wav", fast: bool = False
) -> TranscriptionResult:
    """Transcribe while serializing access to a Whisper model instance.

    faster-whisper is not built for truly concurrent transcribe calls on one
    model instance; serializing keeps latency predictable and prevents crashes
    when multiple sessions stream audio simultaneously.

    ``fast=True`` (the partial/speculative preview) runs on a *dedicated*
    model instance with its own lock, so a slow preview can never delay the
    authoritative final transcription — the measured "listening latency" was
    the final queued behind an in-flight partial on the shared model.
    """
    if fast:
        with _partial_infer_lock:
            return transcribe_audio_bytes(
                audio_bytes, audio_format=audio_format, fast=True,
                model=_get_partial_model())
    with _model_lock:
        return transcribe_audio_bytes(
            audio_bytes, audio_format=audio_format, fast=False,
            model=_get_model())


def is_available() -> bool:
    """Check if Whisper model can be loaded."""
    try:
        _get_model()
        return True
    except Exception as e:
        print(f"Whisper not available: {e}")
        return False