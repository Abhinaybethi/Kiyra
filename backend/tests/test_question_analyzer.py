"""Phase 5.1: deterministic question analysis + strict coding answer mode.

Pure unit tests for ``realtime.question_analyzer`` — no LLM, no server:
  * intent classification for the spec's example questions
  * language/framework detection (python/django, node.js/express, java/spring,
    C/C++, SQL, React/next.js)
  * requires_code escalation for ambiguous coding requests
  * project_explanation vs project_coding split
  * follow-up continuity (analysis inherited across turns)
  * coding_prompt_block content + language-specific notes, and guarantees
    that non-coding questions produce NO coding block (prompt never bloat).
"""
from __future__ import annotations

from realtime.question_analyzer import (
    QuestionAnalysis,
    analyze_question,
    coding_prompt_block,
)


# ── Intent + language classification of the spec examples ───────────────────

def test_ambiguous_nodejs_program_question():
    a = analyze_question("write a program how to use nodejs")
    assert a.intent == "coding"
    assert a.requires_code is True
    assert a.language == "javascript"
    assert a.framework == "node.js"
    assert a.question_type == "code_generation"


def test_nodejs_event_loop_is_concept_not_code():
    a = analyze_question("Explain Node.js event loop")
    assert a.intent == "technical_concept"
    assert a.requires_code is False
    assert a.language == "javascript"  # detected but unused without code
    assert a.framework == "node.js"


def test_write_nodejs_http_server_is_coding():
    a = analyze_question("Write a Node.js program to create an HTTP server")
    assert a.intent == "coding"
    assert a.requires_code is True
    assert a.language == "javascript"
    assert a.framework == "node.js"


def test_how_did_i_implement_is_project_explanation():
    a = analyze_question("How did I implement authentication in my project?")
    assert a.intent == "project_implementation"
    assert a.project_related is True
    assert a.project_intent == "project_implementation"
    assert a.requires_code is False


def test_show_me_code_from_project_is_project_coding():
    a = analyze_question("Show me the authentication code from my project")
    assert a.intent == "project_code"
    assert a.project_related is True
    assert a.project_intent == "project_code"
    assert a.requires_code is True


def test_behavioral_project_question_stays_behavioral():
    a = analyze_question("Tell me about a project you are proud of")
    assert a.intent == "behavioral"
    assert a.requires_code is False


# ── Language / framework detection ─────────────────────────────────────────

def test_language_framework_matrix():
    cases = [
        ("write a django view", ("python", "django")),
        ("write code using express", ("javascript", "express")),
        ("implement a Spring Boot endpoint", ("java", "spring boot")),
        ("solve this in C++", ("c/c++", "")),
        ("write a C program", ("c/c++", "")),
        ("write a query in SQL", ("sql", "")),
        ("fix this mysql query", ("sql", "")),
        ("build a react component", ("react", "")),
        ("create a next.js page", ("react", "next.js")),
        ("write a python program to reverse a string", ("python", "")),
    ]
    for question, (lang, fw) in cases:
        a = analyze_question(question)
        assert a.language == lang, f"{question!r} → language {a.language!r}"
        assert a.framework == fw, f"{question!r} → framework {a.framework!r}"


# ── requires_code escalation ────────────────────────────────────────────────

def test_cluster_coding_requests_require_code():
    for question in [
        "write code to parse json",
        "implement a binary search",
        "create a function that sorts",
        "give me the code for a singleton",
        "how to use python to read a file",
        "write a bst",
    ]:
        a = analyze_question(question)
        assert a.requires_code is True, question
        assert a.intent in ("coding", "sql"), question


def test_conceptual_questions_never_require_code():
    for question in [
        "Explain Node.js event loop",
        "What is the difference between REST and GraphQL?",
        "Why do you use indexes in a database?",
        "Describe how a TCP handshake works",
    ]:
        a = analyze_question(question)
        assert a.requires_code is False, question
        assert a.intent in ("technical_concept", "sql_concept", "general"), question


def test_behavioral_and_resume_never_require_code():
    a = analyze_question("Tell me about a time you faced a challenge at work")
    assert a.intent == "behavioral" and a.requires_code is False
    a = analyze_question("Walk me through your experience at your previous company")
    assert a.intent == "resume" and a.requires_code is False


def test_sql_query_requires_code_but_sql_concept_does_not():
    a = analyze_question("Write a SQL query to find duplicate emails")
    assert a.intent == "sql" and a.requires_code is True
    assert a.question_type == "sql_query"
    a = analyze_question("Explain how SQL indexes work")
    assert a.intent == "sql" and a.requires_code is False
    assert a.question_type == "sql_concept"


def test_debugging_question_requires_code():
    a = analyze_question("Why is my Node.js server crashing on startup?")
    assert a.intent == "debugging"
    assert a.requires_code is True
    assert a.language == "javascript"
    assert a.framework == "node.js"


# ── Follow-up continuity (do NOT reset analysis between turns) ──────────────

def test_followup_inherits_coding_intent_and_language():
    prev = analyze_question("Write a Node.js HTTP server")
    assert prev.requires_code is True
    follow = analyze_question(
        "Now add authentication", previous=prev)
    assert follow.requires_code is True
    assert follow.intent == prev.intent
    assert follow.language == "javascript"
    assert follow.framework == "node.js"


def test_followup_explicit_new_language_keeps_new_language():
    prev = analyze_question("Write a Node.js HTTP server")
    follow = analyze_question("Redo it in Python", previous=prev)
    assert follow.requires_code is True
    assert follow.language == "python"  # explicit new language wins
    assert follow.framework == ""       # node.js not inherited when python given


def test_no_inheritance_when_previous_not_coding():
    prev = analyze_question("Tell me about yourself")
    follow = analyze_question("How did you handle it?", previous=prev)
    assert follow.requires_code is False


# ── Source metadata is preserved, never dereferenced ────────────────────────

def test_screenshot_source_survives_analysis():
    a = analyze_question(
        "write a program how to use nodejs", source="screenshot")
    assert a.source == "screenshot"
    assert a.to_dict()["source"] == "screenshot"
    assert a.to_dict()["requires_code"] is True


# ── Coding prompt block ─────────────────────────────────────────────────────

def test_coding_block_present_only_when_code_required():
    coding = analyze_question("write a program how to use nodejs")
    block = coding_prompt_block(coding)
    assert "CODE GENERATION" in block
    assert "MUST provide actual executable code" in block
    assert "fenced code block" in block
    assert "CODING ANSWER FORMAT" in block
    assert "node.js" in block  # language-specific note
    assert "browser-only APIs" in block

    concept = analyze_question("Explain Node.js event loop")
    assert coding_prompt_block(concept) == ""
    assert coding_prompt_block(None) == ""
    assert coding_prompt_block(analyze_question(
        "How did I implement authentication in my project?")) == ""


def test_coding_block_language_specific_notes():
    assert "runnable Python 3 code" in coding_prompt_block(
        analyze_question("write a python program to read a csv"))
    assert "SQL query first" in coding_prompt_block(
        analyze_question("write a sql query to group by"))
    assert "standard Java" in coding_prompt_block(
        analyze_question("implement a java class"))


def test_analysis_to_dict_shape():
    a = analyze_question(
        "write a program how to use nodejs", source="screenshot")
    d = a.to_dict()
    assert set(d) == {
        "intent", "requires_code", "language", "framework",
        "question_type", "source", "followup",
        "project_related", "project_intent",
    }
    assert d["intent"] == "coding"
    assert d["requires_code"] is True