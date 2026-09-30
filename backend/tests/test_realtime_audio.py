"""Unit tests for the real-time VAD / utterance pipeline."""
import io
import wave

import numpy as np
import pytest

from realtime.audio import (
    EnergyVAD,
    UtteranceReceiver,
    decode_wav_frame,
    encode_wav_float32,
    frame_rms_db,
)


def make_frame(sample_rate=16000, duration=0.3, amplitude=0.6):
    """Synthesized sine wave frame (voiced-sounding)."""
    n = int(sample_rate * duration)
    t = np.arange(n) / sample_rate
    return (amplitude * np.sin(2 * np.pi * 440 * t)).astype(np.float32)


def silent_frame(sample_rate=16000, duration=0.3):
    return np.zeros(int(sample_rate * duration), dtype=np.float32)


def test_energy_vad_speech_detection():
    vad = EnergyVAD()
    assert vad.is_speech(make_frame(), False) is True
    assert vad.is_speech(silent_frame(), False) is False


def test_energy_vad_hysteresis():
    vad = EnergyVAD(speech_start_threshold=0.3)
    # RMS between rearm (0.18) and start (0.3): voiced continues, silence starts.
    mid = (0.3 * np.sin(np.linspace(0, 40, 800))).astype(np.float32)
    rms = vad.rms(mid)
    assert 0.18 < rms < 0.3  # between rearm and start thresholds
    assert vad.is_speech(mid, currently_voiced=True) is True  # stays voiced
    assert vad.is_speech(mid, currently_voiced=False) is False  # won't start speech


def test_utterance_finalizes_on_trailing_silence():
    rx = UtteranceReceiver(
        speech_end_silence_ms=800,
        minimum_speech_duration=0.1,
        sample_rate=16000,
    )
    t = 100.0
    utt = None
    for _ in range(5):  # 1.5s of speech
        utt = rx.push_frame(make_frame(), t)
        t += 0.3
    assert utt is None  # trailing silence window not yet elapsed
    for _ in range(3):  # 0.9s silence
        utt = rx.push_frame(silent_frame(), t)
        t += 0.3
    assert utt is not None
    assert utt.duration >= 1.5
    assert rx.is_voiced is False


def test_utterance_rejects_short_noise():
    rx = UtteranceReceiver(minimum_speech_duration=0.5, sample_rate=16000)
    t = 100.0
    for _ in range(2):  # 0.6s speech — passes the 0.5s minimum
        rx.push_frame(make_frame(duration=0.3), t)
        t += 0.3
    results = []
    for _ in range(4):
        results.append(rx.push_frame(silent_frame(), t))
        t += 0.3
    assert any(r is not None for r in results)

    rx = UtteranceReceiver(minimum_speech_duration=1.0, sample_rate=16000)
    t = 200.0
    for _ in range(2):  # 0.6s < 1.0s minimum
        rx.push_frame(make_frame(duration=0.3), t)
        t += 0.3
    results = []
    for _ in range(4):
        results.append(rx.push_frame(silent_frame(), t))
        t += 0.3
    assert not any(r is not None for r in results)  # dropped as too short


def test_utterance_maximum_duration_cap():
    rx = UtteranceReceiver(
        maximum_utterance_duration=0.5,
        speech_end_silence_ms=5000,
        minimum_speech_duration=0.1,
        sample_rate=16000,
    )
    t = 100.0
    results = []
    for _ in range(5):  # continuous speech past the 0.5s cap
        results.append(rx.push_frame(make_frame(), t))
        t += 0.3
    assert any(r is not None for r in results)
    # A capped utterance can still be pushed mid-speech in a later chunk.
    finished = [r for r in results if r is not None]
    assert finished[0].duration <= 0.5 + 0.3 + 1e-6


def test_utterance_flush_on_stop():
    rx = UtteranceReceiver(minimum_speech_duration=0.1, sample_rate=16000)
    t = 100.0
    for _ in range(3):
        rx.push_frame(make_frame(), t)
        t += 0.3
    utt = rx.flush()
    assert utt is not None
    assert rx.flush() is None  # already reset


def test_wav_roundtrip():
    samples = make_frame(duration=0.25)
    blob = encode_wav_float32(samples)
    decoded = decode_wav_frame(blob)
    assert decoded.shape == samples.shape
    assert np.allclose(decoded, samples, atol=1.5 / 32768.0)


def test_decode_wav_rejects_multichannel_and_garbage():
    # Build a 2-channel wav
    n = 1600
    bio = io.BytesIO()
    with wave.open(bio, "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes((np.ones(2 * n, dtype=np.int16)).tobytes())
    assert decode_wav_frame(bio.getvalue()).size == 0
    assert decode_wav_frame(b"garbage").size == 0


def test_frame_rms_db_bounds():
    assert frame_rms_db(silent_frame()) <= -100.0
    assert frame_rms_db(make_frame()) > -20.0