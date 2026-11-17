"""Production-prompt-size prefill probe.

The earlier bench used a ~670-token prompt. The live path sends ~1300-1850
tokens (system + sectioned user prompt + RAG context). This probe scales the
prompt to the live size and measures the WALL time to the first streamed
token — the number the candidate actually waits for — with and without the
prefill batch sizes.
"""
from __future__ import annotations

import json
import sys
import time

import requests

OLLAMA = "http://localhost:11434"
MODEL = "qwen3.5:4b"

SYSTEM = (
    "You are Kiyra, a real-time interview copilot. The candidate reads your "
    "answer off a small overlay and speaks it aloud immediately.\n"
    "LENGTH: write 150-190 words. Hard minimum 130 words — a shorter answer "
    "is a failed answer. Never stop mid-sentence. Cover five parts: a "
    "one-sentence thesis; how it actually works; three bullets, each naming "
    "a concrete mechanism, API or number; one concrete example (generic, "
    "never a personal project); one trade-off or gotcha. Prefer specific "
    "mechanisms, names and numbers over generic statements. Never restate "
    "the question.\n"
    "GROUNDING (PRIMARY RULE): the candidate context (resume, projects, "
    "internships, education, metrics) is the source of truth. NEVER invent "
    "projects, technologies, work experience, internships, responsibilities, "
    "achievements, metrics, certifications or education. If the context does "
    "not support something, answer generally without claiming personal "
    "experience you cannot back up.\n"
    "VOICE: when the question is about the candidate's experience, answer in "
    "first person ('I built X to solve Y; I used A, B and C.'). Never say "
    "'according to the resume', 'the candidate' or 'based on the documents'.\n"
    "FOLLOW-UPS & CONTINUITY: follow-ups ('it', 'that approach', 'again') "
    "refer to ACTIVE TOPIC, PREVIOUS CONTEXT and RECENT CONVERSATION — work "
    "out the referent, then answer it from scratch, worded freshly. Stay "
    "factually consistent with the session — never contradict it — but never "
    "restate or continue an earlier answer. Prefer established "
    "project/technology names from context over generic ones.\n"
    "STYLE: sound like a real candidate. Most relevant context first, merge "
    "duplicates, ignore irrelevant chunks, never combine unrelated projects. "
    "Avoid buzzwords, robotic phrasing, disclaimers and long theory. If a "
    "claim (e.g. deployment) is unsupported, say so briefly instead of "
    "inventing it.\n"
    "OUTPUT: plain text only. No headings, citations, meta commentary or "
    "preamble — exactly what the candidate reads aloud."
)

CONTEXT_BLOCK = (
    "[QA_PAIR [Node.js Express Q1: What is Node.js?]]: Q1: What is Node.js?\n"
    "A: Node.js is a runtime environment that allows JavaScript to run outside "
    "the browser. It is built on Chrome's V8 engine and is used for building "
    "scalable server-side applications.\n\n"
)

REQUIREMENTS = (
    "ANSWER REQUIREMENTS:\n"
    "- Answer in first person as the candidate (\"I built…\"); never say "
    "\"according to the resume\", \"the candidate\", or \"based on the "
    "documents\".\n"
    "- Ground every personal claim in RELEVANT KNOWLEDGE or PREVIOUS "
    "CONTEXT; never invent projects, technologies, metrics, or experience.\n"
    "- If CURRENT QUESTION reaches back to an earlier turn, use ACTIVE TOPIC "
    "and RECENT CONVERSATION to work out WHAT it refers to, then answer that "
    "referent from scratch. Stay factually consistent with the session, but "
    "a consistent answer is usually worded differently.\n"
    "- Plain speakable text only: no headings, citations, or meta commentary. "
    "Follow the length rules from the system prompt.\n"
    "- Answer ONLY the CURRENT QUESTION, and start from the beginning.\n"
    "- Length: detailed mode means 150-190 words. Cover a one-sentence "
    "thesis, three bullets that each name a concrete mechanism, API or "
    "number, one concrete example, and one trade-off. A one-paragraph "
    "definition is NOT enough. Finish on a complete sentence — a cut-off "
    "answer is a failed answer."
)


def build_user(repeats: int) -> str:
    return (
        "CANDIDATE:\nAbhinay Bethi\n\n"
        "ACTIVE TOPIC:\nKiyra project architecture\n\n"
        "PREVIOUS CONTEXT:\nWe discussed the FastAPI backend and the "
        "desktop overlay.\n\n"
        "RELEVANT KNOWLEDGE:\n" + (CONTEXT_BLOCK * repeats) + "\n\n"
        "QUESTION TYPE:\nconceptual\n\n"
        "RECENT CONVERSATION:\n(none)\n\n"
        "CURRENT QUESTION:\nExplain my Kiyra project and the architecture you "
        "used.\n\n" + REQUIREMENTS
    )


def run(options: dict, user: str, label: str) -> dict:
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": user},
        ],
        "stream": True,
        "think": False,
        "keep_alive": "30m",
        "options": {"temperature": 0.5, "num_predict": 270, "num_ctx": 4096,
                    **options},
    }
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
    return {
        "label": label,
        "wall_ms": round((time.monotonic() - t0) * 1000),
        "first_token_ms": round(first_ms or 0),
        "load_ms": round((final.get("load_duration") or 0) / 1e6),
        "prefill_ms": round((final.get("prompt_eval_duration") or 0) / 1e6),
        "gen_ms": round((final.get("eval_duration") or 0) / 1e6),
        "ptok": final.get("prompt_eval_count"),
        "ctok": final.get("eval_count"),
        "done": final.get("done_reason"),
        "chars": len("".join(text)),
    }


def show(r: dict):
    print(f"  {r['label']:<22} first_token={r['first_token_ms']:>6}ms  "
          f"wall={r['wall_ms']:>6}ms  load={r['load_ms']:>5} "
          f"prefill={r['prefill_ms']:>6} gen={r['gen_ms']:>6}  "
          f"ptok={r['ptok']} ctok={r['ctok']} done={r['done']}")


def main() -> int:
    variants = [
        ("ctx4096 no-batch", {}),
        ("ctx4096 batch1024", {"num_batch": 1024, "num_ubatch": 1024}),
        ("ctx4096 batch2048", {"num_batch": 2048, "num_ubatch": 2048}),
        ("ctx2048 batch1024", {"num_ctx": 2048, "num_batch": 1024,
                               "num_ubatch": 1024}),
        ("ctx3072 batch1024", {"num_ctx": 3072, "num_batch": 1024,
                               "num_ubatch": 1024}),
    ]
    for repeats, tag in ((1, "small ~820tok"), (3, "live ~950tok"),
                         (6, "big ~1135tok")):
        user = build_user(repeats)
        print(f"\n{'=' * 104}\n### {tag}\n{'=' * 104}")
        for label, opts in variants:
            for attempt in (1, 2, 3):
                r = run(opts, user, label)
                show(r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
