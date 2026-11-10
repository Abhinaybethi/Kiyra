"""STT lane isolation tests.

The authoritative *final* transcription must never queue behind a partial
preview call. Both run through `transcribe_with_lock`, distinguished by the
`fast` flag: `fast=True` (partial/speculative preview) gets a dedicated model
instance and its own inference lock; `fast=False` (final) keeps the
authoritative model. These tests pin that routing and prove a blocked partial
cannot delay a final.
"""
from __future__ import annotations

import io
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from realtime import transcription as T  # noqa: E402


def test_fast_path_uses_partial_lane(monkeypatch):
    seen = {}

    monkeypatch.setattr(T, "_get_model", lambda: "MAIN")
    monkeypatch.setattr(T, "_get_partial_model", lambda: "PARTIAL")

    def fake(audio_bytes, audio_format="wav", fast=False, model=None):
        seen["model"] = model
        seen["fast"] = fast
        return T.TranscriptionResult(text="ok")

    monkeypatch.setattr(T, "transcribe_audio_bytes", fake)

    T.transcribe_with_lock(b"p", "wav", fast=True)
    assert seen == {"model": "PARTIAL", "fast": True}

    T.transcribe_with_lock(b"f", "wav", fast=False)
    assert seen == {"model": "MAIN", "fast": False}


def test_lanes_have_distinct_locks_and_models(monkeypatch):
    monkeypatch.setattr(T, "_get_model", lambda: "MAIN")
    monkeypatch.setattr(T, "_get_partial_model", lambda: "PARTIAL")
    assert T._model_lock is not T._partial_infer_lock
    assert T._get_model() != T._get_partial_model()


def test_blocked_partial_does_not_delay_final(monkeypatch):
    partial_started = threading.Event()
    release_partial = threading.Event()

    monkeypatch.setattr(T, "_get_model", lambda: "MAIN")
    monkeypatch.setattr(T, "_get_partial_model", lambda: "PARTIAL")

    def fake(audio_bytes, audio_format="wav", fast=False, model=None):
        if model == "PARTIAL":
            partial_started.set()
            release_partial.wait(5.0)  # hold the partial lane open
        return T.TranscriptionResult(text=model or "?")

    monkeypatch.setattr(T, "transcribe_audio_bytes", fake)

    th = threading.Thread(
        target=lambda: T.transcribe_with_lock(b"p", "wav", fast=True),
        daemon=True)
    th.start()
    assert partial_started.wait(2.0), "partial lane never started"

    t0 = time.monotonic()
    res = T.transcribe_with_lock(b"f", "wav", fast=False)
    elapsed = time.monotonic() - t0

    release_partial.set()
    th.join(timeout=2.0)

    assert res.text == "MAIN"
    assert elapsed < 1.0, (
        f"final blocked behind partial for {elapsed:.2f}s — lanes not isolated")


def test_silero_vad_disabled_so_quiet_mics_are_not_rejected():
    """faster-whisper's Silero VAD must stay OFF for the final transcription.

    The backend EnergyVAD already segments speech-bounded utterances, so
    re-running Silero inside Whisper is redundant — and on a real quiet-mic
    capture (rms ~0.05, peaks ~0.09) it scored the whole clip "no speech" and
    returned '' with zero words: the exact "listening hears me but nothing is
    transcribed" symptom. This pins vad_filter=False so a regression can't
    silently re-enable it.
    """
    class FakeModel:
        def __init__(self):
            self.kwargs = None

        def transcribe(self, audio_input, **kwargs):
            self.kwargs = kwargs
            return iter([]), type("Info", (), {"language": "en"})()

    m = FakeModel()
    T._transcribe(m, io.BytesIO(b""), fast=False)
    assert m.kwargs["vad_filter"] is False, (
        "Whisper's Silero VAD must stay off — the backend EnergyVAD already "
        "segments utterances and Silero rejects quiet-mic speech wholesale")
    assert m.kwargs["no_speech_threshold"] <= 0.6
