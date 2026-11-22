"""Replay the EXACT prompt the live path sent, straight to Ollama.

The live path reported `prefill_ms=1845` for a 957-token prompt with
num_batch=2048, while a synthetic prompt of the same size prefilled in
156 ms. This replays the captured production prompt (written by the live
path when KIYRA_DUMP_PROMPT=1) so the difference can be pinned on the
prompt itself rather than on the pipeline.

Run:  cd backend && uv run python tests\\integration\\bench_replay_dump.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time

import requests

OLLAMA = "http://localhost:11434"
MODEL = "qwen3.5:4b"
DUMP = os.path.join(tempfile.gettempdir(), "kiyra_prompt_dump.json")


def run(messages, options, label):
    payload = {
        "model": MODEL,
        "messages": messages,
        "stream": True,
        "think": False,
        "keep_alive": "30m",
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
    print(f"  {label:<28} first={round(first or 0):>6}ms  "
          f"load={round((final.get('load_duration') or 0) / 1e6):>5} "
          f"prefill={round((final.get('prompt_eval_duration') or 0) / 1e6):>6} "
          f"gen={round((final.get('eval_duration') or 0) / 1e6):>6} "
          f"ptok={final.get('prompt_eval_count')} "
          f"ctok={final.get('eval_count')}")


def main() -> int:
    if not os.path.exists(DUMP):
        print(f"No prompt dump at {DUMP} — run the live path with "
              f"KIYRA_DUMP_PROMPT=1 first.")
        return 1
    with open(DUMP, encoding="utf-8") as fh:
        messages = json.load(fh)
    print(f"dump: system={len(messages[0]['content'])} chars  "
          f"user={len(messages[1]['content'])} chars")
    for label, opts in (
        ("no-batch", {"temperature": 0.3, "num_predict": 270, "num_ctx": 4096}),
        ("batch2048", {"temperature": 0.3, "num_predict": 270, "num_ctx": 4096,
                       "num_batch": 2048, "num_ubatch": 2048}),
        ("batch2048 warm ctx", {"temperature": 0.3, "num_predict": 270,
                                "num_ctx": 4096, "num_batch": 2048,
                                "num_ubatch": 2048}),
    ):
        print(f"\n### {label}")
        for _ in range(3):
            run(messages, opts, label)
    return 0


if __name__ == "__main__":
    sys.exit(main())
