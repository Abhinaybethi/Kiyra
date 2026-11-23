"""CPU contention probe: standalone Ollama vs the live backend path.

Observation:
  * Standalone request, ~1330-token prompt: prefill ~1000 ms
  * Same prompt through the live backend:      prefill ~2200 ms

Ollama's prefill is GPU work, so CPU contention should not matter — unless
the backend process is starving Ollama's runner of CPU (the sampler, the
HTTP handling and the tokenizer all run on CPU between GPU kernels), or the
GPU is being time-sliced with another process.

This probe samples per-process CPU time while (a) a standalone request runs
and (b) a live turn runs, so the difference can be attributed.

Run:  cd backend && uv run python tests\\integration\\bench_cpu_contention.py
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
import time

import requests
import websockets

from _live_auth import http_headers, ws_connect_headers

OLLAMA = "http://localhost:11434"
BACKEND = "http://localhost:8000"
MODEL = "qwen3.5:4b"

SYSTEM = ("You are Kiyra, a real-time interview copilot. " * 40)
USER = ("Candidate context:\n" + ("The quick brown fox jumps over the lazy dog. " * 300)
        + "\n\nCURRENT QUESTION: Explain the architecture.")


def _cpu_seconds() -> dict:
    """CPU seconds per process name (cumulative)."""
    out: dict = {}
    try:
        res = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-Process python,ollama -ErrorAction SilentlyContinue | "
             "ForEach-Object { \"$($_.ProcessName):$($_.Id):$($_.CPU)\" }"],
            capture_output=True, text=True, timeout=10)
        for line in res.stdout.splitlines():
            parts = line.strip().split(":")
            if len(parts) == 3:
                try:
                    out[f"{parts[0]}:{parts[1]}"] = float(parts[2])
                except ValueError:
                    pass
    except Exception:
        pass
    return out


class Sampler(threading.Thread):
    """Samples per-process CPU seconds at 100 ms and reports the deltas."""

    def __init__(self):
        super().__init__(daemon=True)
        self.start_cpu: dict = {}
        self.end_cpu: dict = {}
        self._stop = threading.Event()

    def run(self):
        self.start_cpu = _cpu_seconds()
        while not self._stop.is_set():
            self._stop.wait(0.1)
        self.end_cpu = _cpu_seconds()

    def stop(self):
        self._stop.set()
        self.join(timeout=2)

    def report(self) -> str:
        rows = []
        for key, end in self.end_cpu.items():
            start = self.start_cpu.get(key, end)
            rows.append((key, end - start))
        rows.sort(key=lambda kv: -kv[1])
        return ", ".join(f"{k}={v:.2f}s" for k, v in rows[:6])


def standalone(options: dict) -> dict:
    payload = {
        "model": MODEL,
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": USER}],
        "stream": True, "think": False, "keep_alive": "30m",
        "options": options,
    }
    t0 = time.monotonic()
    first = None
    final = {}
    with requests.post(f"{OLLAMA}/api/chat", json=payload, timeout=300,
                       stream=True) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            if not line:
                continue
            chunk = json.loads(line)
            if (chunk.get("message", {}) or {}).get("content") and first is None:
                first = (time.monotonic() - t0) * 1000
            if chunk.get("done"):
                final = chunk
                break
    return {
        "first_ms": round(first or 0),
        "prefill_ms": round((final.get("prompt_eval_duration") or 0) / 1e6),
        "ptok": final.get("prompt_eval_count"),
    }


async def live_turn() -> dict:
    r = requests.post(f"{BACKEND}/api/interviews", json={
        "mode": "live", "interview_type": "mixed", "title": "CPU probe",
    }, headers=http_headers(), timeout=30)
    sid = r.json()["id"]
    url = f"ws://localhost:8000/api/interviews/{sid}/ws"
    events: list = []

    async with websockets.connect(
            url, max_size=None, additional_headers=ws_connect_headers()) as ws:
        async def reader():
            try:
                async for raw in ws:
                    events.append((time.monotonic(), json.loads(raw)))
            except Exception:
                pass

        task = asyncio.create_task(reader())
        await ws.send(json.dumps({"type": "mode.set",
                                  "payload": {"mode": "interview_listening"}}))
        await asyncio.sleep(0.4)
        t0 = time.monotonic()
        await ws.send(json.dumps({"type": "turn.send",
                                  "payload": {"text": "Explain the architecture."}}))
        first = None
        generated = None
        deadline = t0 + 120
        while time.monotonic() < deadline:
            for ts, ev in events:
                if ev["type"] == "answer.streamed" and first is None:
                    first = ts
                elif ev["type"] == "answer.generated":
                    generated = ts
            if generated:
                break
            await asyncio.sleep(0.02)
        task.cancel()
    perf = {}
    for _ts, ev in events:
        if ev["type"] == "kiyra.perf":
            perf = ev.get("payload", {})
    return {
        "first_ms": round((first - t0) * 1000) if first else -1,
        "total_ms": round((generated - t0) * 1000) if generated else -1,
        "prefill_ms": perf.get("prefill_ms", -1),
        "ptok": perf.get("prompt_tokens", -1),
    }


def main() -> int:
    opts = {"temperature": 0.3, "num_predict": 270, "num_ctx": 4096}
    print("### standalone (no backend activity)")
    for i in range(3):
        s = Sampler()
        s.start()
        time.sleep(0.2)
        r = standalone(opts)
        s.stop()
        print(f"  run{i}: first={r['first_ms']}ms prefill={r['prefill_ms']}ms "
              f"ptok={r['ptok']}  cpu[{s.report()}]")

    print("\n### live backend turn")
    for i in range(3):
        s = Sampler()
        s.start()
        time.sleep(0.2)
        r = asyncio.run(live_turn())
        s.stop()
        print(f"  run{i}: first={r['first_ms']}ms prefill={r['prefill_ms']}ms "
              f"ptok={r['ptok']}  cpu[{s.report()}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
