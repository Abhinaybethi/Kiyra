"""Real Windows hardware verification for Kiyra's WASAPI loopback-only capture.

MANUAL HARNESS — not a pytest test (filename deliberately lacks test_ prefix).

Run with the *desktop* interpreter (it has pyaudiowpatch + numpy):

    python backend\\tests\\integration\\wasapi_loopback_check.py

What it proves on real hardware:
  * The chosen endpoint is the WASAPI loopback twin of the default render
    device (isLoopbackDevice=True, host API=WASAPI) — the patched PortAudio
    backend opens it with AUDCLNT_STREAMFLAGS_LOOPBACK in shared mode.
  * A tone played through the DEFAULT PLAYBACK DEVICE (laptop speakers, wired
    headphones or Bluetooth headphones — whichever Windows is using) appears
    in the captured digital PCM at the expected frequency.
  * With no playback, the pipeline receives only digital silence: no physical
    microphone, headset mic or acoustic path can contribute samples.
  * sounddevice.InputStream is replaced with a trap: any microphone open attempt
    fails loudly, so a silent pass also proves the mic path is absent.

Endpoint-change / disconnect steps (B, C, F) require physical user action; the
harness prints instructions and can be re-run after switching devices.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "desktop"))

import pyaudiowpatch as p

import audio_loopback as al


class RecorderSink:
    """Collect the 16 kHz mono float stream before WAV encoding."""

    def __init__(self):
        self.blocks = []

    def feed(self, mono):
        if mono is not None and mono.size:
            self.blocks.append(np.asarray(mono, dtype=np.float32).copy())

    def samples(self):
        return np.concatenate(self.blocks) if self.blocks else np.zeros(0, dtype=np.float32)


def _tone_bytes(rate: int, channels: int, seconds: float, freq: float = 440.0,
                amplitude: float = 0.8) -> bytes:
    t = np.arange(int(rate * seconds)) / float(rate)
    mono = (amplitude * 32767.0 * np.sin(2 * np.pi * freq * t)).astype(np.int16)
    if channels == 1:
        return mono.tobytes()
    return np.repeat(mono[:, None], channels, axis=1).astype(np.int16).tobytes()


def _dominant_freq(x: np.ndarray, rate: int) -> float:
    if x.size < 4096:
        return 0.0
    w = x[:min(x.size, rate * 4)] * np.hanning(min(x.size, rate * 4))
    spectrum = np.abs(np.fft.rfft(w))
    return float(np.fft.rfftfreq(w.size, 1.0 / rate)[int(np.argmax(spectrum))])


def _trap_microphone():
    mod = type(sys)("sounddevice")

    def _no_mic(*_a, **_kw):
        raise AssertionError("microphone InputStream was opened — architecture violation")

    mod.InputStream = _no_mic
    mod.query_devices = lambda: (_ for _ in ()).throw(
        AssertionError("microphone enumeration attempted"))
    sys.modules["sounddevice"] = mod
    print("[GUARD] sounddevice.InputStream trapped (any mic open will fail)")


def main() -> int:
    _trap_microphone()
    assert not hasattr(al, "MicrophoneCapture"), "mic capture still present"

    pa = p.PyAudio()
    try:
        default_out = pa.get_default_wasapi_device(d_out=True)
        info = al.find_wasapi_loopback_device(pa=pa)
        wasapi_index = next(
            h["index"] for h in pa.get_host_api_info_generator()
            if int(h["type"]) == al.WASAPI_HOST_API_TYPE)
        print("[ENDPOINT] default render = %r" % default_out["name"])
        print("[ENDPOINT] loopback      = %s" % info)
        print("[ENDPOINT] WASAPI host api index=%d type=%d" %
              (wasapi_index, al.WASAPI_HOST_API_TYPE))
        if not info:
            print("FAIL: no verified WASAPI loopback endpoint")
            return 2
        if not info.get("is_loopback") or info.get("host_api") != wasapi_index:
            print("FAIL: endpoint is not WASAPI loopback")
            return 2
    finally:
        pa.terminate()

    recorder = RecorderSink()
    cap = al.LoopbackCapture(device_info=info, sink=recorder)
    if not cap.start():
        print("FAIL: loopback stream did not start")
        return 2
    try:
        # Phase 1 — no playback: only the watchdog's digital silence.
        time.sleep(1.0)
        idle = recorder.samples()
        idle_peak = float(np.max(np.abs(idle))) if idle.size else 0.0
        print("[IDLE] samples=%d peak=%.6f source=silence-only" % (idle.size, idle_peak))

        # Phase 2 — play a 440 Hz tone through the default Windows output.
        pa = p.PyAudio()
        try:
            out = pa.open(
                format=p.paInt16, channels=2, rate=48000,
                output=True, output_device_index=int(default_out["index"]),
                frames_per_buffer=4800)
            out.write(_tone_bytes(48000, 2, 2.0))
            time.sleep(0.3)
            out.stop_stream()
            out.close()
        finally:
            pa.terminate()

        time.sleep(0.3)
        all_samples = recorder.samples()
    finally:
        cap.stop()

    played = all_samples[idle.size:] if all_samples.size > idle.size else all_samples
    rms = float(np.sqrt(np.mean(played.astype(np.float64) ** 2))) if played.size else 0.0
    peak = _dominant_freq(played, al.TARGET_SR)
    print("[CAPTURE] samples=%d rms=%.6f dominant_hz=%.1f" %
          (played.size, rms, peak))
    print("[STREAM] discontinuities=%d overflows=%d silence_s=%.2f idle_peak=%.6f" %
          (cap.discontinuities, cap.overflows, cap.silence_seconds, idle_peak))

    # Phase 3 — the REAL overlay orchestration (no Tk window needed): run
    # _capture_loop with a stub app, play another tone, inspect the WS queue.
    import queue as _queue
    import threading
    import types
    import wave

    import stealth_overlay as so

    ws_queue = _queue.Queue(maxsize=200)

    def _enqueue(wav):
        al.bounded_put_drop_oldest(ws_queue, wav)

    app = types.SimpleNamespace(
        _stop_event=threading.Event(),
        loopback_device=info,
        _last_loop=info,
        _loop_name=None,
        _loop_choice=None,
        _loopback_active=False,
        _audio_queue=ws_queue,
        _push_audio_chunk=_enqueue,
        _render_sys_audio_state=lambda: None,
        _sync_sys_audio_selector=lambda: None,
        _ui_set_status=lambda msg, ok=False: print("[APP-STATUS]", msg),
    )
    thread = threading.Thread(target=so.StealthOverlayApp._capture_loop,
                              args=(app,), daemon=True)
    thread.start()
    time.sleep(0.6)
    pa = p.PyAudio()
    try:
        out = pa.open(
            format=p.paInt16, channels=2, rate=48000,
            output=True, output_device_index=int(default_out["index"]),
            frames_per_buffer=4800)
        out.write(_tone_bytes(48000, 2, 1.2))
        time.sleep(0.2)
        out.stop_stream()
        out.close()
    finally:
        pa.terminate()
    time.sleep(0.2)
    app._stop_event.set()
    thread.join(timeout=5)

    frames = []
    while not ws_queue.empty():
        raw = ws_queue.get_nowait()
        with wave.open(__import__("io").BytesIO(raw), "rb") as wf:
            frames.append(np.frombuffer(wf.readframes(wf.getnframes()),
                                        dtype=np.int16).astype(np.float32) / 32768.0)
    overlay_samples = np.concatenate(frames) if frames else np.zeros(0, dtype=np.float32)
    overlay_rms = (float(np.sqrt(np.mean(overlay_samples ** 2)))
                   if overlay_samples.size else 0.0)
    overlay_freq = _dominant_freq(overlay_samples, al.TARGET_SR)
    print("[OVERLAY] ws_frames=%d rms=%.6f dominant_hz=%.1f active=%s" %
          (len(frames), overlay_rms, overlay_freq, app._loopback_active))

    ok = True
    if overlay_rms < 1e-4 or abs(overlay_freq - 440.0) > 20.0:
        print("FAIL: overlay WS stream did not carry the playback tone")
        ok = False
    if idle_peak > 1e-4:
        print("FAIL: signal captured while nothing was rendering (mic path?)")
        ok = False
    if played.size < al.TARGET_SR:
        print("FAIL: no loopback samples during playback")
        ok = False
    if rms < 1e-4:
        print("FAIL: loopback captured no energy from the render stream")
        ok = False
    if abs(peak - 440.0) > 20.0:
        print("FAIL: expected 440 Hz tone, got %.1f Hz" % peak)
        ok = False

    print("RESULT:", "PASS - WASAPI loopback-only capture verified" if ok else "FAIL")
    if ok:
        print("For wired/Bluetooth endpoints: make that device Windows' default "
              "playback device and re-run this harness (tests B and C).")
        print("For disconnection (F): unplug/disable the default playback device; "
              "the overlay must log '[AUDIO_LOOPBACK] disconnected' and never switch to mic.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
