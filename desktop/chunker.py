"""Audio chunker: aggregates float32 audio frames and emits WAV bytes per chunk."""
from __future__ import annotations

from typing import Callable, Optional
import threading
import queue
import numpy as np

from .audio import pcm16_from_float32, encode_wav_bytes


class AudioChunker:
    """Collect float32 mono audio buffers and produce WAV bytes of PCM16.

    Usage:
        chunker = AudioChunker(samplerate=16000, chunk_seconds=2.0, on_chunk=callback)
        chunker.start()
        chunker.push_frames(numpy_array)
        chunker.stop()  # flushes final partial chunk
    """

    def __init__(
        self,
        samplerate: int = 16000,
        chunk_seconds: float = 2.0,
        on_chunk: Optional[Callable[[bytes], None]] = None,
    ):
        self.samplerate = int(samplerate)
        self.chunk_seconds = float(chunk_seconds)
        self.on_chunk = on_chunk

        self._frames_per_chunk = int(self.samplerate * self.chunk_seconds)

        self._queue: queue.Queue[np.ndarray] = queue.Queue()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._buffer = np.zeros(0, dtype=np.float32)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self):
        # signal stop and wait for thread to flush
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None

    def push_frames(self, frames: np.ndarray):
        """Push a numpy float32 1-D array (mono) into the chunker."""
        if not isinstance(frames, np.ndarray):
            frames = np.array(frames, dtype=np.float32)
        else:
            frames = frames.astype(np.float32, copy=False)
        self._queue.put(frames)

    def _run_loop(self):
        try:
            while not self._stop_event.is_set() or not self._queue.empty():
                try:
                    frames = self._queue.get(timeout=0.05)
                    if frames.size == 0:
                        continue
                    self._buffer = np.concatenate([self._buffer, frames]) if self._buffer.size else frames
                except queue.Empty:
                    # nothing new; if stop requested, flush
                    if self._stop_event.is_set():
                        break
                    continue

                #Emit as many full chunks as possible
                while self._buffer.shape[0] >= self._frames_per_chunk:
                    chunk = self._buffer[: self._frames_per_chunk]
                    self._buffer = self._buffer[self._frames_per_chunk :]
                    # normalize if needed
                    maxv = float(np.max(np.abs(chunk))) if chunk.size else 1.0
                    if maxv > 1.0:
                        chunk = chunk / maxv
                    pcm = pcm16_from_float32(chunk)
                    wav = encode_wav_bytes(pcm, samplerate=self.samplerate)
                    if self.on_chunk:
                        try:
                            self.on_chunk(wav)
                        except Exception:
                            pass

            # On exit, flush remaining partial buffer if any
            if self._buffer.size:
                chunk = self._buffer
                maxv = float(np.max(np.abs(chunk))) if chunk.size else 1.0
                if maxv > 1.0:
                    chunk = chunk / maxv
                pcm = pcm16_from_float32(chunk)
                wav = encode_wav_bytes(pcm, samplerate=self.samplerate)
                if self.on_chunk:
                    try:
                        self.on_chunk(wav)
                    except Exception:
                        pass
            # clear buffer
            self._buffer = np.zeros(0, dtype=np.float32)
        finally:
            return
