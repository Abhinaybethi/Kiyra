"""Unit tests for the desktop WASAPI loopback-only capture stack.

These tests run WITHOUT audio hardware: pyaudiowpatch is replaced by an
in-memory stub that simulates PortAudio callbacks with WASAPI timestamps, so the
DSP (flag verification / resample / mono downmix / chunk size / discontinuity
detection / endpoint monitoring) is verified headlessly.

Architecture contract under test: the interview pipeline has exactly one audio
source — the WASAPI loopback twin of the default playback endpoint.  There is
no microphone capture, no mixing sink, and no microphone fallback.
"""
from __future__ import annotations

import queue
import sys
import time
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
DESKTOP_DIR = ROOT / "desktop"
sys.path.insert(0, str(DESKTOP_DIR))


# ─────────────────────────────────────────────────────────────────────────────
# Stub pyaudiowpatch (WASAPI host API + verified loopback endpoint)
# ─────────────────────────────────────────────────────────────────────────────

class _FakeStream:
    def __init__(self, _parent=None, **kwargs):
        self._cb = kwargs.get("stream_callback") or kwargs.get("callback")
        self._parent = _parent
        self.kwargs = kwargs
        self.started = False

    def start_stream(self):
        self.started = True

    def stop_stream(self):
        self.started = False

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def close(self):
        self._parent.last_closed = True

    def feed(self, data, time_info=None, status=0):
        if self._cb is not None:
            channels = int(self.kwargs.get("channels", 1))
            self._cb(data, len(data) // (2 * channels), time_info or {}, status)


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
            "maxOutputChannels": 0,
            "hostApi": 2,
            "isLoopbackDevice": True,
        }
        self.last_closed = False

    def terminate(self):
        pass

    def get_host_api_info_generator(self):
        return iter([{"index": 2, "type": 13, "name": "Windows WASAPI"},
                     {"index": 0, "type": 2, "name": "MME"}])

    def get_default_wasapi_loopback(self):
        return self.default_loopback

    def get_loopback_device_info_generator(self):
        if self.default_loopback is None:
            return iter([])
        return iter([self.default_loopback])

    def open(self, **kwargs):
        assert kwargs.get("input") is True, "only loopback INPUT streams allowed"
        stream = _FakeStream(self, **kwargs)
        self.streams.append(stream)
        return stream


def _install_stubs():
    fake_pa = _FakePyAudio()
    mod = type(sys)("pyaudiowpatch")
    mod.PyAudio = lambda: fake_pa  # shared singleton
    mod.paInt16 = _FakePyAudio.paInt16
    mod.paContinue = _FakePyAudio.paContinue
    sys.modules["pyaudiowpatch"] = mod
    return {"pa": fake_pa}


_STUBS = _install_stubs()

import audio_loopback  # noqa: E402


def _info(**overrides):
    base = dict(_STUBS["pa"].default_loopback or {
        "index": 10, "name": "Speakers [Loopback]",
        "defaultSampleRate": 48000.0, "maxInputChannels": 2,
        "maxOutputChannels": 0, "hostApi": 2, "isLoopbackDevice": True,
    })
    base.update(overrides)
    return base


def _normalised(**overrides):
    return audio_loopback._normalise_loopback_info(_STUBS["pa"], _info(**overrides))


# ─────────────────────────────────────────────────────────────────────────────
# Architecture: no microphone path anywhere in the interview pipeline
# ─────────────────────────────────────────────────────────────────────────────

def test_no_microphone_or_mixer_api_in_audio_stack():
    for forbidden in ("MicrophoneCapture", "enumerate_microphones",
                      "_MixingChunkSink", "sounddevice"):
        assert not hasattr(audio_loopback, forbidden), forbidden


def test_overlay_source_has_no_microphone_path():
    src = (DESKTOP_DIR / "stealth_overlay.py").read_text(encoding="utf-8")
    for forbidden in ("import sounddevice", "MicrophoneCapture",
                      "_MixingChunkSink", "enumerate_microphones",
                      "self.mic_device", "_mic_choice"):
        assert forbidden not in src, forbidden


# ─────────────────────────────────────────────────────────────────────────────
# Discovery / flag verification
# ─────────────────────────────────────────────────────────────────────────────

def test_find_wasapi_loopback_prefers_default():
    info = audio_loopback.find_wasapi_loopback_device()
    assert info["index"] == 10
    assert info["samplerate"] == 48000
    assert info["channels"] == 2
    assert info["is_loopback"] is True
    assert info["host_api"] == 2
    assert info["render_endpoint"] == "Speakers (Realtek(R) Audio)"
    assert "Loopback" in info["name"]


def test_non_loopback_device_is_rejected():
    assert _normalised(isLoopbackDevice=False, name="Microphone (Realtek)",
                       maxInputChannels=1) is None


def test_non_wasapi_device_is_rejected():
    assert _normalised(hostApi=0, name="Stereo Mix", isLoopbackDevice=True) is None


def test_fallback_still_requires_loopback_flags():
    _STUBS["pa"].default_loopback = None
    assert audio_loopback.find_wasapi_loopback_device() is None
    out = _info()
    _STUBS["pa"].default_loopback = out


def test_validate_rejects_unverified_device_without_opening_stream():
    before = len(_STUBS["pa"].streams)
    assert audio_loopback.validate_wasapi_loopback(
        {"index": 1, "name": "Mic", "samplerate": 16000, "channels": 1}) is False
    assert len(_STUBS["pa"].streams) == before


def test_validate_opens_actual_loopback_stream():
    assert audio_loopback.validate_wasapi_loopback(_normalised()) is True
    assert _STUBS["pa"].streams[-1].kwargs["input_device_index"] == 10


# ─────────────────────────────────────────────────────────────────────────────
# Loopback capture DSP (resample + downmix + chunking)
# ─────────────────────────────────────────────────────────────────────────────

def test_loopback_capture_emits_4000_sample_mono_chunks():
    emitted = []
    cap = audio_loopback.LoopbackCapture(
        on_chunk=emitted.append,
        device_info=_normalised(),
    )
    assert cap.start() is True

    n = int(48000 * 4)
    left = np.full(n, 1000, dtype=np.int16)
    right = np.full(n, 3000, dtype=np.int16)
    stereo = np.stack([left, right], axis=1).reshape(-1)
    _STUBS["pa"].streams[-1].feed(stereo.tobytes(),
                                  {"input_buffer_adc_time": 1000.0})
    cap.stop()

    assert len(emitted) == 16  # 4 s / 0.25 s
    for wav in emitted:
        parsed = _decode_wav(wav)
        assert parsed["samplerate"] == 16000
        assert parsed["channels"] == 1
        assert parsed["samples"].shape[0] == 4000
    assert 1900 <= int(_decode_wav(emitted[0])["samples"][0]) <= 2100


def test_loopback_capture_native_rate_equals_target_no_resample_error():
    emitted = []
    cap = audio_loopback.LoopbackCapture(
        on_chunk=emitted.append,
        device_info=_normalised(defaultSampleRate=16000.0, maxInputChannels=1),
    )
    assert cap.start() is True
    _STUBS["pa"].streams[-1].feed(
        np.full(16000 // 2, 0, dtype=np.int16).tobytes(),
        {"input_buffer_adc_time": 1.0})
    cap.stop()
    assert len(emitted) == 2


def test_timestamp_gap_is_reported_as_discontinuity():
    messages = []
    cap = audio_loopback.LoopbackCapture(
        on_chunk=lambda _wav: None,
        device_info=_normalised(),
        on_status=messages.append,
    )
    assert cap.start() is True
    stream = _STUBS["pa"].streams[-1]
    # First callback establishes the clock; second jumps 1 s instead of 0.1 s.
    frame = np.zeros(4800 * 2, dtype=np.int16).tobytes()  # 4800 stereo frames
    stream.feed(frame, {"input_buffer_adc_time": 100.0})
    stream.feed(frame, {"input_buffer_adc_time": 101.1})
    cap.stop()
    assert cap.discontinuities == 1
    assert any("discontinuity" in m and "gap_ms=1000" in m for m in messages)


def test_idle_watchdog_feeds_only_digital_silence(monkeypatch):
    monkeypatch.setattr(audio_loopback, "IDLE_AFTER_SECONDS", 0.02)
    emitted = []
    cap = audio_loopback.LoopbackCapture(
        on_chunk=emitted.append, device_info=_normalised())
    assert cap.start() is True
    time.sleep(0.45)  # enough silence to fill at least one 250 ms chunk
    cap.stop()
    assert cap.silence_seconds > 0.0
    assert emitted
    assert all(np.count_nonzero(_decode_wav(w)["samples"]) == 0 for w in emitted)


# ─────────────────────────────────────────────────────────────────────────────
# Endpoint changes / disconnection
# ─────────────────────────────────────────────────────────────────────────────

def test_monitor_detects_endpoint_change_and_disconnection():
    monitor = audio_loopback.WASAPIDeviceMonitor()
    first = audio_loopback.find_wasapi_loopback_device(pa=monitor._pa, verbose=False)
    assert monitor.changed(first) is True
    assert monitor.changed(dict(first)) is False

    _STUBS["pa"].default_loopback = _info(
        index=77, name="Headphones (BT) [Loopback]")
    second = audio_loopback.find_wasapi_loopback_device(pa=monitor._pa, verbose=False)
    assert second["index"] == 77
    assert monitor.changed(second) is True

    _STUBS["pa"].default_loopback = None
    assert monitor.changed(None) is True
    monitor.close()
    _STUBS["pa"].default_loopback = _info()


def test_capture_loop_reinitializes_on_endpoint_change_and_never_uses_mic(monkeypatch):
    """Orchestration level: one stream, reinit on change, disconnect stays loopback."""
    import threading
    import types

    import stealth_overlay as so

    dev_a = _normalised()
    dev_b = _normalised(index=77, name="Headphones (BT) [Loopback]")
    events = []

    class FakeCapture:
        def __init__(self, device_info, **_kw):
            self.device = device_info
            events.append("start:" + device_info["name"])

        def start(self):
            return True

        def stop(self):
            events.append("stop")

        def endpoint_key(self):
            return (self.device["index"], self.device["name"])

    class FakeMonitor:
        def __init__(self):
            self.seq = iter([dev_a, dev_b, None])

        def changed(self, info):
            return True

        def poll(self):
            nxt = next(self.seq)
            if nxt is None:
                app._stop_event.set()
            return nxt

        def close(self):
            events.append("monitor_closed")

    monkeypatch.setattr(so, "LoopbackCapture", FakeCapture)
    monkeypatch.setattr(so, "WASAPIDeviceMonitor", FakeMonitor)
    monkeypatch.setattr(so, "validate_wasapi_loopback", lambda _info: True)
    monkeypatch.setattr(so, "MONITOR_INTERVAL_SECONDS", 0.01)

    app = types.SimpleNamespace(
        _stop_event=threading.Event(),
        loopback_device=dev_a,
        _last_loop=dev_a,
        _loop_name=None,
        _loop_choice=None,
        _loopback_active=False,
        _audio_queue=types.SimpleNamespace(maxsize=50),
        _push_audio_chunk=lambda _b: events.append("chunk"),
        _render_sys_audio_state=lambda: events.append("render"),
        _sync_sys_audio_selector=lambda: events.append("selector"),
        _ui_set_status=lambda msg, ok=False: events.append("status:" + msg),
    )
    so.StealthOverlayApp._capture_loop(app)

    assert events.count("start:" + dev_a["name"]) == 1
    assert events.count("start:" + dev_b["name"]) == 1
    assert events.count("stop") >= 2
    assert "monitor_closed" in events
    assert any("disconnected" in e for e in events)
    assert not any("microphone_only" in e or "src=mic" in e for e in events)


# ─────────────────────────────────────────────────────────────────────────────
# Backpressure / WAV contract
# ─────────────────────────────────────────────────────────────────────────────

def test_bounded_put_drop_oldest():
    q = queue.Queue(maxsize=2)
    assert audio_loopback.bounded_put_drop_oldest(q, b"a") is False
    assert audio_loopback.bounded_put_drop_oldest(q, b"b") is False
    assert audio_loopback.bounded_put_drop_oldest(q, b"c") is True
    assert list(q.queue) == [b"b", b"c"]


def test_bounded_put_drop_oldest_keeps_newest():
    q = queue.Queue(maxsize=1)
    audio_loopback.bounded_put_drop_oldest(q, 1)
    audio_loopback.bounded_put_drop_oldest(q, 2)
    audio_loopback.bounded_put_drop_oldest(q, 3)
    assert list(q.queue) == [3]


def test_resample_linear_48000_to_16000_ratio():
    from audio import resample_linear

    n = 48000
    x = np.sin(2 * np.pi * 440 * np.arange(n) / 48000).astype(np.float32)
    y = resample_linear(x, 48000, 16000)
    assert y.shape[0] == pytest.approx(n / 3, abs=2)
    corr = np.correlate(y[:1600], y[:1600], "full")[1599:]
    peak = int(np.argmax(corr[10:])) + 10
    assert abs(peak - int(16000 / 440)) <= 2


def test_encode_wav_bytes_contract():
    from audio import encode_wav_bytes

    wav = encode_wav_bytes(np.zeros(4000, dtype=np.int16), 16000)
    assert wav[:4] == b"RIFF"
    assert wav[8:12] == b"WAVE"
    parsed = _decode_wav(wav)
    assert parsed["samples"].shape[0] == 4000
    assert parsed["samplerate"] == 16000


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
