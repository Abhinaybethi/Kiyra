"""
Windows OS-level system playback capture for the Kiyra Stealth Overlay.

THE INTERVIEW AUDIO PIPELINE HAS EXACTLY ONE SOURCE: Windows WASAPI loopback.

The capture endpoint is the ``[Loopback]`` analogue of the current default
Windows *render* (playback) endpoint, discovered through ``pyaudiowpatch``.
Opening that endpoint with ``input=True`` makes the patched PortAudio/WASAPI
backend create the capture client with ``AUDCLNT_STREAMFLAGS_LOOPBACK`` in
**shared mode**, so the samples come straight from the Windows Audio Engine's
render path — never from a physical microphone, headphone mic, speaker mic or
acoustic recording.  The device's ``isLoopbackDevice`` flag and the WASAPI host
API are verified before any stream is opened; a non-loopback device is refused.

Chunks emitted by :class:`LoopbackCapture` are exactly what the backend expects
on the WebSocket: **16000 Hz, mono, PCM16, 250 ms** WAV bytes (4000 samples).
Native-rate stereo float data is down-mixed and linearly resampled on the way.

Additional guarantees:
  * Callback timestamps (``input_buffer_adc_time``) are tracked; render-path
    buffer discontinuities are detected and reported, with bounded silence
    inserted so the 16 kHz timeline stays continuous.
  * When Windows has no active render stream, WASAPI loopback delivers no
    callbacks at all.  A watchdog feeds digital silence (never microphone
    audio) so the VAD/Whisper pipeline keeps receiving a steady 250 ms stream.
  * Default playback endpoint changes (wired/Bluetooth headsets, speakers) are
    detected by :class:`WASAPIDeviceMonitor`; the stream is reinitialized on the
    new loopback endpoint.  If the endpoint disappears, capture reports the
    disconnection and waits for it to return — it NEVER switches to a mic.
"""
from __future__ import annotations

import math
import threading
import time
from typing import Callable, Optional

import numpy as np

from audio import encode_wav_bytes, pcm16_from_float32, resample_linear

TARGET_SR = 16000
CHUNK_SECONDS = 0.25
CHUNK_SAMPLES = int(TARGET_SR * CHUNK_SECONDS)  # 4000 samples

# pyaudiowpatch's patched PortAudio sets AUDCLNT_STREAMFLAGS_LOOPBACK (0x20000)
# internally when an endpoint with isLoopbackDevice=True is opened for input.
# We assert that flag via the device metadata instead of guessing by name.
WASAPI_HOST_API_TYPE = 13  # paWASAPI
PA_INPUT_OVERFLOW = 0x00000002  # paInputOverflow (status bit in callbacks)
MAX_GAP_SECONDS = 0.5  # cap for inserted silence after a discontinuity
IDLE_AFTER_SECONDS = 0.15  # no callbacks for this long => feed digital silence
MONITOR_INTERVAL_SECONDS = 0.5


def _load_pyaudiowpatch():
    """Import pyaudiowpatch lazily (may not be installed on every machine)."""
    try:
        import pyaudiowpatch as pyaudio

        return pyaudio
    except Exception:
        return None


def bounded_put_drop_oldest(q, item) -> bool:
    """Non-blocking put onto a bounded queue; on overflow drop the OLDEST item.

    Returns True if an item had to be discarded to make room, False otherwise.
    This is the single source of truth for backpressure in the overlay.
    """
    try:
        q.put_nowait(item)
        return False
    except Exception:
        pass
    try:
        q.get_nowait()  # drop oldest
        discarded = True
    except Exception:
        discarded = False
    try:
        q.put_nowait(item)
        return discarded
    except Exception:
        return True


# ─────────────────────────────────────────────────────────────────────────────
# Loopback endpoint discovery / verification
# ─────────────────────────────────────────────────────────────────────────────

def _wasapi_host_api_indices(pa) -> set:
    """Indices of every WASAPI host API exposed by this PortAudio build."""
    out = set()
    try:
        for ha in pa.get_host_api_info_generator():
            if int(ha.get("type", -1)) == WASAPI_HOST_API_TYPE or \
                    "wasapi" in str(ha.get("name", "")).lower():
                out.add(int(ha["index"]))
    except Exception:
        pass
    return out


def _normalise_loopback_info(pa, dev: Optional[dict]) -> Optional[dict]:
    """Accept ONLY a verified WASAPI loopback endpoint, or return None.

    This is the guard that stops a ordinary microphone device (or a MME "Stereo
    Mix") from ever entering the pipeline: ``isLoopbackDevice`` must be True and
    the endpoint must belong to a WASAPI host API.
    """
    if not dev:
        return None
    if dev.get("isLoopbackDevice") is not True:
        print("[AUDIO_LOOPBACK] rejected non-loopback endpoint (no capture)")
        return None
    wasapi = _wasapi_host_api_indices(pa)
    if wasapi and int(dev.get("hostApi", -1)) not in wasapi:
        print("[AUDIO_LOOPBACK] rejected non-WASAPI endpoint (no capture)")
        return None
    name = str(dev.get("name") or "")
    if not dev.get("maxInputChannels"):
        print("[AUDIO_LOOPBACK] rejected endpoint with no capture channels")
        return None
    info = {
        "index": int(dev["index"]),
        "name": name,
        "samplerate": int(dev.get("defaultSampleRate") or 48000),
        "channels": max(1, int(dev.get("maxInputChannels") or 2)),
        "host_api": int(dev.get("hostApi", -1)),
        "is_loopback": True,
        "render_endpoint": name[:-len(" [Loopback]")] if name.endswith(" [Loopback]") else name,
    }
    return info


def find_wasapi_loopback_device(pa=None, verbose: bool = True) -> Optional[dict]:
    """Discover the WASAPI loopback endpoint for the current default playback
    device (speakers / wired headphones / Bluetooth headphones).

    Never enumerates or returns a microphone device.  ``pa`` may be supplied by
    :class:`WASAPIDeviceMonitor` to avoid creating a PortAudio instance per poll.
    """
    pyaudio = _load_pyaudiowpatch()
    if pyaudio is None:
        print("[AUDIO_LOOPBACK] unavailable reason=pyaudiowpatch not installed")
        return None

    own_pa = pa is None
    pa = pa or pyaudio.PyAudio()
    try:
        dev = None
        reason = "no WASAPI loopback endpoint exposed by the system"
        try:
            dev = pa.get_default_wasapi_loopback()
        except Exception as e:  # pragma: no cover - platform dependent
            reason = f"get_default_wasapi_loopback failed: {e}"

        if dev is None:
            try:  # still loopback-only: first loopback endpoint the OS reports
                dev = next(pa.get_loopback_device_info_generator(), None)
            except Exception as e:  # pragma: no cover - platform dependent
                reason = f"loopback enumeration failed: {e}"

        if dev is None:
            print(f"[AUDIO_LOOPBACK] unavailable reason={reason}")
            return None

        info = _normalise_loopback_info(pa, dev)
        if info is None:
            if verbose:
                print("[AUDIO_LOOPBACK] unavailable reason=endpoint failed loopback verification")
            return None
        if verbose:
            print(
                "[AUDIO_DEVICE] loopback output=%r index=%d native_rate=%d channels=%d "
                "render=%r wasapi_flag=on"
                % (info["name"], info["index"], info["samplerate"],
                   info["channels"], info["render_endpoint"])
            )
        return info
    finally:
        if own_pa:
            try:
                pa.terminate()
            except Exception:
                pass


def validate_wasapi_loopback(info: dict) -> bool:
    """Actually OPEN + START the loopback stream and read one buffer.

    A device listing that merely "looks loopback-ish" is not proof that the
    WASAPI capture client can be created with AUDCLNT_STREAMFLAGS_LOOPBACK.
    """
    if not info or not info.get("is_loopback"):
        print("[AUDIO_LOOPBACK] unavailable reason=endpoint is not a verified loopback")
        return False
    pyaudio = _load_pyaudiowpatch()
    if pyaudio is None:
        return False
    pa = pyaudio.PyAudio()
    try:
        stream = pa.open(
            format=pyaudio.paInt16,
            channels=int(info["channels"]),
            rate=int(info["samplerate"]),
            frames_per_buffer=int(info["samplerate"] * 0.1),
            input=True,
            input_device_index=int(info["index"]),
        )
        # Use a callback here: a blocking read() can wait forever on an idle
        # render endpoint (WASAPI loopback emits no buffers without playback).
        stream.start_stream()
        time.sleep(0.1)
        stream.stop_stream()
        stream.close()
        return True
    except Exception as e:
        print(f"[AUDIO_LOOPBACK] unavailable reason={e}")
        return False
    finally:
        try:
            pa.terminate()
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# 16 kHz mono chunk assembly
# ─────────────────────────────────────────────────────────────────────────────

class _ChunkSink:
    """Accumulate mono float32 @16 kHz and emit 250 ms PCM16 WAV chunks."""

    def __init__(self, on_chunk: Optional[Callable[[bytes], None]],
                 chunk_samples: int = CHUNK_SAMPLES):
        self.on_chunk = on_chunk
        self.chunk_samples = chunk_samples
        self._acc = np.zeros(0, dtype=np.float32)
        self._lock = threading.Lock()
        self.chunks_made = 0

    def feed(self, mono: np.ndarray):
        if mono is None or mono.size == 0:
            return
        with self._lock:
            self._acc = np.concatenate([self._acc, mono])
            out = []
            while self._acc.size >= self.chunk_samples:
                chunk = self._acc[: self.chunk_samples]
                self._acc = self._acc[self.chunk_samples:]
                out.append(encode_wav_bytes(pcm16_from_float32(chunk), TARGET_SR))
                self.chunks_made += 1
        for wav in out:
            if self.on_chunk is not None:
                try:
                    self.on_chunk(wav)
                except Exception:
                    pass


# ─────────────────────────────────────────────────────────────────────────────
# Native WASAPI loopback capture (one source, no microphone)
# ─────────────────────────────────────────────────────────────────────────────

class LoopbackCapture:
    """WASAPI shared-mode render-endpoint loopback capture.

    ``device_info`` is the dict from :func:`find_wasapi_loopback_device`.
    ``on_status`` receives short diagnostic strings (endpoint changes, gaps).
    """

    def __init__(self, on_chunk: Optional[Callable[[bytes], None]] = None,
                 device_info: Optional[dict] = None,
                 sink: Optional[object] = None,
                 on_status: Optional[Callable[[str], None]] = None):
        self.on_chunk = on_chunk
        self.device = device_info
        self._sink = sink if sink is not None else _ChunkSink(on_chunk)
        self._on_status = on_status
        self._pa = None
        self._stream = None
        self._running = False
        self._lock = threading.Lock()
        self._watchdog = None
        self._stop_watchdog = threading.Event()
        # Timestamp / continuity state (all protected by self._lock).
        self._prev_ts = None
        self._last_cb_wall = 0.0
        self._silence_since_cb = 0.0
        self._last_feed_wall = 0.0
        self._using_portaudio_clock = False
        self._idle = True
        self.discontinuities = 0
        self.overflows = 0
        self.silence_seconds = 0.0
        self._pa_continue = 0

    # -- diagnostics ---------------------------------------------------------
    def _status(self, message: str):
        print(message)
        if self._on_status is not None:
            try:
                self._on_status(message)
            except Exception:
                pass

    def endpoint_key(self):
        if not self.device:
            return None
        return (int(self.device["index"]), str(self.device["name"]))

    # -- callback ------------------------------------------------------------
    def _cb(self, in_data, frame_count, time_info, status):
        try:
            now = time.monotonic()
            native = int(self.device["samplerate"])
            dur = float(frame_count) / float(native)
            ts = None
            if isinstance(time_info, dict):
                ts = time_info.get("input_buffer_adc_time") or \
                     time_info.get("input_buffer_adc_time", None)
                if not ts:
                    ts = time_info.get("current_time")
            if ts is not None and not math.isfinite(float(ts)):
                ts = None
            if status:
                self.overflows += 1 if (int(status) & PA_INPUT_OVERFLOW) else 0

            with self._lock:
                if self._prev_ts is None:
                    self._using_portaudio_clock = ts is not None

                # Timestamp-based discontinuity detection.  Only meaningful
                # while the render stream is continuously active; after an idle
                # period the watchdog has already filled the timeline with
                # digital silence, so do not insert it twice.
                gap = 0.0
                prev_ts = self._prev_ts
                if self._using_portaudio_clock and ts is not None and prev_ts is not None:
                    gap = float(ts) - (float(prev_ts) + dur)
                self._prev_ts = ts

                was_idle = self._idle
                idle_for = now - self._last_cb_wall if self._last_cb_wall else 0.0
                self._last_cb_wall = now
                self._last_feed_wall = now
                if was_idle:
                    self._idle = False
                    self._status(
                        "[AUDIO_LOOPBACK] stream resumed (render active) "
                        "idle_gap_ms=%.0f" % (idle_for * 1000))

                extra = max(0.0, gap - self._silence_since_cb)
                self._silence_since_cb = 0.0
                if gap > max(0.02, dur * 1.5) and not was_idle:
                    self.discontinuities += 1
                    self._status(
                        "[AUDIO_LOOPBACK] buffer discontinuity "
                        "gap_ms=%.0f expected_ms=%.0f" % (gap * 1000, dur * 1000))
                if extra > 0.0 and extra <= MAX_GAP_SECONDS:
                    self._feed_silence_locked(min(extra, MAX_GAP_SECONDS), native)

            arr = np.frombuffer(in_data, dtype=np.int16).astype(np.float32) / 32768.0
            native_channels = max(1, int(self.device["channels"]))
            if native_channels > 1:
                arr = arr.reshape(-1, native_channels).mean(axis=1)
            if native != TARGET_SR:
                arr = resample_linear(arr, native, TARGET_SR)
            self._sink.feed(arr.astype(np.float32, copy=False))
        except Exception as e:  # keep the audio callback non-blocking
            self._status(f"[AUDIO_LOOPBACK] callback error={e}")
        return (None, self._pa_continue)

    # -- silence watchdog ----------------------------------------------------
    def _feed_silence_locked(self, seconds: float, native: int):
        samples = int(min(seconds, MAX_GAP_SECONDS) * TARGET_SR)
        if samples <= 0:
            return
        self._sink.feed(np.zeros(samples, dtype=np.float32))
        self.silence_seconds += samples / float(TARGET_SR)

    def _watchdog_loop(self):
        """WASAPI loopback emits NO callbacks while nothing is rendering.

        Keep the 250 ms chunk stream alive with digital silence only.  This is
        not an audio *source*: it is timeline filler for an idle render endpoint.
        """
        native = int(self.device["samplerate"])
        while not self._stop_watchdog.wait(0.05):
            with self._lock:
                if not self._running:
                    continue
                now = time.monotonic()
                since = now - (self._last_feed_wall or now)
                if since < IDLE_AFTER_SECONDS:
                    continue
                if not self._idle:
                    self._idle = True
                    self._status("[AUDIO_LOOPBACK] idle (no render activity; silence only)")
                fill = min(since, 0.25)
                self._last_feed_wall = now
                self._silence_since_cb += fill
                self._feed_silence_locked(fill, native)

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> bool:
        """Open + start shared-mode WASAPI loopback. True iff actually running."""
        pyaudio = _load_pyaudiowpatch()
        if pyaudio is None:
            print("[AUDIO_LOOPBACK] unavailable reason=pyaudiowpatch not installed")
            return False

        if self.device is None:
            self.device = find_wasapi_loopback_device()
        if self.device is None:
            return False
        if not self.device.get("is_loopback"):
            print("[AUDIO_LOOPBACK] refused non-loopback device")
            return False
        if not validate_wasapi_loopback(self.device):
            return False

        pa = pyaudio.PyAudio()
        self._pa = pa
        self._pa_continue = int(pyaudio.paContinue)
        native = int(self.device["samplerate"])
        channels = max(1, int(self.device["channels"]))
        print(f"[AUDIO_RESAMPLE] {native}Hz -> {TARGET_SR}Hz (wasapi-loopback)")
        if channels > 1:
            print(f"[AUDIO_RESAMPLE] downmix {channels}ch -> mono (wasapi-loopback)")

        try:
            self._stream = pa.open(
                format=pyaudio.paInt16,
                channels=channels,
                rate=native,
                frames_per_buffer=int(native * 0.1),
                input=True,                       # loopback endpoint is an input
                input_device_index=int(self.device["index"]),
                stream_callback=self._cb,
            )
            self._stream.start_stream()
            self._running = True
            self._last_cb_wall = time.monotonic()
            self._last_feed_wall = self._last_cb_wall
            self._stop_watchdog.clear()
            self._watchdog = threading.Thread(
                target=self._watchdog_loop, name="kiyra-loopback-watchdog",
                daemon=True)
            self._watchdog.start()
            self._status(
                "[AUDIO_LOOPBACK] started source=wasapi-loopback shared_mode=on "
                "flags=AUDCLNT_STREAMFLAGS_LOOPBACK endpoint=%r"
                % self.device["name"])
            return True
        except Exception as e:
            print(f"[AUDIO_LOOPBACK] unavailable reason={e}")
            self._close()
            return False

    def stop(self):
        self._running = False
        self._stop_watchdog.set()
        if self._watchdog is not None:
            self._watchdog.join(timeout=0.3)
            self._watchdog = None
        self._close()

    def _close(self):
        try:
            if self._stream is not None:
                self._stream.stop_stream()
                self._stream.close()
        except Exception:
            pass
        self._stream = None
        try:
            if self._pa is not None:
                self._pa.terminate()
        except Exception:
            pass
        self._pa = None
        self._running = False


class WASAPIDeviceMonitor:
    """Poll the default render endpoint and expose its verified loopback twin."""

    def __init__(self):
        pyaudio = _load_pyaudiowpatch()
        self._pa = pyaudio.PyAudio() if pyaudio is not None else None
        self._last_key = None
        self._last_info = None

    def poll(self) -> Optional[dict]:
        """Return the current verified loopback info (or None if absent)."""
        if self._pa is None:
            return None
        return find_wasapi_loopback_device(pa=self._pa, verbose=False)

    def changed(self, info: Optional[dict]) -> bool:
        key = (int(info["index"]), str(info["name"])) if info else None
        changed = key != self._last_key
        self._last_key = key
        self._last_info = info
        return changed

    def close(self):
        try:
            if self._pa is not None:
                self._pa.terminate()
        except Exception:
            pass
        self._pa = None
