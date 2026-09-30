"""Deterministic question analysis for the real-time answer path.

Classifies a question into structured metadata *before* the
ContextBuilder/Bonsai prompt so coding questions get a strict,
executable-code answer format while behavioral / project / technical
questions keep the humanized (usually first-person, non-essay) style.

Everything here is keyword/pattern based — no extra LLM call, no model,
no inference. The same analyzer serves manual, voice AND screenshot
(OCR) questions; the request `source` is only recorded as metadata.

``analyze_question`` is a pure function usable in tests. ``previous``
carries the prior turn's analysis so follow-ups like "now add
authentication" inherit the coding intent / language / framework instead
of resetting to "general".
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_WS = re.compile(r"\s+")


def _norm(text: str) -> str:
    """Lowercase, punctuation-stripped, whitespace-collapsed form."""
    return _WS.sub(" ", _NON_ALNUM.sub(" ", text or "")).strip()


def _has(haystack: str, phrase: str) -> bool:
    """Word-bounded substring check on space-joined text."""
    return f" {phrase} " in f" {haystack} "


def _has_any(haystack: str, phrases) -> bool:
    for p in phrases:
        if _has(haystack, p) or f" {p} " in f" {haystack} ":
            return True
    return False


# ── Language & framework tables ──────────────────────────────────────────────
# (matcher seen in the RAW lowercased text, language, canonical framework).
_COMPOUND = [
    ("node.js", "javascript", "node.js"),
    ("nodejs", "javascript", "node.js"),
    ("express.js", "javascript", "express"),
    ("expressjs", "javascript", "express"),
    ("next.js", "react", "next.js"),
    ("nextjs", "react", "next.js"),
    ("django", "python", "django"),
    ("flask", "python", "flask"),
    ("fastapi", "python", "fastapi"),
    ("spring boot", "java", "spring boot"),
    ("spring", "java", "spring boot"),
    ("react.js", "react", ""),
    ("reactjs", "react", ""),
    ("mysql", "sql", ""),
    ("postgresql", "sql", ""),
    ("postgres", "sql", ""),
    ("sqlite", "sql", ""),
    ("microsoft sql server", "sql", ""),
]
# Word-boundary generic aliases (checked on the normalized text).
_WORD_ALIASES = [
    ("python", "python", ""),
    ("javascript", "javascript", ""),
    ("js", "javascript", ""),
    ("java", "java", ""),
    ("cpp", "c/c++", ""),
    ("sql", "sql", ""),
    ("react", "react", ""),
]
_C_RAW_RE = re.compile(r"c\+\+|\bcpp\b|\bin\s+c\b|\busing\s+c\b|\bc\s+code\b")


def _detect_language_framework(text: str) -> tuple[str, str]:
    """Return (language, framework); both '' when nothing is detected."""
    raw = (text or "").lower()
    norm = _norm(raw)
    # Compound tokens first (they carry both language and framework).
    for matcher, lang, fw in _COMPOUND:
        if matcher in raw:
            return lang, fw
    # Generic word aliases.
    for word, lang, fw in _WORD_ALIASES:
        if re.search(rf"\b{re.escape(word)}\b", norm, re.IGNORECASE):
            return lang, fw
    if _C_RAW_RE.search(raw):
        return "c/c++", ""
    return "", ""


# ── Intent phrase tables (deterministic, ordered by priority) ────────────────

_SQL_QUERY_PHRASES = [
    "write a query", "write the query", "write a sql", "write a sql query",
    "sql query", "a sql", "query that", "query for", "how to query",
    "how do you query", "write the sql",
]

_PROJECT_REFS = [
    "my project", "my application", "my app", "my implementation",
    "my backend", "my frontend", "my api", "my database",
    "my authentication", "my code", "in my project", "on my project",
    "from my project", "for my project", "of my project",
    "how did you implement", "how did you build", "why did you use",
    "why did you choose", "what did you use", "what did you build",
    "explain this project", "talk about this project", "your project",
    "the project you built", "the implementation of",
]

# Unambiguous requests for executable code.
_STRONG_CODING = [
    "write a program", "write the program", "write program",
    "write code", "write the code", "write a function", "write a method",
    "write a class", "write an api", "write a script", "write a server",
    "write a component", "write a react", "create a program", "create a class",
    "create a function", "create an api", "create a server", "create a component",
    "create api", "build a program", "build a function", "build a class",
    "build a server", "build an api", "build a react", "create a react",
    "implement this", "implement that", "implement a", "implement the",
    "code this", "code that", "solve this", "solve the",
    "coding question", "code question",
    "give me the code", "show me the code", "give the code", "show the code",
    "code in", "solution in", "code for",
    "program to", "function to", "class to", "script to", "server to",
    "component to",
    "how would you write", "how do you write", "how would you implement",
    "would you implement", "how to implement",
    "write a", "create a", "build a",
    "make a program", "make a function", "make a server",
    "the code from", "code from",
]

# Vague-but-coding phrasing ("how to use X") — only honoured when the question
# is NOT framed as a concept/behavioral/resume question (e.g. "Explain how to
# use Node.js" stays a technical_concept; "how to use nodejs" is a program).
_WEAK_CODING = [
    "how to use", "how to write", "how to create", "how to build",
    "how to code",
]

_DEBUG_PHRASES = [
    "debug", "debugging", "debugger", "why is it not working",
    "why is my code", "why does my code", "why does the code",
    "not working", "isn't working", "is broken", "it crashes",
    "it keeps crashing", "stack trace", "traceback", "fix this", "fix the",
    "how would you fix", "how to fix", "fix my", "bug", "buggy",
    "segfault", "null reference", "nullpointerexception", "exception",
    "the error", "error message",
]

_TECH_CONCEPT_PHRASES = [
    "explain", "what is", "what are", "what's", "whats", "define",
    "what does", "means", "describe how",
    "how does", "how do", "how it works", "why is", "why does",
    "difference between", "differences", "versus", " vs ",
    "event loop", "concept", "architecture", "pros and cons",
    "when would you use", "when do you use", "best practices",
    "under the hood", "behind the scenes",
]

_BEHAVIORAL_PHRASES = [
    "tell me about yourself", "walk me through yourself",
    "describe a time", "tell me about a time", "a situation where",
    "team conflict", "conflict with", "strengths", "weaknesses",
    "greatest strength", "greatest weakness", "most proud", "proud of",
    "challenge you faced", "led a", "leadership", "how do you handle",
    "how do you work", "work under pressure",
    "tell me about a project you are proud", "favorite project",
]

_RESUME_PHRASES = [
    "resume", "your background", "your experience", "years of experience",
    "previous company", "previous role", "work history", "internship",
    "education", "degree", "university", "college", "your school",
    "what did you study", "professional experience",
    "what technologies have you used",
]


@dataclass
class QuestionAnalysis:
    """Structured intent metadata attached to a question generation request."""
    intent: str = "general"
    requires_code: bool = False
    language: str = ""
    framework: str = ""
    question_type: str = "general"
    source: str = ""
    followup: bool = False

    def to_dict(self) -> dict:
        return {
            "intent": self.intent,
            "requires_code": bool(self.requires_code),
            "language": self.language or "",
            "framework": self.framework or "",
            "question_type": self.question_type or "",
            "source": self.source or "",
            "followup": bool(self.followup),
        }


def analyze_question(
    question: str,
    *,
    source: str = "",
    previous: Optional[QuestionAnalysis] = None,
    followup: bool = False,
) -> QuestionAnalysis:
    """Classify ``question`` into structured metadata.

    ``previous`` is the last turn's analysis; when the current question gives
    no explicit intent AND the previous question required code, the new
    question inherits the coding intent / language / framework (follow-up
    continuity) instead of falling back to "general".
    """
    text = (question or "").strip()
    raw = text.lower()
    language, framework = _detect_language_framework(text)

    sql_marker = language == "sql" or _has_any(raw, _SQL_QUERY_PHRASES)
    strong_coding = _has_any(raw, _STRONG_CODING)
    weak_coding = _has_any(raw, _WEAK_CODING)
    debugging = _has_any(raw, _DEBUG_PHRASES)
    technical = _has_any(raw, _TECH_CONCEPT_PHRASES)
    behavioral = _has_any(raw, _BEHAVIORAL_PHRASES)
    resume = _has_any(raw, _RESUME_PHRASES)
    project = _has_any(raw, _PROJECT_REFS)

    analysis = QuestionAnalysis(source=source, followup=bool(followup))

    # SQL questions are first-class (queries are code too).
    if sql_marker:
        analysis.language = language or "sql"
        analysis.framework = framework
        analysis.requires_code = bool(
            strong_coding or _has_any(raw, [
                "write a query", "write the query", "write a sql",
                "how to query", "how do you query"]))
        analysis.intent = "sql"
        analysis.question_type = "sql_query" if analysis.requires_code else "sql_concept"

    # Project questions keep the resume/project RAG first-person style and
    # only escalate to code when code is explicitly requested.
    elif project:
        analysis.language = language
        analysis.framework = framework
        analysis.requires_code = bool(
            strong_coding or debugging or _has(raw, "code"))
        analysis.question_type = (
            "project_coding" if analysis.requires_code else "project_explanation")
        analysis.intent = analysis.question_type

    elif strong_coding:
        analysis.language = language
        analysis.framework = framework
        analysis.requires_code = True
        analysis.intent = "coding"
        analysis.question_type = "code_generation"

    elif weak_coding and not (technical or behavioral or resume):
        analysis.language = language
        analysis.framework = framework
        analysis.requires_code = True
        analysis.intent = "coding"
        analysis.question_type = "code_generation"

    elif debugging:
        analysis.language = language
        analysis.framework = framework
        analysis.requires_code = True
        analysis.intent = "debugging"
        analysis.question_type = "code_fix"

    elif behavioral:
        analysis.intent = "behavioral"
        analysis.question_type = "behavioral"

    elif resume:
        analysis.intent = "resume"
        analysis.question_type = "resume"

    elif technical:
        analysis.language = language
        analysis.framework = framework
        analysis.intent = "technical_concept"
        analysis.question_type = "concept_explanation"

    else:
        analysis.language = language
        analysis.framework = framework
        analysis.intent = "general"
        analysis.question_type = "general"

    # Follow-up continuity: a follow-up with no explicit signal that continues
    # a coding question keeps producing code ("now add authentication").
    if (analysis.intent == "general" and not analysis.requires_code
            and previous is not None and previous.requires_code):
        analysis.intent = previous.intent
        analysis.requires_code = True
        analysis.question_type = previous.question_type or "code_generation"
        if not analysis.language:
            analysis.language = previous.language
        if not analysis.framework:
            analysis.framework = previous.framework

    return analysis


# ── Coding answer mode (only attached when requires_code is True) ────────────

def _language_specific_block(analysis: QuestionAnalysis) -> str:
    lang = (analysis.language or "").lower()
    fw = (analysis.framework or "").lower()
    if lang == "javascript":
        if fw.startswith("node"):
            return ("Language note: use JavaScript compatible with Node.js. "
                    "Prefer Node.js built-in APIs unless the question asks "
                    "for another library. Provide runnable code. Do not use "
                    "browser-only APIs.")
        return ("Language note: use JavaScript with the runtime the question "
                "implies (Node.js unless browser APIs are being discussed).")
    if lang == "python":
        return "Language note: use runnable Python 3 code."
    if lang == "java":
        return ("Language note: use standard Java unless a framework is "
                "explicitly requested.")
    if lang == "c/c++":
        return ("Language note: use standard C++ (or C, as asked); avoid "
                "platform-specific APIs.")
    if lang == "sql":
        return ("Language note: provide the SQL query first, followed by a "
                "short explanation.")
    if lang == "react":
        return ("Language note: use functional React with hooks and JSX "
                "compatible with the version implied by the question.")
    return ""


def coding_prompt_block(analysis: Optional[QuestionAnalysis]) -> str:
    """Dedicated instruction block for coding questions; '' otherwise.

    Only ever included in the prompt when ``requires_code`` is True, so
    behavioral / project / technical questions keep the humanized style and
    the prompt is never bloated.
    """
    if analysis is None or not analysis.requires_code:
        return ""
    lines = [
        "TASK TYPE: CODE GENERATION",
        "The interviewer is asking for a programming solution.",
        "You MUST provide actual executable code.",
        "Rules:",
        "- Start with a short approach or assumption.",
        "- Provide complete code.",
        "- Do NOT answer only with conceptual explanation.",
        "- Do NOT replace code with pseudocode unless the interviewer "
        "explicitly asks for pseudocode.",
        "- Keep the code interview-appropriate and runnable.",
        "- Briefly explain the important parts after the code.",
        "- Include example input/output when applicable.",
        "- Include how to run the code when useful.",
        "- Include complexity only when applicable.",
        "- Never invent project-specific technologies or implementation "
        "details.",
        "- If requirements are ambiguous, make the simplest reasonable "
        "assumption and state it briefly.",
        "",
        "CODING ANSWER FORMAT:",
        "1. Approach / assumption",
        "2. Complete code",
        "3. Example / output",
        "4. How to run",
        "5. Complexity, if applicable",
        "",
        "CRITICAL:",
        "The answer MUST contain a fenced code block (```).",
    ]
    if analysis.language:
        lines.append("")
        lines.append("Detected language: %s%s" % (
            analysis.language,
            (" (%s)" % analysis.framework) if analysis.framework else "",
        ))
        note = _language_specific_block(analysis)
        if note:
            lines.append(note)
    return "\n".join(lines)