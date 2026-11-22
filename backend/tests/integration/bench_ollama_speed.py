"""Micro-benchmark for the Ollama generation path (no backend, no WebSocket).

Answers the question the live probe cannot: *where* does the wall-clock go,
and which knobs actually move it? Each variant is sent straight to
``/api/chat`` with the production payload, and Ollama's own timings
(``load_duration`` / ``prompt_eval_duration`` / ``eval_duration``) are
reported.

Variants tested:
  baseline        the exact production payload
  batch1024       + num_batch/num_ubatch = 1024 (bigger prefill batches)
  batch2048       + num_batch/num_ubatch = 2048
  trimmed         baseline with the shortened system prompt

Every variant is run TWICE: the first request may pay a runner reload
(changing num_batch re-allocates the compute pool), so only the second
request is representative of steady-state live use.

Run:  cd backend && uv run python tests\\integration\\bench_ollama_speed.py
"""
from __future__ import annotations

import json
import sys
import time
from typing import Any

import requests

OLLAMA = "http://localhost:11434"
MODEL = "qwen3.5:4b"
NUM_CTX = 4096
NUM_PREDICT = 270

QUESTION = "Explain my Kiyra project and the architecture you used."

SYSTEM = (
    "You are Kiyra, a real-time interview copilot. The candidate reads your "
    "answer off a small overlay and speaks it aloud to the interviewer "
    "immediately.\n"
    "LENGTH: write 150-190 words. Hard minimum 130 words — a shorter answer "
    "is a failed answer. Do not go over the range either: an answer that "
    "stops mid-sentence is a failed answer too. Cover all five parts before "
    "you stop: (1) a one-sentence thesis; (2) a short paragraph on how it "
    "actually works; (3) three bullets, each naming a concrete mechanism, "
    "API or number; (4) one concrete example (generic, never a personal "
    "project); (5) one trade-off or gotcha. Prefer specific mechanisms, "
    "names and numbers over generic statements. Never restate the question.\n"
    "GROUNDING (PRIMARY RULE): use the supplied candidate context (resume, "
    "projects, internships, education, metrics) as the source of truth. "
    "NEVER invent projects, technologies, work experience, internships, "
    "responsibilities, achievements, metrics, certifications, or education. "
    "If the context does not support something, answer generally without "
    "claiming personal experience you cannot back up.\n"
    "VOICE: when the question is about the candidate's experience, answer in "
    "first person ('I built X to solve Y; I used A, B and C.'). Never say "
    "'according to the resume', 'the candidate', or 'based on the "
    "documents'.\n"
    "FOLLOW-UPS & CONTINUITY: questions often reach back to earlier turns "
    "('it', 'that approach', 'again'). Use ACTIVE TOPIC, PREVIOUS CONTEXT "
    "and RECENT CONVERSATION to work out what the referent is, then answer "
    "that referent from scratch, worded freshly. Stay factually consistent "
    "with the session — never contradict it — but never restate or continue "
    "an earlier answer, even when that earlier answer would also fit the "
    "current question. Prefer the established project/technology names from "
    "context over generic ones.\n"
    "STYLE: sound like a real candidate. Prefer the most relevant context, "
    "merge duplicates, ignore irrelevant chunks, never combine unrelated "
    "projects. Avoid buzzwords, robotic phrasing, disclaimers, and long "
    "theory. Put the most important information first. If a claim (e.g. "
    "deployment) is not supported, say so briefly instead of inventing it.\n"
    "OUTPUT: plain text only. No headings, no citations, no meta commentary, "
    "no 'Here is...', no preamble. Exactly what the candidate should read "
    "aloud."
)

# The shortened system prompt (the "trimmed" variant). Same rules, fewer
# tokens: no duplicated "cover all five parts" list, tighter prose.
SYSTEM_TRIMMED = (
    "You are Kiyra, a real-time interview copilot. The candidate reads your "
    "answer off a small overlay and speaks it aloud immediately.\n"
    "LENGTH: 150-190 words, minimum 130. Never stop mid-sentence. Cover "
    "five parts: a one-sentence thesis; how it works; three bullets naming "
    "a concrete mechanism, API or number; one concrete example; one "
    "trade-off or gotcha. Prefer specific mechanisms, names and numbers over "
    "generic statements. Never restate the question.\n"
    "GROUNDING (PRIMARY RULE): the candidate context (resume, projects, "
    "internships, education, metrics) is the source of truth. NEVER invent "
    "projects, technologies, experience, responsibilities, achievements, "
    "metrics, certifications or education. If context does not support "
    "something, answer generally without claiming personal experience.\n"
    "VOICE: for questions about the candidate's experience, answer in first "
    "person ('I built X to solve Y; I used A, B and C.'). Never say "
    "'according to the resume' or 'the candidate'.\n"
    "CONTINUITY: follow-ups ('it', 'that approach', 'again') refer to ACTIVE "
    "TOPIC, PREVIOUS CONTEXT and RECENT CONVERSATION — answer that referent "
    "from scratch, worded freshly, consistent with the session, never "
    "restating an earlier answer. Prefer established project/technology "
    "names from context over generic ones.\n"
    "STYLE: sound like a real candidate. Most relevant context first, merge "
    "duplicates, ignore irrelevant chunks, never combine unrelated projects. "
    "No buzzwords, no robotic phrasing, no disclaimers, no long theory. If a "
    "claim is unsupported, say so briefly.\n"
    "OUTPUT: plain text only. No headings, citations, meta commentary or "
    "preamble — exactly what the candidate reads aloud."
)

CONTEXT = (
    "[RESUME]: BETHI ABHINAY SOFTWARE ENGINEER Hyderabad, Telangana, "
    "India. OBJECTIVE Computer Science Engineering graduate seeking a "
    "Software Engineer role to apply strong foundations in Python, Java, "
    "object-oriented programming, data structures, REST APIs, databases and "
    "software development. Interested in building reliable applications, "
    "solving engineering problems, and learning production software "
    "development. SKILLS Python, Java, JavaScript, React, Node.js, Express, "
    "REST APIs, SQL, Git, Docker. PROJECTS Kiyra - a local-first AI interview "
    "copilot with FastAPI, Next.js and a Windows overlay. EduScan - an "
    "automated document scanning tool."
)

USER = (
    f"Interviewer question: {QUESTION}\nQuestion type: conceptual\n"
    f"Candidate: Abhinay Bethi\n\nCandidate context (use only if directly "
    f"relevant):\n{CONTEXT}\n\nGive the answer."
)


def _ms(ns: Any) -> float:
    try:
        return round((ns or 0) / 1_000_000, 1)
    except Exception:
        return -1.0


def run(system: str, options: dict, label: str) -> dict:
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": USER},
        ],
        "stream": False,
        "think": False,
        "keep_alive": "30m",
        "options": {
            "temperature": 0.5,
            "num_predict": NUM_PREDICT,
            "num_ctx": NUM_CTX,
            **options,
        },
    }
    t0 = time.monotonic()
    try:
        r = requests.post(f"{OLLAMA}/api/chat", json=payload, timeout=300)
        r.raise_for_status()
    except Exception as e:
        print(f"  [{label}] FAILED: {e}")
        return {}
    wall = (time.monotonic() - t0) * 1000
    j = r.json()
    out = {
        "label": label,
        "wall_ms": round(wall),
        "load_ms": _ms(j.get("load_duration")),
        "prefill_ms": _ms(j.get("prompt_eval_duration")),
        "gen_ms": _ms(j.get("eval_duration")),
        "prompt_tokens": j.get("prompt_eval_count"),
        "completion_tokens": j.get("eval_count"),
        "done_reason": j.get("done_reason"),
        "text": (j.get("message", {}) or {}).get("content", ""),
    }
    gen_tok = out["completion_tokens"] or 0
    out["tok_per_s"] = round(gen_tok / (out["gen_ms"] / 1000), 1) if out["gen_ms"] else -1
    out["words"] = len(out["text"].split())
    return out


def show(r: dict, note: str = ""):
    if not r:
        return
    print(f"  {r['label']:<12} wall={r['wall_ms']:>6}ms  load={r['load_ms']:>7}ms  "
          f"prefill={r['prefill_ms']:>7}ms  gen={r['gen_ms']:>7}ms  "
          f"ptok={r['prompt_tokens']:>5}  ctok={r['completion_tokens']:>4}  "
          f"{r['tok_per_s']:>5} tok/s  words={r['words']:>4}  "
          f"done={r['done_reason']} {note}")


def main() -> int:
    variants = [
        ("baseline", SYSTEM, {}),
        ("batch1024", SYSTEM, {"num_batch": 1024, "num_ubatch": 1024}),
        ("batch2048", SYSTEM, {"num_batch": 2048, "num_ubatch": 2048}),
        ("trimmed", SYSTEM_TRIMMED, {}),
        ("trim+1024", SYSTEM_TRIMMED, {"num_batch": 1024, "num_ubatch": 1024}),
    ]
    print("=" * 108)
    print("Ollama speed bench — each variant run twice (first may pay a runner reload)")
    print("=" * 108)
    for label, system, opts in variants:
        print(f"\n### {label}")
        for attempt in (1, 2):
            r = run(system, opts, label)
            show(r, "(cold/reload)" if attempt == 1 else "(steady state)")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
