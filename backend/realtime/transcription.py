"""faster-whisper transcription service — Optimized for real-time performance."""
from __future__ import annotations

import io
import tempfile
import os
import wave
import hashlib
from dataclasses import dataclass
from typing import Optional
from functools import lru_cache

from config import settings


@dataclass
class TranscriptionResult:
    """Transcription result with metadata."""
    text: str
    language: str = "en"
    duration: float = 0.0
    segments: list = None
    confidence: float = 0.95


_whisper_model = None
_transcription_cache = {}


def _get_model():
    """Get or create Whisper model (singleton)."""
    global _whisper_model
    if _whisper_model is None:
        try:
            from faster_whisper import WhisperModel
            model_size = settings.transcription_model  # base, small, medium, large
            
            # Optimized for real-time: base model, CPU (can use GPU if available)
            # Use int8 quantization for speed and memory efficiency
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


def transcribe_audio_bytes(audio_bytes: bytes, audio_format: str = "wav") -> TranscriptionResult:
    """
    Transcribe raw audio bytes with caching.
    
    Args:
        audio_bytes: Raw audio data
        audio_format: "wav" or "webm"
    
    Returns:
        TranscriptionResult with text, duration, etc.
    """
    model = _get_model()

    # Cache key: hash of audio bytes
    cache_key = hashlib.md5(audio_bytes).hexdigest()
    if cache_key in _transcription_cache:
        return _transcription_cache[cache_key]

    # Write to temp file (faster-whisper needs a file path)
    suffix = f".{audio_format}"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(audio_bytes)
        tmp_path = tmp.name

    try:
        segments, info = model.transcribe(
            tmp_path,
            beam_size=5,  # Balance speed/accuracy
            language="en",
            vad_filter=True,  # Filter silence (saves compute)
            condition_on_previous_text=False,  # Reduce hallucination
        )
        
        text_parts = []
        segment_list = []
        duration = 0.0
        confidence_sum = 0.0
        segment_count = 0

        for seg in segments:
            text_parts.append(seg.text.strip())
            segment_list.append({
                "start": seg.start,
                "end": seg.end,
                "text": seg.text.strip(),
                "confidence": getattr(seg, "confidence", 0.95)
            })
            duration = max(duration, seg.end)
            confidence_sum += getattr(seg, "confidence", 0.95)
            segment_count += 1

        avg_confidence = confidence_sum / segment_count if segment_count > 0 else 0.95
        
        result = TranscriptionResult(
            text=" ".join(text_parts).strip(),
            language=info.language,
            duration=duration,
            segments=segment_list,
            confidence=min(avg_confidence, 1.0)
        )
        
        # Cache result
        _transcription_cache[cache_key] = result
        
        # Limit cache size
        if len(_transcription_cache) > 100:
            _transcription_cache.pop(next(iter(_transcription_cache)))
        
        return result
        
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def is_available() -> bool:
    """Check if Whisper model can be loaded."""
    try:
        _get_model()
        return True
    except Exception as e:
        print(f"Whisper not available: {e}")
        return False


def detect_question(text: str) -> tuple[bool, float]:
    """
    Heuristic question detection using linguistic patterns.
    
    Returns:
        (is_question: bool, confidence: float 0.0-1.0)
    """
    if not text or len(text.strip()) < 5:
        return False, 0.0

    text_lower = text.lower().strip()
    
    # Question signal patterns (ordered by strength)
    question_starts = [
        ("what", 0.4),
        ("how", 0.4),
        ("why", 0.3),
        ("when", 0.3),
        ("where", 0.3),
        ("who", 0.3),
        ("which", 0.3),
        ("can you", 0.35),
        ("could you", 0.35),
        ("would you", 0.35),
        ("tell me", 0.3),
        ("describe", 0.25),
        ("explain", 0.25),
        ("walk me through", 0.4),
        ("have you", 0.2),
        ("do you", 0.2),
        ("did you", 0.2),
        ("are you", 0.2),
    ]
    
    score = 0.0
    
    # Check ending with question mark (strongest signal)
    if text.rstrip().endswith("?"):
        score += 0.5
    
    # Check starts with question word
    for qw, weight in question_starts:
        if text_lower.startswith(qw):
            score += weight
            break
    
    # Check contains question word (weaker)
    for qw, weight in question_starts[:5]:  # Top question words
        if qw in text_lower and not text_lower.startswith(qw):
            score += 0.05
            break
    
    # Length heuristic: questions are typically 10-100 words
    word_count = len(text.split())
    if 5 <= word_count <= 100:
        score += 0.1
    elif word_count > 100:
        score -= 0.2  # Likely a monologue/statement
    
    # No multiple sentences (questions are usually single sentence)
    sentence_count = len([s for s in text.split('.') if s.strip()])
    if sentence_count > 2:
        score -= 0.15
    
    confidence = min(max(score, 0.0), 1.0)
    
    # Threshold for detection
    return confidence >= 0.55, confidence
