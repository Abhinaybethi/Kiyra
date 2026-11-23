"""GPU clock / power sampler during a live turn.

The live path's wall time is dominated by generation (~35 tok/s). If the GPU
is downclocked (Windows "Balanced" plan on a laptop), the generation rate —
and therefore how fast the candidate can read the answer — is capped below
what the hardware can do.

This samples the GPU clock, power draw and utilisation while a real live
turn runs, so the effective generation speed can be attributed to the GPU
state rather than guessed at.

Run:  cd backend && uv run python tests\\integration\\bench_gpu_clock.py
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

BACKEND = "http://localhost:8000"


def _gpu() -> str:
    try:
        out = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=clocks.sm,power.draw,temperature.gpu,utilization.gpu,"
             "memory.used,clocks_throttle_reasons.active",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5)
        return out.stdout.strip()
    except Exception:
        return ""


class GpuSampler(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.samples: list[str] = []
        self._stop = threading.Event()

    def run(self):
        while not self._stop.is_set():
            s = _gpu()
            if s:
                self.samples.append(s)
            self._stop.wait(0.5)

    def stop(self):
        self._stop.set()
        self.join(timeout=2)

    def report(self):
        print(f"  gpu samples ({len(self.samples)}):")
        for s in self.samples[:20]:
            print(f"    {s}")


async def live_turn(question: str) -> dict:
    r = requests.post(f"{BACKEND}/api/interviews", json={
        "mode": "live", "interview_type": "mixed", "title": "GPU clock",
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
                                  "payload": {"text": question}}))
        first = None
        generated = None
        deadline = t0 + 180
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
    gen_ms = perf.get("gen_ms", -1) or -1
    ctok = perf.get("completion_tokens", -1) or -1
    return {
        "ttft_ms": round((first - t0) * 1000) if first else -1,
        "total_ms": round((generated - t0) * 1000) if generated else -1,
        "gen_ms": gen_ms,
        "ctok": ctok,
        "tok_per_s": round(ctok / (gen_ms / 1000), 1) if gen_ms > 0 and ctok > 0 else -1,
    }


def main() -> int:
    print("### idle gpu")
    print("  ", _gpu())
    for q in ("Tell me about yourself.",
              "Explain how the Node.js event loop works in detail."):
        print(f"\n### live turn: {q}")
        s = GpuSampler()
        s.start()
        time.sleep(0.3)
        r = asyncio.run(live_turn(q))
        s.stop()
        print(f"  ttft={r['ttft_ms']}ms total={r['total_ms']}ms "
              f"gen={r['gen_ms']}ms ctok={r['ctok']} -> {r['tok_per_s']} tok/s")
        s.report()
    return 0


if __name__ == "__main__":
    sys.exit(main())
