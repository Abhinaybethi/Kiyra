"""LIVE latency probe for the text -> answer pipeline (no microphone).

Drives the real WebSocket with `turn.send` (the exact SEND the overlay sends)
and measures, on the client side, every visible stage:

    send -> turn.submitted -> answer.generating -> first streamed chunk
         -> answer.generated -> kiyra.perf

The server-side `[KIYRA-PERF]` record arrives as a `kiyra.perf` event while
listening mode is on, so the whole breakdown is available even when stdout is
not being captured to a file.

This file deliberately does not start with ``test_`` so pytest never collects
it (it needs a live backend + a real model).

Run:  cd backend && uv run python tests\\integration\\live_latency.py
"""
from __future__ import annotations

import asyncio
import json
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
        "mode": "live",
        "interview_type": "mixed",
        "title": "Latency probe",
    }, timeout=30)
    r.raise_for_status()
    return r.json()["id"]


async def collect(ws, sink):
    try:
        async for raw in ws:
            sink.append((time.monotonic(), json.loads(raw)))
    except asyncio.CancelledError:
        raise
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
    words = len(answer.split())
    total_ms = (generated[0] - t0) * 1000 if generated else -1
    ttft_ms = (first_chunk - t0) * 1000 if first_chunk else -1
    gen_s = ((generated[0] - first_chunk) * 1000) if (generated and first_chunk) else -1
    wps = (words / (gen_s / 1000.0)) if gen_s > 0 else -1

    print(f"\n[{label}] {question}")
    print(f"  client  ttft={ttft_ms:.0f}ms total={total_ms:.0f}ms "
          f"words={words} stream_s={gen_s / 1000.0:.2f} wps={wps:.1f}")
    if perf:
        print("  server  " + " ".join(
            f"{k}={perf.get(k)}" for k in (
                "vad_ms", "stt_ms", "detection_ms", "retrieval_ms",
                "context_ms", "prompt_ms", "llm_ttft_ms", "llm_total_ms",
                "first_token_ms", "total_ms", "chunks", "prompt_tokens",
                "completion_tokens")))
    else:
        print("  server  <no kiyra.perf event>")
    return {
        "label": label, "ttft_ms": ttft_ms, "total_ms": total_ms,
        "words": words, "perf": perf or {},
    }


async def main():
    session_id = create_session()
    print(f"session {session_id}")
    url = f"ws://localhost:8000/api/interviews/{session_id}/ws"
    rows = []
    async with websockets.connect(url, max_size=None) as ws:
        sink: list = []
        task = asyncio.create_task(collect(ws, sink))
        await ws.send(json.dumps({"type": "mode.set",
                                  "payload": {"mode": "interview_listening"}}))
        await asyncio.sleep(0.4)
        try:
            for label, q in QUESTIONS:
                rows.append(await one_turn(ws, sink, label, q))
                await asyncio.sleep(0.5)
        finally:
            task.cancel()

    print("\n" + "=" * 78)
    print(f"{'type':<12}{'ttft_ms':>10}{'total_ms':>10}{'words':>8}"
          f"{'llm_ttft':>10}{'llm_total':>11}{'rag':>8}")
    for r in rows:
        p = r["perf"]
        print(f"{r['label']:<12}{r['ttft_ms']:>10.0f}{r['total_ms']:>10.0f}"
              f"{r['words']:>8}{p.get('llm_ttft_ms', -1):>10}"
              f"{p.get('llm_total_ms', -1):>11}{p.get('retrieval_ms', -1):>8}")
    ok = [r for r in rows if r["total_ms"] > 0]
    if ok:
        print(f"\navg ttft={sum(r['ttft_ms'] for r in ok) / len(ok):.0f}ms "
              f"avg total={sum(r['total_ms'] for r in ok) / len(ok):.0f}ms "
              f"avg words={sum(r['words'] for r in ok) / len(ok):.0f}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
