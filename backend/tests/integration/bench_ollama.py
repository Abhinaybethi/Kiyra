"""Benchmark Ollama throughput for the Kiyra answer shape.

Not a pytest test (no ``test_`` prefix). Run:

    cd backend && uv run python tests/integration/bench_ollama.py
"""
from __future__ import annotations

import json
import time
import urllib.request

URL = "http://localhost:11434/api/chat"
MODEL = "qwen3.5:4b"

FILLER = (
    "The candidate built a real-time interview assistant with FastAPI, "
    "WebSockets, Whisper speech recognition, and a retrieval layer over "
    "uploaded documents. "
)

PROMPT = (
    "You are an interview copilot. Answer in 150-220 words.\n\n"
    "RELEVANT KNOWLEDGE:\n" + FILLER * 40 + "\n\n"
    "CURRENT QUESTION: Explain the architecture of the Kiyra project.\n"
)


def run(options: dict, label: str) -> None:
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": "You are Kiyra, an interview copilot."},
            {"role": "user", "content": PROMPT},
        ],
        "stream": False,
        "think": False,
        "keep_alive": "30m",
        "options": {"temperature": 0.5, "num_predict": 220, **options},
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(URL, data=data,
                                 headers={"Content-Type": "application/json"})
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=300) as r:
        body = json.loads(r.read())
    wall = (time.monotonic() - t0) * 1000
    pe = body.get("prompt_eval_count", 0)
    pc = body.get("eval_count", 0)
    pd = body.get("prompt_eval_duration", 1) / 1e9
    ed = body.get("eval_duration", 1) / 1e9
    ld = body.get("load_duration", 0) / 1e9
    print(f"{label:<18} wall={wall:7.0f}ms load={ld:5.2f}s "
          f"prompt={pe:4d}tok ({pe / pd:6.0f} tok/s) "
          f"gen={pc:4d}tok ({pc / ed:5.1f} tok/s) "
          f"content={len(body.get('message', {}).get('content', ''))}ch")


if __name__ == "__main__":
    run({}, "default ctx")
    run({"num_ctx": 4096}, "num_ctx=4096")
    run({"num_ctx": 2048}, "num_ctx=2048")
    run({"num_ctx": 4096, "num_predict": 160}, "ctx4096/pred160")
