"""Unit tests for the desktop capture stack (desktop/audio_loopback.py).

These tests run WITHOUT audio hardware: pyaudiowpatch and sounddevice are
replaced by in-memory stubs that simulate PortAudio callbacks, so the DSP
(resample / mono downmix / chunk size / drop-oldest / discovery parsing) is
verified headlessly.
"""
from __future__ import annotations

import queue
import sys
from pathlib import Path

import numpy as np
import pytest

DESKTOP_DIR = Path(__file__).resolve().parents[2] / "desktop"
sys.path.insert(0, str(DESKTOP_DIR))


# ─────────────────────────────────────────────────────────────────────────────
# Stub modules for pyaudiowpatch / sounddevice
# ─────────────────────────────────────────────────────────────────────────────

class _FakeStream:
    def __init__(self, _parent=None, **kwargs):
        self._cb = kwargs.get("stream_callback") or kwargs.get("callback")
        self._parent = _parent
        self.started = False

    def start_stream(self):
        self.started = True

    def stop_stream(self):
        pass

    def start(self):
        self.started = True

    def stop(self):
        pass

    def close(self):
        self._parent.last_closed = True

    def feed(self, data, *args):
        if self._cb is not None:
            self._cb(data, 0, None, None)


class _FakePyAudio:
    paInt16 = 8
    paFloat32 = 0x10000
    paContinue = 0x20000

    def __init__(self):
        self.streams = []
        self.default_loopback = {
            "index": 10,
            "name": "Speakers (Realtek(R) Audio) [Loopback]",
            "defaultSampleRate": 48000.0,
            "maxInputChannels": 2,
        }
        self.last_closed = False

    def terminate(self):
        pass

    def get_default_wasapi_loopback(self):
        return self.default_loopback

    def get_loopback_device_info_generator(self):
        if self.default_loopback is None:
            return iter([])
        return iter([self.default_loopback])

    def open(self, **kwargs):
        stream = _FakeStream(self, **kwargs)
        self.streams.append(stream)
        return stream


class _FakeSounddevice:
    def __init__(self):
        self.last = None

    def query_devices(self):
        return [
            {"name": "Microphone (Realtek Audio)", "max_input_channels": 1,
             "default_samplerate": 48000.0, "hostapi": 0},
            {"name": "Intel Smart Sound", "max_input_channels": 2,
             "default_samplerate": 48000.0, "hostapi": 0},
        ]

    def InputStream(self, **kwargs):
        self.last = kwargs
        return _FakeStream(**kwargs)


def _install_stubs():
    fake_pa = _FakePyAudio()
    fake_sd = _FakeSounddevice()

    pyaudiowpatch_mod = type(sys)("pyaudiowpatch")
    pyaudiowpatch_mod.PyAudio = lambda: fake_pa  # shared singleton instance
    pyaudiowpatch_mod.paInt16 = _FakePyAudio.paInt16
    pyaudiowpatch_mod.paContinue = _FakePyAudio.paContinue

    sounddevice_mod = type(sys)("sounddevice")
    sounddevice_mod.InputStream = fake_sd.InputStream
    sounddevice_mod.query_devices = fake_sd.query_devices

    sys.modules["pyaudiowpatch"] = pyaudiowpatch_mod
    sys.modules["sounddevice"] = sounddevice_mod
    return {"pa": fake_pa, "sd": fake_sd}


_STUBS = _install_stubs()

import audio_loopback  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Discovery
# ─────────────────────────────────────────────────────────────────────────────

def test_find_wasapi_loopback_prefers_default():
    info = audio_loopback.find_wasapi_loopback_device()
    assert info is not None
    assert info["index"] == 10
    assert info["samplerate"] == 48000
    assert info["channels"] == 2
    assert "Loopback" in info["name"]


def test_find_wasapi_loopback_falls_back_to_generator(capsys):
    _STUBS["pa"].default_loopback = None
    info = audio_loopback.find_wasapi_loopback_device()
    assert info is None
    out = capsys.readouterr().out
    assert "unavailable" in out


def test_find_wasapi_loopback_floats_are_ints():
    _STUBS["pa"].default_loopback = {
        "index": 3,
        "name": "X [Loopback]",
        "defaultSampleRate": 44100.0,
        "maxInputChannels": 2,
    }
    info = audio_loopback.find_wasapi_loopback_device()
    assert info["index"] == 3
    assert info["samplerate"] == 44100
    assert isinstance(info["samplerate"], int)


def test_validate_wasapi_loopback_true_when_open_succeeds():
    assert audio_loopback.validate_wasapi_loopback({
        "index": 10, "name": "X", "samplerate": 48000, "channels": 2,
    }) is True


def test_enumerate_microphones():
    mics = audio_loopback.enumerate_microphones()
    assert len(mics) == 2
    assert mics[0]["index"] == 0
    assert mics[0]["name"] == "Microphone (Realtek Audio)"


# ─────────────────────────────────────────────────────────────────────────────
# Loopback capture DSP (resample + downmix + chunking)
# ─────────────────────────────────────────────────────────────────────────────

def test_loopback_capture_emits_4000_sample_mono_chunks():
    emitted = []
    cap = audio_loopback.LoopbackCapture(
        on_chunk=emitted.append,
        device_info={"index": 10, "name": "X [Loopback]", "samplerate": 48000, "channels": 2},
    )
    assert cap.start() is True

    # 4 seconds of 48 kHz stereo int16: left=1000, right=3000 → mono mean 2000.
    n = int(48000 * 4)
    left = np.full(n, 1000, dtype=np.int16)
    right = np.full(n, 3000, dtype=np.int16)
    stereo = np.repeat(np.stack([left, right], axis=1), 1, axis=0).reshape(-1)
    _STUBS["pa"].streams[-1].feed(stereo.tobytes())
    cap.stop()

    assert len(emitted) == 16  # 4s / 0.25s
    for wav in emitted:
        parsed = _decode_wav(wav)
        assert parsed["samplerate"] == 16000
        assert parsed["channels"] == 1
        assert parsed["samples"].shape[0] == 4000
    # Mono downmix value survives: mean(1000,3000)=2000 → pcm int16 ≈ 2000.
    first = _decode_wav(emitted[0])["samples"][0]
    assert 1900 <= int(first) <= 2100


def test_loopback_capture_native_rate_equals_target_no_resample_error():
    emitted = []
    # A 16 kHz loopback endpoint should not crash the resample path (no-op).
    cap = audio_loopback.LoopbackCapture(
        on_chunk=emitted.append,
        device_info={"index": 4, "name": "Y [Loopback]", "samplerate": 16000, "channels": 1},
    )
    assert cap.start() is True
    n = int(16000 * 0.5)
    _STUBS["pa"].streams[-1].feed(np.full(n, 0, dtype=np.int16).tobytes())
    cap.stop()
    assert len(emitted) == 2


def test_microphone_capture_emits_chunks():
    emitted = []
    cap = audio_loopback.MicrophoneCapture(device_index=0, on_chunk=emitted.append)
    assert cap.start() is True
    n = int(16000 * 1.0)
    cap._stream.feed(np.full(n, 0.5, dtype=np.float32))
    cap.stop()
    assert len(emitted) == 4
    for wav in emitted:
        parsed = _decode_wav(wav)
        assert parsed["samplerate"] == 16000
        assert parsed["samples"].shape[0] == 4000


# ─────────────────────────────────────────────────────────────────────────────
# Backpressure (drop-oldest)
# ─────────────────────────────────────────────────────────────────────────────

def test_bounded_put_drop_oldest():
    q = queue.Queue(maxsize=2)
    assert audio_loopback.bounded_put_drop_oldest(q, b"a") is False
    assert audio_loopback.bounded_put_drop_oldest(q, b"b") is False
    assert audio_loopback.bounded_put_drop_oldest(q, b"c") is True  # 'a' dropped
    assert list(q.queue) == [b"b", b"c"]


def test_bounded_put_drop_oldest_keeps_newest():
    q = queue.Queue(maxsize=1)
    audio_loopback.bounded_put_drop_oldest(q, 1)
    audio_loopback.bounded_put_drop_oldest(q, 2)
    audio_loopback.bounded_put_drop_oldest(q, 3)
    assert list(q.queue) == [3]


# ─────────────────────────────────────────────────────────────────────────────
# Resampler / mixer sanity (desktop/audio.py)
# ─────────────────────────────────────────────────────────────────────────────

def test_resample_linear_48000_to_16000_ratio():
    from audio import resample_linear

    n = 48000
    x = np.sin(2 * np.pi * 440 * np.arange(n) / 48000).astype(np.float32)
    y = resample_linear(x, 48000, 16000)
    assert y.shape[0] == pytest.approx(n / 3, abs=2)
    # 440 Hz tone survives resampling: autocorrelation peak near 1 period (36.4 samples @16k).
    corr = np.correlate(y[:1600], y[:1600], "full")[1599:]
    peak = int(np.argmax(corr[10:])) + 10
    assert abs(peak - int(16000 / 440)) <= 2


def test_mix_mono_averages_channels():
    from audio import mix_mono as mm

    a = np.full(100, 0.5, dtype=np.float32)
    b = np.full(50, 0.0, dtype=np.float32)
    m = mm([a, b])
    assert m.shape[0] == 100
    assert m[0] == pytest.approx(0.25)
    assert m[90] == pytest.approx(0.25)


def test_encode_wav_bytes_contract():
    from audio import encode_wav_bytes

    wav = encode_wav_bytes(np.zeros(4000, dtype=np.int16), 16000)
    assert wav[:4] == b"RIFF"
    assert wav[8:12] == b"WAVE"
    parsed = _decode_wav(wav)
    assert parsed["samples"].shape[0] == 4000
    assert parsed["samplerate"] == 16000


# ─────────────────────────────────────────────────────────────────────────────

def _decode_wav(wav: bytes) -> dict:
    import io as _io
    import wave as _wave

    bio = _io.BytesIO(wav)
    with _wave.open(bio, "rb") as wf:
        raw = wf.readframes(wf.getnframes())
        return {
            "channels": wf.getnchannels(),
            "samplerate": wf.getframerate(),
            "samples": np.frombuffer(raw, dtype=np.int16),
        }