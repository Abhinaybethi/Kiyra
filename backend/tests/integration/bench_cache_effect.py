"""Is the fast bench prefill real, or is it KV-cache reuse?

Observation:
  * Standalone replay of the EXACT live prompt: prefill 178-267 ms
  * The same prompt through the live backend:      prefill 1638-2348 ms

Ollama caches the KV state of a prompt's prefix. When the same prompt is
sent twice in a row, the second request can reuse the whole cache — which
would make the standalone bench measure a cache hit, not a real prefill.

This probe separates the two by varying ONE thing: whether the prompt is
identical to the previous request (cache hit) or carries a unique suffix
(cache miss = the real cost the live path pays every turn).

Run:  cd backend && uv run python tests\\integration\\bench_cache_effect.py
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
    print(f"  {label:<30} first={round(first or 0):>6}ms  "
          f"prefill={round((final.get('prompt_eval_duration') or 0) / 1e6):>6}ms  "
          f"gen={round((final.get('eval_duration') or 0) / 1e6):>6}ms  "
          f"ptok={final.get('prompt_eval_count')}")
    return final


def main() -> int:
    if not os.path.exists(DUMP):
        print(f"No prompt dump at {DUMP}")
        return 1
    with open(DUMP, encoding="utf-8") as fh:
        base = json.load(fh)
    print(f"dump: system={len(base[0]['content'])} chars  "
          f"user={len(base[1]['content'])} chars")

    opts = {
        "temperature": 0.3, "num_predict": 270, "num_ctx": 4096,
        "num_batch": 2048, "num_ubatch": 2048,
    }
    no_batch = {
        "temperature": 0.3, "num_predict": 270, "num_ctx": 4096,
    }

    print("\n### batch2048 — IDENTICAL prompt each time (cache hit expected)")
    for i in range(3):
        run(base, opts, "identical")

    print("\n### batch2048 — UNIQUE suffix each time (cache miss = real cost)")
    for i in range(3):
        msgs = [dict(base[0]),
                {"role": "user",
                 "content": base[1]["content"] + f"\n\n[nonce-{i}-{time.time()}]"}]
        run(msgs, opts, "unique suffix")

    print("\n### no batch — UNIQUE suffix each time (cache miss)")
    for i in range(3):
        msgs = [dict(base[0]),
                {"role": "user",
                 "content": base[1]["content"] + f"\n\n[nonce-{i}-{time.time()}]"}]
        run(msgs, no_batch, "unique suffix")

    print("\n### no batch — IDENTICAL prompt each time (cache hit)")
    for i in range(2):
        run(base, no_batch, "identical")
    return 0


if __name__ == "__main__":
    sys.exit(main())
