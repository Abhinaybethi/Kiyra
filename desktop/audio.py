"""Audio utilities for Stealth Overlay.

Provides WAV encoding and simple resampling/mixing helpers used by the desktop overlay
and unit tests.
"""
from __future__ import annotations

import io
import math
import wave
from typing import List

import numpy as np


def pcm16_from_float32(arr: np.ndarray) -> np.ndarray:
    """Convert float32 in -1..1 to int16 PCM."""
    if arr.dtype != np.float32:
        arr = arr.astype(np.float32)
    clipped = np.clip(arr, -1.0, 1.0)
    return (clipped * 32767.0).astype(np.int16)


def resample_linear(x: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    """Simple linear resampling using numpy.interp. Works for small buffers."""
    if src_sr == dst_sr:
        return x
    if x.size == 0:
        return x.astype(np.float32)
    duration = x.shape[0] / float(src_sr)
    dst_n = int(math.ceil(duration * dst_sr))
    if dst_n <= 0:
        return np.zeros(0, dtype=np.float32)
    src_times = np.linspace(0, duration, num=x.shape[0], endpoint=False)
    dst_times = np.linspace(0, duration, num=dst_n, endpoint=False)
    return np.interp(dst_times, src_times, x).astype(np.float32)


def mix_mono(channels: List[np.ndarray]) -> np.ndarray:
    """Mix multiple mono numpy arrays by padding to same length and averaging."""
    if not channels:
        return np.zeros(0, dtype=np.float32)
    max_len = max(arr.shape[0] for arr in channels)
    mix = np.zeros(max_len, dtype=np.float32)
    for arr in channels:
        if arr.shape[0] < max_len:
            padded = np.pad(arr, (0, max_len - arr.shape[0]))
        else:
            padded = arr
        mix += padded
    mix /= float(len(channels))
    return mix


def encode_wav_bytes(samples: np.ndarray, samplerate: int = 16000) -> bytes:
    """Encode a mono int16 numpy array as a WAV file in memory and return bytes."""
    # Ensure int16
    if samples.dtype != np.int16:
        raise ValueError("encode_wav_bytes expects int16 samples")
    bio = io.BytesIO()
    with wave.open(bio, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(samplerate)
        wf.writeframes(samples.tobytes())
    return bio.getvalue()


def duration_seconds_from_frames(frame_count: int, samplerate: int) -> float:
    return frame_count / float(samplerate)
