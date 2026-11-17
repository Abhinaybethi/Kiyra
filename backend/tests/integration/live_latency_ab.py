"""A/B latency harness — measures the live path N times and prints medians.

Used to compare Ollama settings (prefill batch size, prompt trimming) with
the run-to-run noise of the machine averaged out: the median of every turn's
time-to-first-token is far more trustworthy than a single run.

The backend must already be running with the settings under test.

Run:  cd backend && uv run python tests\\integration\\live_latency_ab.py [rounds]
"""
from __future__ import annotations

import asyncio
import json
import statistics
import sys
import time

import requests
import websockets

BACKEND = "http://localhost:8000"

QUESTIONS = [
    ("general", "Tell me about yourself."),
    ("project", "Explain my Kiyra project and the architecture you used."),
    ("rag", "What was the hardest technical problem I solved in Kiyra?"),
    ("coding", "Write a Python function that reverses a linked list."),
    ("behavioural", "Tell me about a time you disagreed with a teammate."),
]


def create_session() -> int:
    r = requests.post(f"{BACKEND}/api/interviews", json={
        "mode": "live", "interview_type": "mixed", "title": "Latency A/B",
    }, timeout=30)
    r.raise_for_status()
    return r.json()["id"]


async def collect(ws, sink):
    try:
        async for raw in ws:
            sink.append((time.monotonic(), json.loads(raw)))
    except Exception:
        pass


async def one_turn(ws, sink, label, question):
    sink.clear()
    t0 = time.monotonic()
    await ws.send(json.dumps({"type": "turn.send",
                              "payload": {"text": question}}))
    first_chunk = None
    generated = None
    perf = None
    deadline = t0 + 120
    while time.monotonic() < deadline:
        for ts, ev in sink:
            if ev["type"] == "answer.streamed" and first_chunk is None:
                first_chunk = ts
            elif ev["type"] == "answer.generated":
                generated = (ts, ev.get("payload", {}))
            elif ev["type"] == "kiyra.perf":
                perf = ev.get("payload", {})
        if generated is not None and perf is not None:
            break
        await asyncio.sleep(0.02)
    answer = (generated[1].get("answer") if generated else "") or ""
    return {
        "label": label,
        "ttft_ms": (first_chunk - t0) * 1000 if first_chunk else -1,
        "total_ms": (generated[0] - t0) * 1000 if generated else -1,
        "words": len(answer.split()),
        "perf": perf or {},
    }


async def main() -> int:
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    session_id = create_session()
    url = f"ws://localhost:8000/api/interviews/{session_id}/ws"
    rows: list[dict] = []
    async with websockets.connect(url, max_size=None) as ws:
        sink: list = []
        task = asyncio.create_task(collect(ws, sink))
        await ws.send(json.dumps({"type": "mode.set",
                                  "payload": {"mode": "interview_listening"}}))
        await asyncio.sleep(0.4)
        try:
            for r in range(rounds):
                for label, q in QUESTIONS:
                    row = await one_turn(ws, sink, label, q)
                    row["round"] = r
                    rows.append(row)
                    p = row["perf"]
                    print(f"  r{row['round']} {row['label']:<12} "
                          f"ttft={row['ttft_ms']:>7.0f} "
                          f"total={row['total_ms']:>8.0f} "
                          f"llm_ttft={p.get('llm_ttft_ms', -1):>6} "
                          f"prefill={p.get('prefill_ms', -1):>6} "
                          f"load={p.get('load_ms', -1):>5} "
                          f"gen={p.get('gen_ms', -1):>6} "
                          f"ptok={p.get('prompt_tokens', -1):>5} "
                          f"words={row['words']:>4}")
                    await asyncio.sleep(0.4)
        finally:
            task.cancel()

    good = [r for r in rows if r["ttft_ms"] > 0]
    if not good:
        print("no successful turns")
        return 1
    ttft = [r["ttft_ms"] for r in good]
    total = [r["total_ms"] for r in good]
    prefill = [r["perf"].get("prefill_ms", -1) for r in good
               if r["perf"].get("prefill_ms", -1) > 0]
    print(f"\nSUMMARY n={len(good)}  "
          f"ttft median={statistics.median(ttft):.0f}ms "
          f"mean={statistics.mean(ttft):.0f}ms min={min(ttft):.0f}ms | "
          f"total median={statistics.median(total):.0f}ms "
          f"mean={statistics.mean(total):.0f}ms | "
          f"prefill median={statistics.median(prefill) if prefill else -1}ms")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
