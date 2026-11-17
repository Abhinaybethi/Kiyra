"""Production-prompt prefill probe with VRAM sampling.

Builds the EXACT prompt the live path builds (same system prompt, same
ContextBuilder sections, same RAG context) and measures the time to the
first streamed token for each candidate batch size, while sampling GPU
memory — because a batch size that does not fit in VRAM makes Ollama fall
back to the CPU, which is 10x slower than a slow GPU.

Run:  cd backend && uv run python tests\\integration\\bench_production_prompt.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time

import requests

sys.path.insert(0, ".")

from config import settings  # noqa: E402
from realtime.memory import ContextBuilder  # noqa: E402
from realtime.prompts import realtime_system  # noqa: E402

OLLAMA = "http://localhost:11434"
MODEL = "qwen3.5:4b"

RAG = ("[QA_PAIR [Node.js Express Q1: What is Node.js?]]: Q1: What is Node.js?\n"
       "A: Node.js is a runtime environment that allows JavaScript to run outside "
       "the browser. It is built on Chrome's V8 engine and is used for building "
       "scalable server-side applications.\n\n"
       "[QA_PAIR [Node.js Express Q11: How does Node.js handle concurrency if it "
       "is single-threaded?]]: Q11: How does Node.js handle concurrency if it is "
       "single-threaded?\nA: Node.js uses an event loop and the libuv thread pool "
       "for asynchronous I/O, so one thread can serve many connections.\n\n"
       "[RESUME [Abhinaybethi-cap-Resume.pdf]]: BETHI ABHINAY SOFTWARE ENGINEER\n\n"
       "OBJECTIVE\nComputer Science Engineering graduate seeking a Software "
       "Engineer role to apply strong foundations in Python, Java, "
       "object-oriented programming, data structures, REST APIs, databases and "
       "software development.\n\nPROJECTS\nF.R.I.D.A.Y. - Modular Voice "
       "Assistant Platform (Python, LiveKit, Docker). Expense-Splitter - "
       "Full-Stack Expense Splitting Application (React, Node.js, Express).\n\n"
       "TECHNICAL SKILLS\nLanguages: Python, Java, JavaScript, SQL\nFrameworks: "
       "FastAPI, Flask, Django, Node.js, Express.js, REST APIs\n")

QUESTION = "Write a Python function that reverses a linked list."


def _gpu() -> tuple[int, int]:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5)
        used, total = out.stdout.strip().split("\n")[0].split(",")
        return int(used), int(total)
    except Exception:
        return -1, -1


class VRAMWatcher(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.peak_used = 0
        self.total = 0
        self._stop = threading.Event()

    def run(self):
        while not self._stop.is_set():
            used, total = _gpu()
            if used > self.peak_used:
                self.peak_used = used
            self.total = total
            self._stop.wait(0.25)

    def stop(self):
        self._stop.set()


def build_prompt() -> tuple[str, str]:
    system = realtime_system(settings.answer_mode, requires_code=True,
                             qtype="coding")
    user = ContextBuilder.assemble(
        question=QUESTION,
        candidate_name="Abhinay Bethi",
        context=RAG,
        memory=None,
        resolution=None,
        analysis=None,
        qtype="coding",
    )
    return system, user


def run(options: dict, system: str, user: str, label: str) -> dict:
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": True,
        "think": False,
        "keep_alive": "30m",
        "options": {"temperature": 0.3, "num_predict": 520, "num_ctx": 4096,
                    **options},
    }
    watcher = VRAMWatcher()
    watcher.start()
    t0 = time.monotonic()
    first_ms = None
    final = {}
    text = []
    with requests.post(f"{OLLAMA}/api/chat", json=payload, timeout=300,
                       stream=True) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            if not line:
                continue
            chunk = json.loads(line)
            if (chunk.get("message", {}) or {}).get("content"):
                if first_ms is None:
                    first_ms = (time.monotonic() - t0) * 1000
                text.append(chunk["message"]["content"])
            if chunk.get("done"):
                final = chunk
                break
    wall = (time.monotonic() - t0) * 1000
    watcher.stop()
    watcher.join(timeout=1)
    return {
        "label": label,
        "wall_ms": round(wall),
        "first_token_ms": round(first_ms or 0),
        "load_ms": round((final.get("load_duration") or 0) / 1e6),
        "prefill_ms": round((final.get("prompt_eval_duration") or 0) / 1e6),
        "gen_ms": round((final.get("eval_duration") or 0) / 1e6),
        "ptok": final.get("prompt_eval_count"),
        "ctok": final.get("eval_count"),
        "done": final.get("done_reason"),
        "words": len("".join(text).split()),
        "vram_peak": watcher.peak_used,
        "vram_total": watcher.total,
    }


def show(r: dict):
    print(f"  {r['label']:<20} first_token={r['first_token_ms']:>6}ms  "
          f"wall={r['wall_ms']:>6}ms  load={r['load_ms']:>5} "
          f"prefill={r['prefill_ms']:>6} gen={r['gen_ms']:>6}  "
          f"ptok={r['ptok']} ctok={r['ctok']} words={r['words']:>4} "
          f"done={r['done']}  vram={r['vram_peak']}/{r['vram_total']}MiB")


def main() -> int:
    system, user = build_prompt()
    print(f"system chars={len(system)}  user chars={len(user)}")
    variants = [
        ("no-batch", {}),
        ("batch1024", {"num_batch": 1024, "num_ubatch": 1024}),
        ("batch2048", {"num_batch": 2048, "num_ubatch": 2048}),
    ]
    for label, opts in variants:
        print(f"\n### {label}")
        for attempt in (1, 2, 3):
            show(run(opts, system, user, label))
    return 0


if __name__ == "__main__":
    sys.exit(main())
