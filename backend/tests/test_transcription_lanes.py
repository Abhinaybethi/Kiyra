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


def test_transcribe_forwards_initial_prompt(monkeypatch):
    """The vocabulary hint must actually reach faster-whisper."""
    class FakeModel:
        def __init__(self):
            self.kwargs = None

        def transcribe(self, audio_input, **kwargs):
            self.kwargs = kwargs
            return iter([]), type("Info", (), {"language": "en"})()

    m = FakeModel()
    T._transcribe(m, io.BytesIO(b""), fast=False,
                  initial_prompt="Kiyra, FastAPI")
    assert m.kwargs["initial_prompt"] == "Kiyra, FastAPI"


def test_initial_prompt_contains_product_and_profile_vocabulary(monkeypatch):
    """"Kiyra" must be in the hint — the `base` model hears it as "CURE"."""
    monkeypatch.setattr(T, "_profile_vocab", lambda: "LedgerSync, GraphQL")
    monkeypatch.setattr(T.settings, "transcription_initial_prompt",
                        "Kiyra, FastAPI", raising=False)
    monkeypatch.setattr(T.settings, "transcription_vocab_from_profile",
                        True, raising=False)

    prompt = T._initial_prompt()

    assert prompt is not None
    assert "Kiyra" in prompt, "product name must bias the decoder"
    assert "LedgerSync" in prompt, "candidate project names must be included"
    assert len(prompt) <= T._vocab_max_chars, (
        "a long hint biases short clips and can be echoed on silence")


def test_initial_prompt_is_none_when_nothing_configured(monkeypatch):
    """No hint at all must mean None — not an empty string sent to Whisper."""
    monkeypatch.setattr(T, "_profile_vocab", lambda: "")
    monkeypatch.setattr(T.settings, "transcription_initial_prompt", "",
                        raising=False)
    monkeypatch.setattr(T.settings, "transcription_vocab_from_profile",
                        True, raising=False)

    assert T._initial_prompt() is None


def test_profile_vocab_degrades_to_empty_string_on_db_failure(monkeypatch):
    """A broken DB lookup must never fail transcription."""
    import sys
    import types

    broken = types.ModuleType("db.database")

    def _boom():
        raise RuntimeError("db unavailable")

    broken.SessionLocal = _boom
    monkeypatch.setitem(sys.modules, "db.database", broken)
    monkeypatch.setattr(T, "_vocab_cache", None, raising=False)

    assert T._profile_vocab() == ""
