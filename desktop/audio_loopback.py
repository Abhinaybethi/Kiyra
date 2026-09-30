"""
Windows system-audio loopback + microphone capture for the Kiyra Stealth Overlay.

Two fully independent capture sources:

  * ``LoopbackCapture`` — WASAPI system-audio loopback via ``pyaudiowpatch``.
    This captures whatever Windows is playing out through the default playback
    device (speakers OR headphones).  This is the interviewer-voice path for the
    "wearing headphones" scenario: meeting audio (Zoom/Teams/Meet) is played out
    the headphones and we capture it at the OS level, so the microphone is not
    needed for the interviewer's voice.
  * ``MicrophoneCapture`` — classic microphone capture via ``sounddevice``.

Both sources emit identical chunks: **16000 Hz, mono, PCM16, 250 ms** WAV bytes
(4000 samples), i.e. exactly the WAV contract the Kiyra backend expects on the
``/api/interviews/{id}/ws`` binary stream.

Design rules:
  * The PortAudio/WASAPI audio callback NEVER blocks: per-source numpy
    accumulation + chunking only, plus a drop-oldest ``put_nowait`` into a
    bounded queue.
  * Loopback is captured at the device's native sample rate (typically 48 kHz)
    and resampled to 16 kHz with the repository's existing lightweight linear
    resampler.  Stereo is down-mixed to mono.
  * A failure in one source never affects the other; the overlay orchestrates
    fallback (``microphone_only_fallback`` / ``loopback_only`` /
    ``no_audio_sources_available``).
  * sounddevice / pyaudiowpatch / numpy are imported lazily so the module can be
    unit-tested on machines without audio hardware.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, List, Optional

import numpy as np

from audio import encode_wav_bytes, pcm16_from_float32, resample_linear

TARGET_SR = 16000
CHUNK_SECONDS = 0.25
CHUNK_SAMPLES = int(TARGET_SR * CHUNK_SECONDS)  # 4000 samples


def _load_pyaudiowpatch():
    """Import pyaudiowpatch lazily (may not be installed on every machine)."""
    try:
        import pyaudiowpatch as pyaudio

        return pyaudio
    except Exception:
        return None


def _load_sounddevice():
    """Import sounddevice lazily (may not be installed on every machine)."""
    try:
        import sounddevice as sd

        return sd
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
# Device discovery
# ─────────────────────────────────────────────────────────────────────────────

def find_wasapi_loopback_device() -> Optional[dict]:
    """Discover the WASAPI loopback endpoint for the *current default* playback
    device (headphones OR speakers — whatever Windows is using right now).

    This does NOT rely on a device named "loopback"/"stereo mix": pyaudiowpatch
    exposes a []-Loopback input endpoint for every active WASAPI playback
    endpoint, and we prefer the default one.

    Returns ``dict(index, name, samplerate, channels)`` or ``None``.
    """
    pyaudio = _load_pyaudiowpatch()
    if pyaudio is None:
        print("[AUDIO_LOOPBACK] unavailable reason=pyaudiowpatch not installed")
        return None

    pa = pyaudio.PyAudio()
    try:
        dev = None
        reason = "no WASAPI loopback endpoint exposed by the system"
        try:
            dev = pa.get_default_wasapi_loopback()
        except Exception as e:  # pragma: no cover - platform dependent
            reason = f"get_default_wasapi_loopback failed: {e}"

        if dev is None:
            try:  # Fallback: first loopback endpoint the OS reports.
                dev = next(pa.get_loopback_device_info_generator(), None)
            except Exception as e:  # pragma: no cover - platform dependent
                reason = f"loopback enumeration failed: {e}"

        if dev is None:
            print(f"[AUDIO_LOOPBACK] unavailable reason={reason}")
            return None

        info = {
            "index": int(dev["index"]),
            "name": str(dev["name"]),
            "samplerate": int(dev.get("defaultSampleRate") or 48000),
            "channels": int(dev.get("maxInputChannels") or 2) or 2,
        }
        print(
            "[AUDIO_DEVICE] loopback output=%r index=%d native_rate=%d channels=%d"
            % (info["name"], info["index"], info["samplerate"], info["channels"])
        )
        return info
    finally:
        try:
            pa.terminate()
        except Exception:
            pass


def validate_wasapi_loopback(info: dict) -> bool:
    """Attempt to actually OPEN + START the given WASAPI loopback stream.

    A device listing that "looks loopback-ish" is not proof it can be opened —
    this does a real open so ``[AUDIO_LOOPBACK] started`` is always honest.
    """
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
        stream.start_stream()
        time.sleep(0.25)
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


def enumerate_microphones() -> List[dict]:
    """List capture-capable microphone devices via sounddevice."""
    sd = _load_sounddevice()
    if sd is None:
        return []
    mics: List[dict] = []
    try:
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] > 0:
                mics.append({
                    "index": i,
                    "name": d["name"],
                    "channels": d["max_input_channels"],
                    "samplerate": d["default_samplerate"],
                })
    except Exception as e:  # pragma: no cover - platform dependent
        print(f"[AUDIO_DEVICE] mic enumeration failed: {e}")
    return mics


# ─────────────────────────────────────────────────────────────────────────────
# Chunk assembly (shared by both sources) — callback-safe numpy only.
# ─────────────────────────────────────────────────────────────────────────────

class _ChunkSink:
    """Accumulate mono float32 @16 kHz and emit 250 ms PCM16 WAV chunks."""

    def __init__(self, on_chunk: Optional[Callable[[bytes, ], None]], chunk_samples: int = CHUNK_SAMPLES):
        self.on_chunk = on_chunk
        self.chunk_samples = chunk_samples
        self._acc = np.zeros(0, dtype=np.float32)
        self.chunks_made = 0

    def feed(self, mono: np.ndarray):
        if mono is None or mono.size == 0:
            return
        self._acc = np.concatenate([self._acc, mono])
        while self._acc.size >= self.chunk_samples:
            chunk = self._acc[: self.chunk_samples]
            self._acc = self._acc[self.chunk_samples:]
            wav = encode_wav_bytes(pcm16_from_float32(chunk), TARGET_SR)
            self.chunks_made += 1
            if self.on_chunk is not None:
                try:
                    self.on_chunk(wav)
                except Exception:
                    pass


class _MixingChunkSink:
    """Pair per-source 250 ms float32 buffers and emit sample-mixed WAV chunks.

    Two independent captures (mic + loopback) each produce a continuous
    16 kHz stream.  Emitting each source's chunks straight into one queue
    INTERLEAVES them (loud speech / near-silence / loud speech / ...) which
    Whisper's VAD rejects as non-speech — proven to yield empty transcripts.
    This sink instead keeps one buffer per source, pairs them positionally
    (both advance at the same 16 kHz rate, so pairing preserves each source's
    timeline with a fixed start offset) and averages the samples, emitting a
    single mixed stream.

    A source that stops feeding for > ``stale_after`` seconds is dropped from
    pairing (others keep streaming); its backlog is trimmed so memory stays
    bounded.  With only one source ever active, this degrades to that source
    alone.
    """

    def __init__(
        self,
        on_chunk: Optional[Callable[[bytes], None]],
        n_sources: int = 2,
        chunk_samples: int = CHUNK_SAMPLES,
        stale_after: float = 0.5,
        source_gates: Optional[dict] = None,
        source_names: Optional[tuple] = None,
    ):
        self.on_chunk = on_chunk
        self.n_sources = n_sources
        self.chunk_samples = chunk_samples
        self.stale_after = stale_after
        # {source_index: rms_threshold} — blocks below the gate are ZEROED
        # (not dropped, so per-source timelines stay aligned for pairing).
        # Align the gate with the backend EnergyVAD speech_start_threshold
        # (0.015): gated-out mic noise can never start/extend a false
        # utterance, while real mic speech passes through untouched.
        self.source_gates = dict(source_gates or {})
        self.source_names = tuple(source_names) if source_names else \
            tuple(f"src{i}" for i in range(n_sources))
        self._bufs = [np.zeros(0, dtype=np.float32) for _ in range(n_sources)]
        self._started = [False] * n_sources
        self._last_feed = [0.0] * n_sources
        self._lock = threading.Lock()
        self.chunks_made = 0
        # Diagnostics: per-source + mixed RMS (rate-limited, no raw audio).
        self._feed_counts = [0] * n_sources
        self._last_src_diag = [0.0] * n_sources
        self._last_mixed_diag = 0.0

    def for_source(self, src: int):
        """Adapter exposing ``feed(mono)`` bound to one source index."""
        sink = self

        class _Adapter:
            def feed(self, mono: np.ndarray):
                sink.feed(src, mono)

        return _Adapter()

    def feed(self, src: int, mono: np.ndarray):
        if mono is None or mono.size == 0:
            return
        rms = float(np.sqrt(np.mean(np.square(mono.astype(np.float64)))))
        gate = self.source_gates.get(src)
        gated = False
        if gate is not None and rms < gate:
            mono = np.zeros_like(mono)
            gated = True
        self._feed_counts[src] += 1
        now = time.monotonic()
        if now - self._last_src_diag[src] >= 2.0:
            self._last_src_diag[src] = now
            print(
                f"[AUDIO_CAPTURE] src={self.source_names[src]} "
                f"frame={self._feed_counts[src]} samples={mono.size} "
                f"rms={rms:.4f}" + (" gated=1" if gated else "")
            )
        with self._lock:
            buf = self._bufs[src]
            self._bufs[src] = np.concatenate([buf, mono]) if buf.size else mono.astype(np.float32, copy=False)
            self._started[src] = True
            self._last_feed[src] = time.monotonic()
            self._flush()

    def _flush(self):
        now = time.monotonic()
        active = [
            i for i in range(self.n_sources)
            if self._started[i] and (now - self._last_feed[i]) <= self.stale_after
        ]
        if not active:
            return
        # Bound memory for stalled sources: keep only their newest chunk.
        for i in range(self.n_sources):
            if i not in active and self._bufs[i].size > self.chunk_samples * 4:
                self._bufs[i] = self._bufs[i][-self.chunk_samples:]
        while min(self._bufs[i].size for i in active) >= self.chunk_samples:
            mixed = None
            for i in active:
                seg = self._bufs[i][: self.chunk_samples]
                self._bufs[i] = self._bufs[i][self.chunk_samples:]
                mixed = seg if mixed is None else mixed + seg
            mixed = mixed / float(len(active))
            wav = encode_wav_bytes(pcm16_from_float32(mixed), TARGET_SR)
            self.chunks_made += 1
            now_m = time.monotonic()
            if now_m - self._last_mixed_diag >= 3.0:
                self._last_mixed_diag = now_m
                mrms = float(np.sqrt(np.mean(np.square(mixed.astype(np.float64)))))
                via = "+".join(self.source_names[i] for i in active)
                print(
                    f"[AUDIO_CAPTURE] src=mixed frame={self.chunks_made} "
                    f"samples={mixed.size} rms={mrms:.4f} via={via}"
                )
            if self.on_chunk is not None:
                try:
                    self.on_chunk(wav)
                except Exception:
                    pass


# ─────────────────────────────────────────────────────────────────────────────
# Loopback capture
# ─────────────────────────────────────────────────────────────────────────────

class LoopbackCapture:
    """WASAPI system-audio loopback capture (native rate → 16 kHz mono chunks).

    ``device_info`` is the dict from :func:`find_wasapi_loopback_device`.
    """

    def __init__(
        self,
        on_chunk: Optional[Callable[[bytes], None]] = None,
        device_info: Optional[dict] = None,
        sink: Optional[object] = None,
    ):
        self.on_chunk = on_chunk
        self.device = device_info
        self._sink = sink if sink is not None else _ChunkSink(on_chunk)
        self._pa = None
        self._stream = None
        self._running = False

    def start(self) -> bool:
        """Open + start loopback. Returns True iff actually capturing."""
        pyaudio = _load_pyaudiowpatch()
        if pyaudio is None:
            print("[AUDIO_LOOPBACK] unavailable reason=pyaudiowpatch not installed")
            return False

        if self.device is None:
            self.device = find_wasapi_loopback_device()
        if self.device is None:
            return False
        if not validate_wasapi_loopback(self.device):
            return False

        pa = pyaudio.PyAudio()
        self._pa = pa
        native = int(self.device["samplerate"])
        channels = max(1, int(self.device["channels"]))
        native_channels = channels

        print(f"[AUDIO_RESAMPLE] {native}Hz -> {TARGET_SR}Hz (loopback)")
        if channels > 1:
            print(f"[AUDIO_RESAMPLE] downmix {channels}ch -> mono (loopback)")

        def _cb(in_data, frame_count, time_info, status):
            try:
                arr = np.frombuffer(in_data, dtype=np.int16).astype(np.float32) / 32768.0
                if native_channels > 1:
                    arr = arr.reshape(-1, native_channels).mean(axis=1)
                if native != TARGET_SR:
                    arr = resample_linear(arr, native, TARGET_SR)
                self._sink.feed(arr.astype(np.float32, copy=False))
            except Exception:
                pass
            return (None, pyaudio.paContinue)

        try:
            self._stream = pa.open(
                format=pyaudio.paInt16,
                channels=native_channels,
                rate=native,
                frames_per_buffer=int(native * 0.1),
                input=True,
                input_device_index=int(self.device["index"]),
                stream_callback=_cb,
            )
            self._stream.start_stream()
            self._running = True
            print("[AUDIO_LOOPBACK] started")
            return True
        except Exception as e:
            print(f"[AUDIO_LOOPBACK] unavailable reason={e}")
            self._close()
            return False

    def stop(self):
        self._running = False
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


# ─────────────────────────────────────────────────────────────────────────────
# Microphone capture
# ─────────────────────────────────────────────────────────────────────────────

class MicrophoneCapture:
    """Microphone capture via sounddevice. Already captured at 16 kHz mono.

    Fully independent from ``LoopbackCapture``: a mic failure never affects
    loopback and vice versa.
    """

    def __init__(
        self,
        device_index: Optional[int] = None,
        on_chunk: Optional[Callable[[bytes], None]] = None,
        samplerate: int = TARGET_SR,
        sink: Optional[object] = None,
    ):
        self.device_index = device_index
        self.on_chunk = on_chunk
        self.samplerate = samplerate
        self._sink = sink if sink is not None else _ChunkSink(on_chunk)
        self._stream = None
        self._running = False

    def start(self) -> bool:
        sd = _load_sounddevice()
        if sd is None:
            print("[AUDIO_MIC] unavailable reason=sounddevice not installed")
            return False

        blocksize = int(0.1 * self.samplerate)  # 100 ms callbacks

        def _cb(indata, frames, time_info, status):
            try:
                self._sink.feed(indata.reshape(-1).astype(np.float32, copy=False))
            except Exception:
                pass

        try:
            self._stream = sd.InputStream(
                device=self.device_index,
                channels=1,
                samplerate=self.samplerate,
                blocksize=blocksize,
                dtype="float32",
                callback=_cb,
            )
            self._stream.start()
            self._running = True
            print(f"[AUDIO_MIC] started (device {self.device_index})")
            return True
        except Exception as e:
            print(f"[AUDIO_MIC] unavailable reason={e}")
            return False

    def stop(self):
        self._running = False
        try:
            if self._stream is not None:
                self._stream.stop()
                self._stream.close()
        except Exception:
            pass
        self._stream = None