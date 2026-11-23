"""Check whether answer token caps are truncating answers mid-sentence.

Drives the real WebSocket once per question type and reports, for each:
  - completion_tokens vs the configured num_predict cap
  - word count
  - whether the text ends in terminal punctuation (truncation signal)

Not a pytest test (no ``test_`` prefix). Run:
    cd backend && uv run python tests/integration/live_truncation.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import time

import requests
import websockets

from _live_auth import http_headers, ws_connect_headers

BACKEND = "http://localhost:8000"

QUESTIONS = [
    ("general", "Tell me about yourself."),
    ("project", "Explain my Kiyra project and the architecture you used."),
    ("rag", "What was the hardest technical problem I solved in Kiyra?"),
    ("coding", "Write a Python function that reverses a linked list."),
    ("behavioural", "Tell me about a time you disagreed with a teammate."),
]

TERMINAL = ".!?\"')]}"


def create_session() -> int:
    r = requests.post(f"{BACKEND}/api/interviews", json={
        "mode": "live",
        "interview_type": "mixed",
        "title": "Truncation probe",
    }, headers=http_headers(), timeout=30)
    r.raise_for_status()
    return r.json()["id"]


async def collect(ws, sink):
    try:
        async for raw in ws:
            sink.append(json.loads(raw))
    except Exception:
        pass


async def one_turn(ws, sink, label, question):
    sink.clear()
    await ws.send(json.dumps({"type": "turn.send",
                              "payload": {"text": question}}))
    deadline = time.monotonic() + 120
    answer = ""
    perf = {}
    while time.monotonic() < deadline:
        for ev in sink:
            if ev["type"] == "answer.generated":
                answer = ev.get("payload", {}).get("answer", "") or ""
            elif ev["type"] == "kiyra.perf":
                perf = ev.get("payload", {})
        if answer and perf:
            break
        await asyncio.sleep(0.02)

    words = len(answer.split())
    ct = perf.get("completion_tokens", -1)
    ends_ok = answer.rstrip().endswith(tuple(TERMINAL))
    print(f"\n[{label}] {question}")
    print(f"  words={words} completion_tokens={ct} "
          f"ends_cleanly={ends_ok}")
    print(f"  tail: ...{answer[-140:]!r}")
    return {"label": label, "words": words, "ct": ct, "clean": ends_ok}


async def main():
    sid = create_session()
    url = f"ws://localhost:8000/api/interviews/{sid}/ws"
    rows = []
    async with websockets.connect(
            url, max_size=None, additional_headers=ws_connect_headers()) as ws:
        sink: list = []
        task = asyncio.create_task(collect(ws, sink))
        await ws.send(json.dumps({"type": "mode.set",
                                  "payload": {"mode": "interview_listening"}}))
        await asyncio.sleep(0.4)
        try:
            for label, q in QUESTIONS:
                rows.append(await one_turn(ws, sink, label, q))
                await asyncio.sleep(0.4)
        finally:
            task.cancel()

    print("\n" + "=" * 70)
    print(f"{'type':<13}{'words':>7}{'comp_tok':>10}{'clean_end':>12}")
    for r in rows:
        print(f"{r['label']:<13}{r['words']:>7}{r['ct']:>10}{str(r['clean']):>12}")
    bad = [r for r in rows if not r["clean"]]
    print(f"\ntruncated answers: {len(bad)}/{len(rows)}"
          + (f" -> {[r['label'] for r in bad]}" if bad else ""))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
