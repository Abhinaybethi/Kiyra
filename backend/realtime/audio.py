"""Voice Activity Detection and utterance segmentation for real-time audio.

Energy-based VAD: cheap, deterministic, no model downloads. Frames arrive as
16 kHz mono float32. `UtteranceReceiver` accumulates voiced audio, discards
silence, and finalizes complete utterances on trailing silence or a hard
duration cap.

Design rules:
  * The audio callback must never be blocked — ingestion here only enqueues.
  * Stale/overflow audio is dropped rather than allowed to grow unbounded.
  * No global mutable state — every session owns its own receiver.
"""
from __future__ import annotations

import io
import math
import time
import wave
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np


@dataclass
class Utterance:
    """A finalized block of speech, ready for STT."""
    audio: np.ndarray  # float32 mono @ 16 kHz
    rms: float
    start_ts: float
    duration: float
    speech_seconds: float = 0.0


class EnergyVAD:
    """Configurable energy-based voice activity detection."""

    def __init__(
        self,
        sample_rate: int = 16000,
        speech_start_threshold: float = 0.015,
        silence_rearm_threshold: float = None,  # hysteresis: stay voiced until lower
    ):
        self.sample_rate = sample_rate
        self.start_threshold = speech_start_threshold
        self.rearm_threshold = silence_rearm_threshold or (speech_start_threshold * 0.6)

    @staticmethod
    def rms(frame: np.ndarray) -> float:
        if frame.size == 0:
            return 0.0
        return float(np.sqrt(np.mean(np.square(frame.astype(np.float64)))))

    def is_speech(self, frame: np.ndarray, currently_voiced: bool) -> bool:
        """Speech state with hysteresis to avoid flapping on noisy boundaries."""
        rms = self.rms(frame)
        if currently_voiced:
            return rms >= self.rearm_threshold
        return rms >= self.start_threshold


class UtteranceReceiver:
    """Aggregates voiced frames into finalized utterances via trailing silence.

    Callbacks fire from the calling event loop thread — never from a loud
    audio callback (ingest via `push_frame` on the async pipeline).
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        speech_end_silence_ms: int = 300,
        minimum_speech_duration: float = 0.25,
        maximum_utterance_duration: float = 12.0,
        speech_start_threshold: float = 0.015,
        on_utterance: Optional[Callable[[Utterance], None]] = None,
    ):
        self.sample_rate = sample_rate
        self.end_silence_ms = speech_end_silence_ms
        self.min_speech = minimum_speech_duration
        self.max_speech = maximum_utterance_duration
        self.vad = EnergyVAD(sample_rate, speech_start_threshold=speech_start_threshold)
        self.on_utterance = on_utterance

        self._buffer = np.zeros(0, dtype=np.float32)
        self._voiced = False
        self._speech_started_at: Optional[float] = None
        self._last_voice_ts: Optional[float] = None
        self._speech_seconds = 0.0

    @property
    def is_voiced(self) -> bool:
        return self._voiced

    @property
    def buffered_seconds(self) -> float:
        return self._buffer.shape[0] / max(self.sample_rate, 1)

    @property
    def speech_seconds(self) -> float:
        """Accumulated voiced audio (excludes trailing silence)."""
        return self._speech_seconds

    def snapshot(self) -> np.ndarray:
        """Copy of the speech buffered *so far* — feeds the partial/speculative
        STT path without disturbing the live utterance state."""
        return self._buffer.copy()

    def reset(self):
        self._buffer = np.zeros(0, dtype=np.float32)
        self._voiced = False
        self._speech_started_at = None
        self._last_voice_ts = None
        self._speech_seconds = 0.0

    def push_frame(self, frame: np.ndarray, ts: float = None) -> Optional[Utterance]:
        """Feed a mono float32 frame. Returns a finalized utterance when ready.

        `ts` is the monotonic receive time used for end-of-speech detection.
        """
        now = time.monotonic() if ts is None else ts
        frame = frame.reshape(-1)
        frames_seconds = frame.size / max(self.sample_rate, 1)

        if frame.size == 0:
            return None

        rms = self.vad.rms(frame)
        speech = self.vad.is_speech(frame, self._voiced)

        # Hard cap — force-finalize very long utterances *even mid-speech* so
        # a candidate monologue keeps the pipeline streaming. Include the frame
        # that crosses the cap, then finalize.
        if self._voiced and self.buffered_seconds + frames_seconds >= self.max_speech:
            self._buffer = np.concatenate([self._buffer, frame]) if self._buffer.size else frame
            if speech:
                self._speech_seconds += frames_seconds
            self._last_voice_ts = now
            return self._finalize(now)

        if speech:
            if not self._voiced:
                self._voiced = True
                self._speech_started_at = now
            self._last_voice_ts = now
            self._buffer = np.concatenate([self._buffer, frame]) if self._buffer.size else frame
            self._speech_seconds += frames_seconds
            return None

        # Silence during speech: keep buffering until the trailing-silence
        # window elapses (words must not be clipped early).
        if self._voiced:
            self._buffer = np.concatenate([self._buffer, frame]) if self._buffer.size else frame
            if (now - (self._last_voice_ts or now)) * 1000 >= self.end_silence_ms:
                return self._finalize(now)
            return None

        # Nothing voiced: drop any residual non-speech accumulation so a
        # background-noise tail can never grow unbounded.
        self._reset_tail()
        return None

    def _reset_tail(self):
        self._buffer = np.zeros(0, dtype=np.float32)

    def _finalize(self, now: float) -> Optional[Utterance]:
        duration = self.buffered_seconds
        start = self._speech_started_at if self._speech_started_at is not None else now
        speech = self._speech_seconds
        audio = self._buffer
        self.reset()
        if speech < self.min_speech or audio.size == 0:
            return None
        utt = Utterance(audio=audio, rms=float(np.sqrt(np.mean(np.square(audio.astype(np.float64))))),
                        start_ts=start, duration=duration, speech_seconds=speech)
        if self.on_utterance:
            try:
                self.on_utterance(utt)
            except Exception:
                pass
        return utt

    def flush(self) -> Optional[Utterance]:
        """Force-finalize any pending speech (used on disconnect/stop)."""
        if self._voiced and self._buffer.size:
            now = time.monotonic()
            duration = self.buffered_seconds
            if self._speech_seconds >= self.min_speech:
                utt = Utterance(audio=self._buffer, rms=float(
                    np.sqrt(np.mean(np.square(self._buffer.astype(np.float64))))),
                    start_ts=self._speech_started_at or now, duration=duration,
                    speech_seconds=self._speech_seconds)
                self.reset()
                if self.on_utterance:
                    try:
                        self.on_utterance(utt)
                    except Exception:
                        pass
                return utt
        self.reset()
        return None


# ── Frame (de)serialization helpers ────────────────────────────────────────

def decode_wav_frame(frame_bytes: bytes, sample_rate: int = 16000) -> np.ndarray:
    """Decode a mono int16 WAV frame into float32 @16 kHz."""
    try:
        with wave.open(io.BytesIO(frame_bytes), "rb") as wf:
            n_channels = wf.getnchannels()
            nframes = wf.getnframes()
            raw = wf.readframes(nframes)
    except Exception:
        return np.zeros(0, dtype=np.float32)
    if n_channels != 1:
        return np.zeros(0, dtype=np.float32)
    pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    return pcm.reshape(-1)


def encode_wav_float32(samples: np.ndarray, sample_rate: int = 16000) -> bytes:
    """Encode float32 mono samples into a WAV byte blob for STT."""
    if samples.size == 0:
        samples = np.zeros(1, dtype=np.float32)
    pcm = np.clip(samples, -1.0, 1.0)
    pcm16 = (pcm * 32767.0).astype(np.int16)
    bio = io.BytesIO()
    with wave.open(bio, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm16.tobytes())
    return bio.getvalue()


def frame_rms_db(frame: np.ndarray) -> float:
    """RMS in dBFS (negative), used just for logging."""
    rms = EnergyVAD.rms(frame)
    if rms <= 1e-6:
        return -120.0
    return 20.0 * math.log10(rms)