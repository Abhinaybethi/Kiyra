"""
Windows System Audio Loopback & Microphone Capture
Captures interviewer voice from meeting apps (Zoom, Teams, Google Meet) via WASAPI loopback.
"""
from __future__ import annotations

import io
import queue
import sys
import threading
import time
import wave
from typing import Callable, Optional


class AudioLoopbackCapture:
    """
    Captures system audio loopback (what you hear through speakers/headphones)
    and candidate microphone, streaming chunks for real-time STT.
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        chunk_duration_sec: float = 3.0,
        on_audio_chunk: Optional[Callable[[bytes], None]] = None,
    ):
        self.sample_rate = sample_rate
        self.chunk_duration_sec = chunk_duration_sec
        self.on_audio_chunk = on_audio_chunk
        self.is_running = False
        self._thread: Optional[threading.Thread] = None
        self._audio_queue: queue.Queue = queue.Queue()

    def start(self):
        """Start background loopback capture."""
        if self.is_running:
            return
        self.is_running = True
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()

    def stop(self):
        """Stop background capture."""
        self.is_running = False
        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None

    def _capture_loop(self):
        """Loopback capture implementation using sounddevice / pyaudiowpatch / fallback."""
        try:
            import sounddevice as sd
            import numpy as np

            # Find WASAPI loopback device on Windows
            devices = sd.query_devices()
            loopback_device = None
            hostapis = sd.query_hostapis()
            wasapi_api_index = None

            for i, api in enumerate(hostapis):
                if "WASAPI" in api.get("name", "").upper():
                    wasapi_api_index = i
                    break

            if wasapi_api_index is not None:
                for idx, dev in enumerate(devices):
                    if (
                        dev.get("hostapi") == wasapi_api_index
                        and dev.get("max_input_channels", 0) > 0
                        and "loopback" in dev.get("name", "").lower()
                    ):
                        loopback_device = idx
                        break

            channels = 1

            def callback(indata, frames, time_info, status):
                if not self.is_running:
                    return
                # Convert float32 [-1.0, 1.0] to int16 PCM
                audio_int16 = (indata * 32767).astype(np.int16)
                self._audio_queue.put(audio_int16.tobytes())

            stream_kwargs = {
                "samplerate": self.sample_rate,
                "channels": channels,
                "dtype": "float32",
                "callback": callback,
                "blocksize": int(self.sample_rate * 0.5),  # 500ms blocks
            }
            if loopback_device is not None:
                stream_kwargs["device"] = loopback_device

            with sd.InputStream(**stream_kwargs):
                buffer = bytearray()
                bytes_per_chunk = int(self.sample_rate * 2 * self.chunk_duration_sec)  # 16-bit = 2 bytes/sample

                while self.is_running:
                    try:
                        data = self._audio_queue.get(timeout=0.5)
                        buffer.extend(data)
                        if len(buffer) >= bytes_per_chunk:
                            chunk = bytes(buffer[:bytes_per_chunk])
                            buffer = buffer[bytes_per_chunk:]
                            wav_bytes = self._pcm_to_wav(chunk, self.sample_rate, channels)
                            if self.on_audio_chunk:
                                self.on_audio_chunk(wav_bytes)
                    except queue.Empty:
                        continue

        except ImportError:
            while self.is_running:
                time.sleep(1.0)
        except Exception as e:
            print(f"[AudioLoopback] Capture error: {e}", file=sys.stderr)

    @staticmethod
    def _pcm_to_wav(pcm_bytes: bytes, sample_rate: int, channels: int) -> bytes:
        """Convert raw int16 PCM bytes to standard WAV bytes."""
        wav_io = io.BytesIO()
        with wave.open(wav_io, "wb") as wav_file:
            wav_file.setnchannels(channels)
            wav_file.setsampwidth(2)  # 16-bit
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(pcm_bytes)
        return wav_io.getvalue()
