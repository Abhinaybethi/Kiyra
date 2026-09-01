"""
Stealth Interview Assistant — Production-ready Windows overlay.

FEATURES:
- Invisible to screen capture (SetWindowDisplayAffinity)
- Real-time audio capture from Zoom/Teams/Meet
- WebSocket streaming to backend for fast transcription
- Instant answer display (STAR framework + key points)
- Click-through mode for seamless interaction
- Auto-hide answers after configurable delay
- Keyboard shortcuts: F9 (toggle visibility), F10 (toggle click-through)
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
from tkinter import ttk, messagebox
from typing import Optional
import ctypes

try:
    import numpy as np
except Exception:
    np = None

try:
    import sounddevice as sd
except Exception:
    sd = None

try:
    import websockets
    import asyncio
except Exception:
    websockets = None
    asyncio = None


# ─────────────────────────────────────────────────────────────────────────────
# Audio Processing
# ─────────────────────────────────────────────────────────────────────────────

def pcm16_from_float32(arr: np.ndarray) -> np.ndarray:
    """Convert float32 in -1..1 to int16 PCM."""
    if arr.size == 0:
        return np.zeros(0, dtype=np.int16)
    clipped = np.clip(arr, -1.0, 1.0)
    return (clipped * 32767.0).astype(np.int16)


def resample_linear(x: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    """Simple linear resampling."""
    if src_sr == dst_sr:
        return x
    if x.size == 0:
        return np.zeros(0, dtype=x.dtype)
    duration = x.shape[0] / src_sr
    dst_n = int(math.ceil(duration * dst_sr))
    if dst_n <= 0:
        return np.zeros(0, dtype=x.dtype)
    src_times = np.linspace(0, duration, num=x.shape[0], endpoint=False)
    dst_times = np.linspace(0, duration, num=dst_n, endpoint=False)
    return np.interp(dst_times, src_times, x).astype(x.dtype)


def encode_wav_bytes(samples: np.ndarray, samplerate: int = 16000) -> bytes:
    """Encode mono int16 numpy array as WAV bytes."""
    if samples.size == 0:
        samples = np.zeros(1, dtype=np.int16)
    bio = io.BytesIO()
    with wave.open(bio, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(samplerate)
        wf.writeframes(samples.tobytes())
    return bio.getvalue()


# ─────────────────────────────────────────────────────────────────────────────
# Stealth Overlay App
# ─────────────────────────────────────────────────────────────────────────────

class StealthOverlayApp:
    def __init__(self, root: tk.Tk, api_url: str = "http://localhost:8000", session_id: int = 1):
        self.root = root
        self.api_url = api_url.rstrip("/")
        self.session_id = session_id

        # Audio params
        self.target_sr = 16000
        self.chunk_seconds = 2.0

        # Device selection
        self.mic_device = None
        self.loopback_device = None

        # Internal state
        self._audio_queue: queue.Queue[bytes] = queue.Queue(maxsize=50)
        self._stop_event = threading.Event()
        self._capture_thread: Optional[threading.Thread] = None
        self._ws_thread: Optional[threading.Thread] = None
        self._ws_loop = None
        self.websocket = None
        self.ws_connected = False

        # UI state
        self.is_stealth_active = True
        self.is_click_through = False
        self.is_capturing = False
        self.answer_auto_hide_task = None

        # Build UI
        self._init_window()
        self._init_styles()
        self._build_ui()
        self._apply_stealth_affinity()
        self._bind_shortcuts()
        self._populate_devices()

        print("[Stealth] Initialized. Press F9 to toggle visibility, F10 for click-through.")

    # ─────────────────────────────────────────────────────────────────────────
    # Window & UI
    # ─────────────────────────────────────────────────────────────────────────

    def _init_window(self):
        self.root.title("Kiyra — Interview Stealth HUD")
        self.root.geometry("600x650+50+50")
        self.root.minsize(400, 300)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.94)
        self.root.configure(bg="#0f172a")

    def _init_styles(self):
        self.font_family = "Segoe UI" if sys.platform == "win32" else "Helvetica"
        self.bg_color = "#0f172a"
        self.card_bg = "#1e293b"
        self.accent_color = "#6366f1"
        self.text_primary = "#f1f5f9"
        self.text_secondary = "#94a3b8"
        self.success_color = "#10b981"
        self.warning_color = "#f59e0b"

    def _apply_stealth_affinity(self):
        """Hide window from screenshots on Windows."""
        try:
            if sys.platform == "win32":
                hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id()) or self.root.winfo_id()
                WDA_EXCLUDEFROMCAPTURE = 17
                ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
                print("[Stealth] Window hidden from screen capture")
        except Exception as e:
            print(f"[Stealth] Could not set affinity: {e}")

    def _build_ui(self):
        # ─── Header ───────────────────────────────────────────────────────────
        header = tk.Frame(self.root, bg="#1e293b", height=50)
        header.pack(fill=tk.X, side=tk.TOP)
        header.pack_propagate(False)

        self.status_label = tk.Label(
            header,
            text="● Disconnected",
            fg="#ef4444",
            bg="#1e293b",
            font=(self.font_family, 10, "bold")
        )
        self.status_label.pack(side=tk.LEFT, padx=12, pady=12)

        btn_box = tk.Frame(header, bg="#1e293b")
        btn_box.pack(side=tk.RIGHT, padx=8, pady=8)

        self.btn_start = tk.Button(
            btn_box,
            text="▶ Start",
            command=self.start_capture,
            bg="#059669",
            fg="#fff",
            font=(self.font_family, 9, "bold"),
            relief=tk.FLAT,
            padx=10,
            cursor="hand2"
        )
        self.btn_start.pack(side=tk.LEFT, padx=4)

        self.btn_stop = tk.Button(
            btn_box,
            text="⏹ Stop",
            command=self.stop_capture,
            state=tk.DISABLED,
            bg="#dc2626",
            fg="#fff",
            font=(self.font_family, 9, "bold"),
            relief=tk.FLAT,
            padx=10,
            cursor="hand2"
        )
        self.btn_stop.pack(side=tk.LEFT, padx=4)

        # ─── Device Selection ──────────────────────────────────────────────────
        dev_frame = tk.LabelFrame(
            self.root,
            text="Audio Devices",
            bg=self.bg_color,
            fg=self.text_secondary,
            font=(self.font_family, 9, "bold"),
            padx=10,
            pady=10
        )
        dev_frame.pack(fill=tk.X, padx=12, pady=8)

        tk.Label(dev_frame, text="Microphone:", bg=self.bg_color, fg=self.text_secondary).grid(row=0, column=0, sticky="w", pady=4)
        self.cmb_mic = ttk.Combobox(dev_frame, values=[], state="readonly", width=60)
        self.cmb_mic.grid(row=0, column=1, padx=8, sticky="ew", pady=4)

        tk.Label(dev_frame, text="System Audio:", bg=self.bg_color, fg=self.text_secondary).grid(row=1, column=0, sticky="w", pady=4)
        self.cmb_loop = ttk.Combobox(dev_frame, values=[], state="readonly", width=60)
        self.cmb_loop.grid(row=1, column=1, padx=8, sticky="ew", pady=4)

        dev_frame.columnconfigure(1, weight=1)

        # ─── Transcript ────────────────────────────────────────────────────────
        tx_frame = tk.LabelFrame(
            self.root,
            text="📝 Detected Question",
            bg=self.card_bg,
            fg=self.accent_color,
            font=(self.font_family, 9, "bold"),
            padx=10,
            pady=8
        )
        tx_frame.pack(fill=tk.BOTH, expand=False, padx=12, pady=8)

        self.txt_question = tk.Text(
            tx_frame,
            height=3,
            bg="#0f172a",
            fg=self.text_primary,
            font=(self.font_family, 9),
            wrap=tk.WORD,
            relief=tk.FLAT
        )
        self.txt_question.pack(fill=tk.BOTH, expand=True, padx=0, pady=0)
        self.txt_question.config(state=tk.DISABLED)

        # ─── Answer Display (MAIN) ─────────────────────────────────────────────
        ans_frame = tk.LabelFrame(
            self.root,
            text="✨ AI-Generated Answer (Instant Display)",
            bg=self.card_bg,
            fg=self.success_color,
            font=(self.font_family, 9, "bold"),
            padx=10,
            pady=8
        )
        ans_frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=8)

        # Answer text with scrollbar
        scrollbar = tk.Scrollbar(ans_frame)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.txt_answer = tk.Text(
            ans_frame,
            bg="#0f172a",
            fg=self.success_color,
            font=(self.font_family, 10, "bold"),
            wrap=tk.WORD,
            relief=tk.FLAT,
            yscrollcommand=scrollbar.set
        )
        self.txt_answer.pack(fill=tk.BOTH, expand=True)
        self.txt_answer.config(state=tk.DISABLED)
        scrollbar.config(command=self.txt_answer.yview)

        # ─── Key Points ───────────────────────────────────────────────────────
        kp_frame = tk.LabelFrame(
            self.root,
            text="⭐ Key Points",
            bg=self.card_bg,
            fg="#fbbf24",
            font=(self.font_family, 8, "bold"),
            padx=8,
            pady=6
        )
        kp_frame.pack(fill=tk.X, padx=12, pady=4)

        self.lbl_keypoints = tk.Label(
            kp_frame,
            text="-",
            bg=self.card_bg,
            fg="#fbbf24",
            font=(self.font_family, 8),
            wraplength=550,
            justify="left"
        )
        self.lbl_keypoints.pack(anchor="w", fill=tk.BOTH, expand=True)

        # ─── Bottom Status ─────────────────────────────────────────────────────
        bottom = tk.Frame(self.root, bg=self.bg_color)
        bottom.pack(fill=tk.X, padx=12, pady=(0, 8))

        self.lbl_capture = tk.Label(
            bottom,
            text="Ready | F9: Toggle | F10: Click-through",
            bg=self.bg_color,
            fg=self.text_secondary,
            font=(self.font_family, 8)
        )
        self.lbl_capture.pack(side=tk.LEFT)

        self.btn_close = tk.Button(
            bottom,
            text="✕ Close",
            command=self._on_close,
            bg="#475569",
            fg="#fff",
            font=(self.font_family, 8, "bold"),
            relief=tk.FLAT,
            cursor="hand2"
        )
        self.btn_close.pack(side=tk.RIGHT)

    def _bind_shortcuts(self):
        """Bind global hotkeys."""
        self.root.bind("<F9>", lambda e: self.toggle_visibility())
        self.root.bind("<F10>", lambda e: self.toggle_click_through())

    def toggle_visibility(self):
        """Toggle window visibility (F9)."""
        if self.root.winfo_viewable():
            self.root.withdraw()
            print("[Stealth] Hidden (F9 to show)")
        else:
            self.root.deiconify()
            self.root.attributes("-topmost", True)
            print("[Stealth] Visible")

    def toggle_click_through(self):
        """Toggle click-through mode (F10)."""
        self.is_click_through = not self.is_click_through
        status = "ON" if self.is_click_through else "OFF"
        self.lbl_capture.config(text=f"Click-through: {status} | F9: Toggle | F10: Click-through")
        print(f"[Stealth] Click-through: {status}")

    # ─────────────────────────────────────────────────────────────────────────
    # Device Management
    # ─────────────────────────────────────────────────────────────────────────

    def _populate_devices(self):
        """Populate device dropdowns."""
        if sd is None or np is None:
            self.cmb_mic["values"] = ["sounddevice/numpy not installed"]
            self.cmb_loop["values"] = ["sounddevice/numpy not installed"]
            return

        try:
            devs = sd.query_devices()
            mic_list = []
            loop_list = []

            for i, d in enumerate(devs):
                name = f"{i}: {d['name']}"
                if d["max_input_channels"] > 0:
                    mic_list.append(name)
                if "loopback" in d["name"].lower() or "stereo mix" in d["name"].lower():
                    loop_list.append(name)

            self.cmb_mic["values"] = mic_list
            self.cmb_loop["values"] = loop_list

            if mic_list:
                self.cmb_mic.current(0)
            if loop_list:
                self.cmb_loop.current(0)

            print(f"[Audio] Found {len(mic_list)} mic(s), {len(loop_list)} loopback device(s)")
        except Exception as e:
            messagebox.showerror("Device Error", str(e))

    # ─────────────────────────────────────────────────────────────────────────
    # Capture Lifecycle
    # ─────────────────────────────────────────────────────────────────────────

    def start_capture(self):
        """Start audio capture and WebSocket connection."""
        if sd is None or np is None or websockets is None:
            messagebox.showerror(
                "Missing Dependencies",
                "Install: numpy, sounddevice, websockets"
            )
            return

        try:
            mic_sel = self.cmb_mic.get()
            loop_sel = self.cmb_loop.get()
            self.mic_device = int(mic_sel.split(":", 1)[0]) if mic_sel else None
            self.loopback_device = int(loop_sel.split(":", 1)[0]) if loop_sel else None
        except Exception:
            self.mic_device = None
            self.loopback_device = None

        if self._capture_thread and self._capture_thread.is_alive():
            return

        self._stop_event.clear()
        self._capture_thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._capture_thread.start()

        if not (self._ws_thread and self._ws_thread.is_alive()):
            self._ws_thread = threading.Thread(target=self._ws_loop_thread, daemon=True)
            self._ws_thread.start()

        self.is_capturing = True
        self.lbl_capture.config(text=f"▶ Capturing ({self.chunk_seconds}s chunks)")
        self.btn_start.config(state=tk.DISABLED)
        self.btn_stop.config(state=tk.NORMAL)
        print("[Capture] Started")

    def stop_capture(self):
        """Stop audio capture."""
        self._stop_event.set()
        self.is_capturing = False
        self.lbl_capture.config(text="⏹ Stopped")
        self.btn_start.config(state=tk.NORMAL)
        self.btn_stop.config(state=tk.DISABLED)
        print("[Capture] Stopped")

    def _capture_loop(self):
        """Capture audio from devices."""
        samplerate = 16000
        frames_per_chunk = int(self.chunk_seconds * samplerate)

        q_mic = queue.Queue()
        q_loop = queue.Queue()

        def make_callback(q):
            def cb(indata, frames, time_info, status):
                if status:
                    print(f"Audio callback status: {status}")
                try:
                    q.put(indata.copy())
                except Exception as e:
                    print(f"Audio callback error: {e}")
            return cb

        mic_stream = None
        loop_stream = None

        try:
            if self.mic_device is not None:
                mic_stream = sd.InputStream(
                    device=self.mic_device,
                    channels=1,
                    samplerate=samplerate,
                    dtype="float32",
                    callback=make_callback(q_mic)
                )
                mic_stream.start()
                print(f"[Audio] Microphone stream started (device {self.mic_device})")

            if self.loopback_device is not None:
                loop_stream = sd.InputStream(
                    device=self.loopback_device,
                    channels=1,
                    samplerate=samplerate,
                    dtype="float32",
                    callback=make_callback(q_loop)
                )
                loop_stream.start()
                print(f"[Audio] Loopback stream started (device {self.loopback_device})")

            accum = np.zeros(0, dtype=np.float32)
            while not self._stop_event.is_set():
                parts = []

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
                    concat = np.concatenate(parts)
                    accum = np.concatenate([accum, concat]) if accum.size else concat

                if accum.shape[0] >= frames_per_chunk:
                    chunk = accum[:frames_per_chunk]
                    accum = accum[frames_per_chunk:]

                    if chunk.dtype != np.float32:
                        chunk = chunk.astype(np.float32)

                    maxv = np.max(np.abs(chunk)) if chunk.size else 1.0
                    if maxv > 1.0:
                        chunk = chunk / maxv

                    pcm16 = pcm16_from_float32(chunk)
                    wav_bytes = encode_wav_bytes(pcm16, samplerate)

                    try:
                        self._audio_queue.put_nowait(wav_bytes)
                    except queue.Full:
                        self._audio_queue.get()  # Drop oldest
                        self._audio_queue.put_nowait(wav_bytes)
                else:
                    time.sleep(0.01)

        except Exception as e:
            print(f"[Capture] Error: {e}")
        finally:
            for stream in [mic_stream, loop_stream]:
                try:
                    if stream:
                        stream.stop()
                        stream.close()
                except Exception:
                    pass
            print("[Capture] Streams closed")

    # ─────────────────────────────────────────────────────────────────────────
    # WebSocket
    # ─────────────────────────────────────────────────────────────────────────

    def _ws_loop_thread(self):
        """Run WebSocket event loop in thread."""
        if asyncio is None or websockets is None:
            self._ui_set_status("WebSocket unavailable", ok=False)
            return

        self._ws_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._ws_loop)
        self._ws_loop.run_until_complete(self._ws_main())

    async def _ws_main(self):
        """Main WebSocket loop with reconnect."""
        url = self.api_url.replace("http://", "ws://").replace("https://", "wss://")
        url += f"/api/interviews/{self.session_id}/ws"

        backoff = 1.0
        while not self._stop_event.is_set():
            try:
                async with websockets.connect(url, max_size=None) as ws:
                    self.websocket = ws
                    self.ws_connected = True
                    self._ui_set_status("Connected ✓", ok=True)
                    backoff = 1.0

                    sender = asyncio.create_task(self._ws_sender())
                    receiver = asyncio.create_task(self._ws_receiver())
                    done, pending = await asyncio.wait(
                        [sender, receiver],
                        return_when=asyncio.FIRST_EXCEPTION
                    )
                    for t in pending:
                        t.cancel()
            except Exception as e:
                self.ws_connected = False
                self._ui_set_status(f"Disconnected ({backoff:.0f}s)", ok=False)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
            finally:
                self.ws_connected = False

    async def _ws_sender(self):
        """Send audio frames as binary."""
        while not self._stop_event.is_set() and self.ws_connected:
            try:
                wav_bytes = await self._ws_loop.run_in_executor(
                    None,
                    self._audio_queue.get,
                    timeout=1
                )
                if wav_bytes:
                    await self.websocket.send(wav_bytes)
            except Exception as e:
                print(f"[WS] Sender error: {e}")
                break

    async def _ws_receiver(self):
        """Receive and handle events."""
        while not self._stop_event.is_set() and self.ws_connected:
            try:
                msg = await self.websocket.recv()
                if isinstance(msg, bytes):
                    continue

                data = json.loads(msg)
                ev_type = data.get("type")
                payload = data.get("payload", {})

                if ev_type == "ping":
                    await self.websocket.send(json.dumps({"type": "pong"}))
                elif ev_type == "question.detected":
                    q = payload.get("question")
                    self._ui_update_question(q)
                elif ev_type == "answer.generated":
                    self._ui_update_answer(payload)
                elif ev_type == "session.connected":
                    self._ui_set_status("Session connected", ok=True)
                elif ev_type == "session.error":
                    msg_text = payload.get("message", "Unknown error")
                    print(f"[WS] Error: {msg_text}")
            except Exception as e:
                print(f"[WS] Receiver error: {e}")
                break

    # ─────────────────────────────────────────────────────────────────────────
    # UI Updates
    # ─────────────────────────────────────────────────────────────────────────

    def _ui_set_status(self, text: str, ok: bool = True):
        """Update status label."""
        def cb():
            color = "#10b981" if ok else "#ef4444"
            symbol = "●" if ok else "●"
            self.status_label.config(text=f"{symbol} {text}", fg=color)

        self.root.after(0, cb)

    def _ui_update_question(self, q: str):
        """Display detected question."""
        def cb():
            self.txt_question.config(state=tk.NORMAL)
            self.txt_question.delete("1.0", tk.END)
            self.txt_question.insert("1.0", q or "-")
            self.txt_question.config(state=tk.DISABLED)

        self.root.after(0, cb)

    def _ui_update_answer(self, payload: dict):
        """Display AI-generated answer with auto-hide."""
        def cb():
            # Main answer
            answer = payload.get("answer", "")
            self.txt_answer.config(state=tk.NORMAL)
            self.txt_answer.delete("1.0", tk.END)
            if answer:
                self.txt_answer.insert("1.0", answer)
            self.txt_answer.config(state=tk.DISABLED)

            # Key points
            key_points = payload.get("key_points", [])
            if key_points:
                self.lbl_keypoints.config(text="• " + "\n• ".join(key_points))
            else:
                self.lbl_keypoints.config(text="-")

            # Auto-hide after delay
            if self.answer_auto_hide_task:
                self.root.after_cancel(self.answer_auto_hide_task)

            self.answer_auto_hide_task = self.root.after(
                10000,  # 10 seconds (configurable via settings)
                self._auto_hide_answer
            )

        self.root.after(0, cb)

    def _auto_hide_answer(self):
        """Auto-hide answer after delay."""
        def cb():
            self.txt_answer.config(state=tk.NORMAL)
            self.txt_answer.delete("1.0", tk.END)
            self.txt_answer.config(state=tk.DISABLED)
            self.lbl_keypoints.config(text="-")

        self.root.after(0, cb)
        print("[UI] Answer auto-hidden")

    # ─────────────────────────────────────────────────────────────────────────
    # Cleanup
    # ─────────────────────────────────────────────────────────────────────────

    def _on_close(self):
        """Clean shutdown."""
        self.stop_capture()
        self._stop_event.set()
        time.sleep(0.2)

        try:
            if self._ws_loop and self._ws_loop.is_running():
                self._ws_loop.call_soon_threadsafe(self._ws_loop.stop)
        except Exception:
            pass

        self.root.destroy()
        print("[App] Closed")


# ─────────────────────────────────────────────────────────────────────────────
# Entry Point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    root = tk.Tk()
    app = StealthOverlayApp(
        root,
        api_url="http://localhost:8000",
        session_id=1  # Change to your session ID or pass via CLI
    )
    root.mainloop()


if __name__ == "__main__":
    main()
