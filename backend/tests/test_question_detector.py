"""Unit tests for deterministic question detection."""
import pytest

from realtime.question_detector import (
    QuestionDetector,
    analyze,
    detect,
    similarity,
)


def test_detect_yes_question():
    is_q, conf = detect("Can you explain how React hooks work under the hood?")
    assert is_q is True
    assert conf >= 0.5


def test_detect_procedural_question():
    is_q, conf = detect("Walk me through how you would design a rate limiter.")
    assert is_q is True
    assert conf >= 0.4


def test_detect_statement_is_not_question():
    is_q, _ = detect("I have been working with Python and FastAPI for three years.")
    assert is_q is False


def test_detect_wh_word_midstream():
    is_q, conf = detect("So what's the difference between a process and a thread?")
    assert is_q is True


def test_detect_short_text_insufficient():
    is_q, conf = detect("hi")
    assert is_q is False
    assert conf == 0.0


def test_analyze_signals_shape():
    result = analyze("Can you explain Event Loop in Node.js?")
    assert result["is_question"] is True
    assert 0.4 <= result["confidence"] <= 1.0
    assert "question_mark" in result["signals"]


def test_similarity():
    assert similarity("what is a class", "what is a class in python") > 0.5
    assert similarity("how does indexing work", "tell me about your hobbies") < 0.3


def test_dedup_rejects_near_duplicate():
    d = QuestionDetector(cooldown_seconds=0, dedup_threshold=0.78)
    q1 = "can you explain how react renders the dom"
    q2 = "can you explain how react renders the dom in detail"
    assert d.detect(q1)[0] is True
    assert d.detect(q2)[0] is False  # near-duplicate within window


def test_cooldown_but_high_confidence_bypasses():
    d = QuestionDetector(cooldown_seconds=60, high_confidence_bypass=0.8)
    now = 1000.0
    assert d.detect("which of these tools are you the most confident about", now=now)[0] is True
    # Distinct, high-confidence (question mark + strong start) question bypasses cooldown.
    ok, conf = d.detect("can you explain the difference between sql and nosql?", now=now + 0.5)
    assert ok is True
    assert conf >= 0.8


def test_low_confidence_within_cooldown_blocked():
    d = QuestionDetector(cooldown_seconds=60, high_confidence_bypass=0.8)
    now = 1000.0
    assert d.detect("which of these tools are you the most confident about", now=now)[0] is True
    ok, _ = d.detect("how do you handle a challenging deadline", now=now + 0.5)
    assert ok is False  # distinct but below 0.8 — blocked by cooldown


def test_reset_clears_state():
    d = QuestionDetector(cooldown_seconds=60)
    now = 1000.0
    d.detect("have you used microservices in production", now=now)
    d.reset()
    ok, _ = d.detect("have you used microservices in production", now=now + 0.1)
    assert ok is True