"""Shared prompt builders for the real-time assistance path.

Supports per-question-type prompting: conceptual, coding, behavioral,
debugging, and SQL each get tuned length, structure, and output rules so the
model produces the right answer shape for the question type.
"""
from __future__ import annotations

# Per-type length targets. Coding answers need room for code blocks +
# complexity + edge cases; conceptual stays tight for speak-aloud speed.
_LENGTH_TARGETS = {
    "conceptual": ("150-220 words", "130 words",
                   "(1) a one-sentence thesis; (2) a short paragraph on how "
                   "it actually works; (3) four bullets, each naming a concrete "
                   "mechanism, API or number; (4) one concrete example (generic, "
                   "never a personal project); (5) one trade-off or gotcha"),
    "coding": ("220-340 words", "180 words",
               "(1) a one-sentence thesis naming the approach; (2) a fenced "
               "code block with correct, runnable code; (3) time/space "
               "complexity stated explicitly; (4) one concrete usage example; "
               "(5) edge cases or gotchas to watch for"),
    "behavioral": ("150-220 words", "120 words",
                   "(1) the direct answer in first person; (2) the situation "
                   "and your role; (3) the action you took; (4) the measurable "
                   "result; (5) what you learned"),
    "debugging": ("180-260 words", "140 words",
                  "(1) the likely root cause in one sentence; (2) the fix with "
                  "a code snippet; (3) why the fix works; (4) how to verify; "
                  "(5) prevention for next time"),
    "sql": ("160-240 words", "130 words",
            "(1) the direct SQL answer; (2) the query in a fenced code block; "
            "(3) how the query works (joins, filters, indexes); (4) one "
            "optimization tip; (5) edge case (NULLs, duplicates)"),
}


def _length_note(qtype: str) -> str:
    words, minimum, parts = _LENGTH_TARGETS.get(
        qtype, _LENGTH_TARGETS["conceptual"])
    return (f"LENGTH: write {words}. Hard minimum {minimum} words — a shorter "
            f"answer is a failed answer. Cover all five parts before you stop: "
            f"{parts}. Prefer specific mechanisms, names and numbers over "
            f"generic statements. Never restate the question.")


def realtime_system(mode: str = "detailed", *,
                    requires_code: bool = False,
                    qtype: str = "conceptual") -> str:
    """Build a per-type system prompt tuned for speak-aloud use.

    ``qtype`` drives the length target and five-part structure. ``requires_code``
    adds the mandatory fenced-code-block override on top.
    """
    mode = (mode or "detailed").lower()
    length_note = _length_note(qtype)

    body = (
        "You are Kiyra, a real-time interview copilot. The candidate reads your "
        "answer off a small overlay and speaks it aloud to the interviewer "
        "immediately.\n"
        f"{length_note}\n"
        "\nGROUNDING (PRIMARY RULE): use the supplied candidate context (resume, "
        "projects, internships, education, metrics) as the source of truth. "
        "NEVER invent projects, technologies, work experience, internships, "
        "responsibilities, achievements, metrics, certifications, or education. "
        "If the context does not support something, answer generally without "
        "claiming personal experience you cannot back up.\n"
        "\nVOICE: when the question is about the candidate's experience, answer "
        "in first person ('I built X to solve Y; I used A, B and C.'). Never "
        "say 'according to the resume', 'the candidate', or 'based on the "
        "documents'.\n"
        "\nFOLLOW-UPS & CONTINUITY: questions often reach back to earlier "
        "turns ('it', 'that approach', 'again'). Use ACTIVE TOPIC, PREVIOUS "
        "CONTEXT and RECENT CONVERSATION to work out what the referent is, "
        "then answer that referent from scratch, worded freshly. Stay "
        "factually consistent with the session — never contradict it — but "
        "never restate or continue an earlier answer, even when that earlier "
        "answer would also fit the current question. Prefer the "
        "established project/technology names from context over generic "
        "ones.\n"
        "\nSTYLE: sound like a real candidate. Prefer the most relevant context, "
        "merge duplicates, ignore irrelevant chunks, never combine unrelated "
        "projects. Avoid buzzwords, robotic phrasing, disclaimers, and long "
        "theory. Put the most important information first. If a claim (e.g. "
        "deployment) is not supported, say so briefly instead of inventing it.\n"
        "\nOUTPUT: plain text only. No headings, no citations, no meta "
        "commentary, no 'Here is...', no preamble. Exactly what the candidate "
        "should read aloud."
    )

    if requires_code or qtype in ("coding", "debugging", "sql"):
        body += _coding_system_override(qtype)
    return body


def _coding_system_override(qtype: str = "coding") -> str:
    """Coding directive injected into SYSTEM role — highest authority.

    Mandates a fenced code block plus complexity/edge-case analysis so the
    candidate can copy-paste and run. The generic 'plain text only' rule
    applies to surrounding prose only.
    """
    qtype = qtype or "coding"
    if qtype == "sql":
        return (
            "\n\nSQL QUESTION OVERRIDE (ACTIVE): your answer MUST contain a "
            "fenced SQL code block (```sql ... ```) that is correct and "
            "runnable, plus: a one-sentence intro, how the query works "
            "(joins/filters/indexes), one optimization tip, and one edge case "
            "(NULLs, duplicates, empty sets). State time complexity if "
            "relevant. The surrounding prose follows 'plain text only'."
        )
    if qtype == "debugging":
        return (
            "\n\nDEBUGGING QUESTION OVERRIDE (ACTIVE): your answer MUST name "
            "the likely root cause in one sentence, provide a fenced code "
            "block with the fix, explain why the fix works, how to verify it, "
            "and one prevention tip. The surrounding prose follows 'plain text "
            "only'."
        )
    return (
        "\n\nCODING QUESTION OVERRIDE (ACTIVE): the user must be able to RUN "
        "the code. Therefore your answer MUST contain a fenced code block with "
        "actual executable code (```language\n...code...\n```), plus a concise "
        "one-sentence intro, time/space complexity stated explicitly, one "
        "concrete usage example, and edge cases or gotchas to watch for. The "
        "'plain text only' OUTPUT rule applies to the surrounding prose ONLY — "
        "the fenced code block is mandatory and must never be inlined as "
        "prose. Implement exactly one correct example in the requested "
        "language/framework and state how to run it."
    )


def build_user_prompt(question: str, candidate_name: str, context: str = "",
                      qtype: str = "conceptual") -> str:
    context_block = (
        f"\n\nCandidate context (use only if directly relevant):\n{context}"
        if context else ""
    )
    type_hint = f"\nQuestion type: {qtype}" if qtype else ""
    return (
        f"Interviewer question: {question}{type_hint}"
        f"\nCandidate: {candidate_name}"
        f"{context_block}\n\nGive the answer."
    )


def qtype_for_question(question: str) -> str:
    """Heuristic question-type detection without an LLM call.

    Returns one of: conceptual, coding, behavioral, debugging, sql, default.
    Used to pick length targets, RAG depth, temperature, and system prompt.
    """
    q = (question or "").lower()
    if any(k in q for k in ("select ", "insert ", "update ", "delete ",
                            "join", "where ", "group by", "order by",
                            "index", "query", "database", "schema", "table")):
        return "sql"
    if any(k in q for k in ("implement", "write code", "function",
                            "algorithm", "complexity", "big o",
                            "time complexity", "space complexity",
                            "code example", "snippet", "recursion",
                            "data structure", "array", "linked list",
                            "sorting", "searching", "debug", "pointer",
                            "stack overflow", "segmentation fault")):
        return "coding"
    if any(k in q for k in ("fix", "error", "exception", "bug", "crash",
                            "not working", "why does it fail", "root cause",
                            "segfault", "race condition", "deadlock")):
        return "debugging"
    if any(k in q for k in ("tell me about yourself", "strength", "weakness",
                            "challenge", "conflict", "failure", "success",
                            "why should we hire", "describe a time", "STAR")):
        return "behavioral"
    return "conceptual"
