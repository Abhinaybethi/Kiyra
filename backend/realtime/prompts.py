"""Shared prompt builders for the real-time assistance path.

Supports per-question-type prompting: conceptual, coding, behavioral,
debugging, and SQL each get tuned length, structure, and output rules so the
model produces the right answer shape for the question type.
"""
from __future__ import annotations

# Per-type length targets. Coding answers need room for code blocks +
# complexity + edge cases; conceptual stays tight for speak-aloud speed.
#
# These are WORD counts. The matching token caps live in
# `config.answer_max_tokens_by_type` and must be sized from them — prose runs
# ~1.2 tokens/word and code ~1.6, so a cap set below the word band's top
# truncates the answer mid-sentence (the "…maintain stability" bug).
_LENGTH_TARGETS = {
    "conceptual": ("150-190 words", "130 words",
                   "a one-sentence thesis; how it actually works; three "
                   "bullets, each naming a concrete mechanism, API or number; "
                   "one concrete example (generic, never a personal project); "
                   "one trade-off or gotcha"),
    "coding": ("220-300 words", "180 words",
               "a one-sentence thesis naming the approach; a fenced code block "
               "with correct, runnable code; time/space complexity stated "
               "explicitly; one concrete usage example; edge cases or gotchas"),
    "behavioral": ("150-190 words", "120 words",
                   "the direct answer in first person; the situation and your "
                   "role; the action you took; the measurable result; what you "
                   "learned"),
    "debugging": ("180-230 words", "140 words",
                  "the likely root cause in one sentence; the fix with a code "
                  "snippet; why the fix works; how to verify; prevention for "
                  "next time"),
    "sql": ("160-210 words", "130 words",
            "the direct SQL answer; the query in a fenced code block; how the "
            "query works (joins, filters, indexes); one optimization tip; one "
            "edge case (NULLs, duplicates)"),
}


def _length_note(qtype: str) -> str:
    """The length + structure rule (one compact paragraph).

    Kept deliberately tight: every token here is re-processed on every turn
    (no prefix caching on this model), and a verbose rule also made the model
    overshoot the word band — it wrote 233 words against a 150-190 target,
    hit the token cap and got clipped mid-sentence.
    """
    words, minimum, parts = _LENGTH_TARGETS.get(
        qtype, _LENGTH_TARGETS["conceptual"])
    return (f"LENGTH: write {words}. Hard minimum {minimum} words — a shorter "
            f"answer is a failed answer. Never stop mid-sentence. Cover five "
            f"parts: {parts}. Prefer specific mechanisms, names and numbers "
            f"over generic statements. Never restate the question.")


def realtime_system(mode: str = "detailed", *,
                    requires_code: bool = False,
                    qtype: str = "conceptual") -> str:
    """Build a per-type system prompt tuned for speak-aloud use.

    ``qtype`` drives the length target and five-part structure. ``requires_code``
    adds the mandatory fenced-code-block override on top.

    The prompt is intentionally compact (≈300 tokens instead of ≈450). The
    rules are unchanged — every constraint is still stated — but the wording
    is terser, because prompt tokens are paid twice on a local model: once in
    the prefill and again in the attention the model spends re-reading them.
    """
    mode = (mode or "detailed").lower()
    length_note = _length_note(qtype)

    body = (
        "You are Kiyra, a real-time interview copilot. The candidate reads "
        "your answer off a small overlay and speaks it aloud immediately.\n"
        f"{length_note}\n"
        "\nGROUNDING (PRIMARY RULE): the candidate context (resume, projects, "
        "internships, education, metrics) is the source of truth. NEVER invent "
        "projects, technologies, work experience, internships, responsibilities, "
        "achievements, metrics, certifications or education. If the context "
        "does not support something, answer generally without claiming "
        "personal experience you cannot back up.\n"
        "\nVOICE: when the question is about the candidate's experience, answer "
        "in first person ('I built X to solve Y; I used A, B and C.'). Never "
        "say 'according to the resume', 'the candidate' or 'based on the "
        "documents'.\n"
        "\nFOLLOW-UPS & CONTINUITY: follow-ups ('it', 'that approach', 'again') "
        "refer to ACTIVE TOPIC, PREVIOUS CONTEXT and RECENT CONVERSATION — work "
        "out the referent, then answer it from scratch, worded freshly. Stay "
        "factually consistent with the session — never contradict it — but "
        "never restate or continue an earlier answer. Prefer established "
        "project/technology names from context over generic ones.\n"
        "\nSTYLE: sound like a real candidate. Most relevant context first, "
        "merge duplicates, ignore irrelevant chunks, never combine unrelated "
        "projects. Avoid buzzwords, robotic phrasing, disclaimers and long "
        "theory. If a claim (e.g. deployment) is unsupported, say so briefly "
        "instead of inventing it.\n"
        "\nOUTPUT: plain text only. No headings, citations, meta commentary or "
        "preamble — exactly what the candidate reads aloud."
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
            "(NULLs, duplicates, empty sets). The surrounding prose follows "
            "'plain text only'."
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
        "the code. Your answer MUST contain a fenced code block with actual "
        "executable code (```language\n...code...\n```), a one-sentence intro, "
        "time/space complexity stated explicitly, one concrete usage example, "
        "and edge cases or gotchas. The 'plain text only' OUTPUT rule applies "
        "to the surrounding prose ONLY. Implement exactly one correct example "
        "in the requested language/framework and say how to run it."
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
