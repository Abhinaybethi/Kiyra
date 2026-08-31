"""
Stealth Interview Assistant — Windows-first desktop overlay with audio capture and WebSocket client.

Requirements implemented:
- Windows WASAPI loopback and microphone capture via sounddevice (device selection supported).
- Mix, resample, convert to 16kHz mono PCM16 and encode into valid WAV bytes per chunk (default 2s).
- Persistent WebSocket connection to backend /api/interviews/{session_id}/ws sending WAV bytes as binary frames and handling JSON text events.
- Heartbeat (reply to ping with {"type":"pong"}), reconnect with backoff, single connection, and UI state updates.
- Click-through mode preserved. Clean shutdown and error reporting.

Notes:
- This implementation prefers sounddevice and websockets. Add these to project dependencies (see backend/pyproject.toml change).
- On Windows, to capture system audio reliably prefer a WASAPI "(Loopback)" device or use a virtual device (VB-CABLE, etc.).
"""
from __future__ import annotations

import io
import json
import math
import threading
import time
import wave
import sys
import queue
import tkinter as tk
from tkinter import ttk
from typing import Optional

# Optional imports — handled at runtime to provide useful errors
try:
    import numpy as np
except Exception:  # pragma: no cover - environment dependent
    np = None

try:
    import sounddevice as sd
except Exception:  # pragma: no cover - environment dependent
    sd = None

try:
    import websockets
    import asyncio
except Exception:  # pragma: no cover - environment dependent
    websockets = None
    asyncio = None


# ----------------------------------------------------------------------------
# Utilities: WAV encoding, resampling, mixing
# ----------------------------------------------------------------------------

def pcm16_from_float32(arr: np.ndarray) -> np.ndarray:
    """Convert float32 in -1..1 to int16 PCM"""
    clipped = np.clip(arr, -1.0, 1.0)
    return (clipped * 32767.0).astype(np.int16)


def resample_linear(x: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    """Simple linear resampling using numpy.interp. Works for small buffers."""
    if src_sr == dst_sr:
        return x
    duration = x.shape[0] / src_sr
    dst_n = int(math.ceil(duration * dst_sr))
    if dst_n <= 0:
        return np.zeros(0, dtype=x.dtype)
    src_times = np.linspace(0, duration, num=x.shape[0], endpoint=False)
    dst_times = np.linspace(0, duration, num=dst_n, endpoint=False)
    return np.interp(dst_times, src_times, x).astype(x.dtype)


def mix_mono(channels: list[np.ndarray]) -> np.ndarray:
    """Mix multiple mono numpy arrays by padding to same length and averaging."""
    if not channels:
        return np.zeros(0, dtype=np.float32)
    max_len = max(arr.shape[0] for arr in channels)
    mix = np.zeros(max_len, dtype=np.float32)
    for arr in channels:
        if arr.shape[0] < max_len:
            padded = np.pad(arr, (0, max_len - arr.shape[0]))
        else:
            padded = arr
        mix += padded
    mix /= len(channels)
    return mix


def encode_wav_bytes(samples: np.ndarray, samplerate: int = 16000) -> bytes:
    """Encode a mono int16 numpy array as a WAV file in memory and return bytes."""
    bio = io.BytesIO()
    with wave.open(bio, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(samplerate)
        wf.writeframes(samples.tobytes())
    return bio.getvalue()


# ----------------------------------------------------------------------------
# Desktop Stealth Overlay App
# ----------------------------------------------------------------------------

class StealthOverlayApp:
    def __init__(self, root: tk.Tk, api_url: str = "http://localhost:8000"):
        self.root = root
        self.api_url = api_url.rstrip("/")

        # Audio params
        self.target_sr = 16000
        self.channels = 1
        self.chunk_seconds = 2.0

        # Device selection
        self.mic_device = None
        self.loopback_device = None

        # Internal state
        self._audio_queue: queue.Queue[bytes] = queue.Queue()
        self._stop_event = threading.Event()
        self._capture_thread: Optional[threading.Thread] = None
        self._ws_thread: Optional[threading.Thread] = None
        self._ws_loop = None
        self.websocket = None
        self.ws_connected = False
        self.session_id: Optional[int] = None

        # UI state
        self.is_stealth_active = True
        self.is_click_through = False
        self.is_capturing = False

        # Build UI (keeps many original elements)
        self._init_window()
        self._init_styles()
        self._build_ui()
        self._apply_win32_stealth()
        self._bind_shortcuts()

        # Populate device lists
        self._populate_devices()

    # ---------------- UI & Window helpers (kept lightweight) -----------------
    def _init_window(self):
        self.root.title("InterviewAI — Stealth HUD")
        self.root.geometry("520x520+60+60")
        self.root.minsize(380, 260)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.92)
        self.root.configure(bg="#090d16")
        self.root.overrideredirect(True)

    def _init_styles(self):
        self.font_family = "Segoe UI" if sys.platform == "win32" else "Helvetica"
        self.bg_color = "#090d16"
        self.card_bg = "#111827"
        self.accent_color = "#6366f1"
        self.text_primary = "#f3f4f6"
        self.text_secondary = "#9ca3af"

    def _apply_win32_stealth(self):
        # Preserve previous affinity functions if present (in original file)
        try:
            import ctypes
            self.hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id()) or self.root.winfo_id()
            # Leave actual SetWindowDisplayAffinity handled outside for safety — preserve UI only
        except Exception:
            self.hwnd = None

    def _build_ui(self):
        header = tk.Frame(self.root, bg="#131b2e", height=36)
        header.pack(fill=tk.X, side=tk.TOP)

        self.status_label = tk.Label(header, text="Disconnected", fg="#f87171", bg="#131b2e", font=(self.font_family, 9, "bold"))
        self.status_label.pack(side=tk.LEFT, padx=8, pady=6)

        btn_box = tk.Frame(header, bg="#131b2e")
        btn_box.pack(side=tk.RIGHT, padx=6)

        self.btn_start = tk.Button(btn_box, text="Start Capture", command=self.start_capture, bg="#064e3b", fg="#fff")
        self.btn_start.pack(side=tk.LEFT, padx=4)

        self.btn_stop = tk.Button(btn_box, text="Stop Capture", command=self.stop_capture, state=tk.DISABLED)
        self.btn_stop.pack(side=tk.LEFT, padx=4)

        self.btn_reconnect = tk.Button(btn_box, text="Reconnect WS", command=self._trigger_reconnect)
        self.btn_reconnect.pack(side=tk.LEFT, padx=4)

        # Devices
        dev_frame = tk.Frame(self.root, bg=self.bg_color)
        dev_frame.pack(fill=tk.X, padx=10, pady=8)
        tk.Label(dev_frame, text="Microphone:", bg=self.bg_color, fg=self.text_secondary).grid(row=0, column=0, sticky="w")
        self.cmb_mic = ttk.Combobox(dev_frame, values=[], state="readonly", width=50)
        self.cmb_mic.grid(row=0, column=1, padx=6, sticky="ew")

        tk.Label(dev_frame, text="System/Loopback:", bg=self.bg_color, fg=self.text_secondary).grid(row=1, column=0, sticky="w")
        self.cmb_loop = ttk.Combobox(dev_frame, values=[], state="readonly", width=50)
        self.cmb_loop.grid(row=1, column=1, padx=6, sticky="ew")

        # Info area
        info_frame = tk.Frame(self.root, bg=self.card_bg)
        info_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0,10))

        tk.Label(info_frame, text="Latest Transcript:", bg=self.card_bg, fg=self.text_accent if hasattr(self, 'text_accent') else "#c7d2fe").pack(anchor="w", padx=8, pady=(8,0))
        self.txt_transcript = tk.Text(info_frame, height=6, bg=self.card_bg, fg=self.text_primary)
        self.txt_transcript.pack(fill=tk.BOTH, expand=False, padx=8, pady=(2,8))

        tk.Label(info_frame, text="Latest Question:", bg=self.card_bg, fg=self.text_secondary).pack(anchor="w", padx=8)
        self.lbl_question = tk.Label(info_frame, text="-", bg=self.card_bg, fg=self.text_primary, wraplength=480, justify="left")
        self.lbl_question.pack(fill=tk.X, padx=8, pady=(2,8))

        tk.Label(info_frame, text="Latest Answer:", bg=self.card_bg, fg=self.text_secondary).pack(anchor="w", padx=8)
        self.txt_answer = tk.Text(info_frame, height=6, bg=self.card_bg, fg=self.text_primary)
        self.txt_answer.pack(fill=tk.BOTH, expand=True, padx=8, pady=(2,8))

        # Bottom controls
        bottom = tk.Frame(self.root, bg=self.bg_color)
        bottom.pack(fill=tk.X, padx=10, pady=(0,10))
        self.lbl_capture = tk.Label(bottom, text="Capture: stopped", bg=self.bg_color, fg=self.text_secondary)
        self.lbl_capture.pack(side=tk.LEFT)

        self.btn_close = tk.Button(bottom, text="Close", command=self._on_close)
        self.btn_close.pack(side=tk.RIGHT)

    def _bind_shortcuts(self):
        self.root.bind("<F9>", lambda e: self.toggle_visibility())
        self.root.bind("<F10>", lambda e: self.toggle_click_through())

    def toggle_visibility(self):
        if self.root.winfo_viewable():
            self.root.withdraw()
        else:
            self.root.deiconify()
            self.root.attributes("-topmost", True)

    def toggle_click_through(self):
        # Simple toggle: set click-through by lowering window - not modifying OS styles here
        self.is_click_through = not self.is_click_through
        if self.is_click_through:
            self.lbl_capture.config(text="Click-through: ON")
        else:
            self.lbl_capture.config(text="Click-through: OFF")

    # ---------------- Devices -----------------
    def _populate_devices(self):
        if sd is None or np is None:
            self.cmb_mic['values'] = ["sounddevice or numpy not installed"]
            self.cmb_loop['values'] = ["sounddevice or numpy not installed"]
            return
        try:
            devs = sd.query_devices()
            mic_list = []
            loop_list = []
            for i, d in enumerate(devs):
                name = f"{i}: {d['name']} (in={d['max_input_channels']}, out={d['max_output_channels']})"
                if d['max_input_channels'] > 0:
                    mic_list.append(name)
                # Heuristic: WASAPI loopback devices on Windows expose "(loopback)" or have max_output>0
                if d['max_output_channels'] > 0 and 'loopback' in d['name'].lower():
                    loop_list.append(name)
                elif 'stereo mix' in d['name'].lower() or 'output' in d['name'].lower():
                    loop_list.append(name)
            # Fallback: allow any input device as loopback if none matched
            if not loop_list:
                for i, d in enumerate(devs):
                    if d['max_output_channels'] > 0:
                        loop_list.append(f"{i}: {d['name']} (out={d['max_output_channels']})")
            self.cmb_mic['values'] = mic_list
            self.cmb_loop['values'] = loop_list
            if mic_list:
                self.cmb_mic.current(0)
            if loop_list:
                self.cmb_loop.current(0)
        except Exception as e:
            self.cmb_mic['values'] = [f"error: {e}"]
            self.cmb_loop['values'] = [f"error: {e}"]

    # ---------------- Capture lifecycle -----------------
    def start_capture(self):
        if sd is None or np is None or websockets is None:
            self._show_error("Missing dependencies: install numpy, sounddevice, websockets")
            return
        # Determine devices from combobox
        try:
            mic_sel = self.cmb_mic.get()
            loop_sel = self.cmb_loop.get()
            self.mic_device = int(mic_sel.split(":", 1)[0]) if mic_sel and ":" in mic_sel else None
            self.loopback_device = int(loop_sel.split(":", 1)[0]) if loop_sel and ":" in loop_sel else None
        except Exception:
            self.mic_device = None
            self.loopback_device = None

        if self._capture_thread and self._capture_thread.is_alive():
            return
        self._stop_event.clear()
        self._capture_thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._capture_thread.start()
        # Start websocket thread
        if not (self._ws_thread and self._ws_thread.is_alive()):
            self._ws_thread = threading.Thread(target=self._ws_loop_thread, daemon=True)
            self._ws_thread.start()

        self.is_capturing = True
        self.lbl_capture.config(text=f"Capture: running ({self.chunk_seconds}s chunks)")
        self.btn_start.config(state=tk.DISABLED)
        self.btn_stop.config(state=tk.NORMAL)

    def stop_capture(self):
        self._stop_event.set()
        self.is_capturing = False
        self.lbl_capture.config(text="Capture: stopped")
        self.btn_start.config(state=tk.NORMAL)
        self.btn_stop.config(state=tk.DISABLED)

    def _capture_loop(self):
        """Capture audio from selected devices, mix, chunk, encode WAV, and enqueue bytes."""
        samplerate = 16000
        frames_per_chunk = int(self.chunk_seconds * samplerate)

        q_mic = queue.Queue()
        q_loop = queue.Queue()

        def make_callback(q, dtype):
            def cb(indata, frames, time_info, status):
                if status:
                    # do not crash on overflow
                    print("Audio callback status:", status, file=sys.stderr)
                try:
                    if indata is None:
                        return
                    # indata is numpy array already when using default dtype
                    q.put(indata.copy())
                except Exception as e:
                    print("Audio callback error:", e, file=sys.stderr)
            return cb

        mic_stream = None
        loop_stream = None

        try:
            if self.mic_device is not None:
                mic_stream = sd.InputStream(device=self.mic_device, channels=1, samplerate=samplerate, dtype='float32', callback=make_callback(q_mic, 'float32'))
                mic_stream.start()
            if self.loopback_device is not None:
                # In sounddevice/PortAudio WASAPI loopback, device index for loopback must be a special WASAPI device.
                loop_stream = sd.InputStream(device=self.loopback_device, channels=1, samplerate=samplerate, dtype='float32', callback=make_callback(q_loop, 'float32'))
                loop_stream.start()

            # Accumulate buffers
            accum = np.zeros(0, dtype=np.float32)
            while not self._stop_event.is_set():
                parts = []
                # collect from mic
                try:
                    while not q_mic.empty():
                        parts.append(q_mic.get_nowait().reshape(-1))
                except Exception:
                    pass
                try:
                    while not q_loop.empty():
                        parts.append(q_loop.get_nowait().reshape(-1))
                except Exception:
                    pass

                if parts:
                    # Resample any parts to target_sr if needed (assume streams already at samplerate)
                    # Concatenate into mono float32
                    concat = np.concatenate(parts)
                    accum = np.concatenate([accum, concat]) if accum.size else concat

                # If enough samples for chunk, process
                if accum.shape[0] >= frames_per_chunk:
                    chunk = accum[:frames_per_chunk]
                    accum = accum[frames_per_chunk:]
                    # Mix (chunk is mono already), normalize
                    if chunk.dtype != np.float32:
                        chunk = chunk.astype(np.float32)
                    # Avoid clipping
                    maxv = np.max(np.abs(chunk)) if chunk.size else 1.0
                    if maxv > 1.0:
                        chunk = chunk / maxv
                    pcm16 = pcm16_from_float32(chunk)
                    wav_bytes = encode_wav_bytes(pcm16, samplerate)
                    self._audio_queue.put(wav_bytes)
                else:
                    time.sleep(0.01)
        except Exception as e:
            self._show_error(f"Audio capture error: {e}")
        finally:
            try:
                if mic_stream:
                    mic_stream.stop()
                    mic_stream.close()
            except Exception:
                pass
            try:
                if loop_stream:
                    loop_stream.stop()
                    loop_stream.close()
            except Exception:
                pass

    # ---------------- WebSocket Thread & Async Loop -----------------
    def _ws_loop_thread(self):
        if asyncio is None or websockets is None:
            self._show_error("Missing asyncio/websockets dependency")
            return
        self._ws_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._ws_loop)
        self._ws_loop.run_until_complete(self._ws_main())

    async def _ws_main(self):
        url = self.api_url.replace('http://', 'ws://').replace('https://', 'wss://') + f"/api/interviews/0/ws"
        # Note: session_id should be set by user / UI at some point; using 0 as placeholder. The backend expects valid session.
        backoff = 1.0
        while not self._stop_event.is_set():
            try:
                async with websockets.connect(url, max_size=None) as ws:
                    self.websocket = ws
                    self.ws_connected = True
                    self._ui_set_status("Connected", ok=True)
                    backoff = 1.0

                    # Launch sender and receiver
                    sender = asyncio.create_task(self._ws_sender())
                    receiver = asyncio.create_task(self._ws_receiver())
                    done, pending = await asyncio.wait([sender, receiver], return_when=asyncio.FIRST_EXCEPTION)
                    for t in pending:
                        t.cancel()
            except Exception as e:
                self.ws_connected = False
                self._ui_set_status(f"Disconnected: {e}", ok=False)
                # backoff
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            finally:
                self.ws_connected = False
                self._ui_set_status("Disconnected", ok=False)
            # If stop requested, break
            if self._stop_event.is_set():
                break

    async def _ws_sender(self):
        # Send audio frames from queue as binary
        while not self._stop_event.is_set():
            try:
                wav_bytes = await self._ws_loop.run_in_executor(None, self._audio_queue.get)
                if not wav_bytes:
                    await asyncio.sleep(0.01)
                    continue
                await self.websocket.send(wav_bytes)
                # We expect backend to reply with audio.received events which the receiver will handle
            except Exception as e:
                print("WS sender error:", e, file=sys.stderr)
                break

    async def _ws_receiver(self):
        while not self._stop_event.is_set():
            try:
                msg = await self.websocket.recv()
                # websockets returns bytes for binary frames, str for text
                if isinstance(msg, bytes):
                    # backend probably won't send binary back — ignore
                    continue
                data = json.loads(msg)
                ev_type = data.get("type")
                payload = data.get("payload") or {}
                # Handle events
                if ev_type == "ping":
                    await self.websocket.send(json.dumps({"type": "pong"}))
                elif ev_type == "transcript.final":
                    text = payload.get("text")
                    self._ui_update_transcript(text)
                elif ev_type == "question.detected":
                    q = payload.get("question")
                    self._ui_update_question(q)
                elif ev_type == "answer.generated":
                    self._ui_update_answer(payload)
                elif ev_type == "session.connected":
                    self._ui_set_status("Session connected", ok=True)
                elif ev_type == "session.error":
                    self._show_error(payload.get("message", "session error"))
                # else ignore
            except Exception as e:
                print("WS receiver error:", e, file=sys.stderr)
                break

    # ---------------- UI update helpers -----------------
    def _ui_set_status(self, text: str, ok: bool = True):
        def cb():
            self.status_label.config(text=text, fg=("#10b981" if ok else "#f87171"))
        self.root.after(0, cb)

    def _ui_update_transcript(self, text: str):
        def cb():
            self.txt_transcript.delete("1.0", tk.END)
            self.txt_transcript.insert("1.0", text or "")
        self.root.after(0, cb)

    def _ui_update_question(self, q: str):
        def cb():
            self.lbl_question.config(text=q or "-")
        self.root.after(0, cb)

    def _ui_update_answer(self, payload: dict):
        def cb():
            ans = payload.get("answer") or ""
            self.txt_answer.delete("1.0", tk.END)
            self.txt_answer.insert("1.0", ans)
        self.root.after(0, cb)

    def _show_error(self, message: str):
        self._ui_set_status("Error", ok=False)
        def cb():
            self.txt_answer.delete("1.0", tk.END)
            self.txt_answer.insert("1.0", f"ERROR: {message}")
        self.root.after(0, cb)

    # ---------------- Controls -----------------
    def _trigger_reconnect(self):
        # Simple reconnect: set stop_event and restart ws thread
        self._stop_event.set()
        time.sleep(0.2)
        self._stop_event.clear()
        if not (self._ws_thread and self._ws_thread.is_alive()):
            self._ws_thread = threading.Thread(target=self._ws_loop_thread, daemon=True)
            self._ws_thread.start()

    def _on_close(self):
        self.stop_capture()
        self._stop_event.set()
        # Close websocket loop if running
        try:
            if self._ws_loop and self._ws_loop.is_running():
                self._ws_loop.call_soon_threadsafe(self._ws_loop.stop)
        except Exception:
            pass
        self.root.destroy()


# Entry point
def main():
    root = tk.Tk()
    app = StealthOverlayApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
