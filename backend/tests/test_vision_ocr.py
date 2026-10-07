"""Unit tests for the OCR text/question post-processing (Phase 5.1.1 bug:
RapidOCR returns one box per word, so texts are newline-smashed and the
analyzer/Bonsai received a garbled question)."""
import pytest

from vision.ocr import extract_question, looks_like_code


def test_extract_question_flattens_word_per_line_ocr():
    text = "Write\nNode.js\na\nprogram\nto\ncreate\nan\nHTTP\nserver."
    assert extract_question(text) == "Write Node.js a program to create an HTTP server."


def test_extract_question_flattens_multiline_question():
    text = "How\nwould\nyou\noptimize\nthis\nSQL\nquery?"
    assert extract_question(text) == "How would you optimize this SQL query?"


def test_extract_question_preserves_plain_question():
    assert extract_question("What is a deadlock?") == "What is a deadlock?"


def test_extract_question_empty_returns_empty():
    assert extract_question("") == ""
    assert extract_question("   \n  ") == ""


def test_extract_question_returns_first_question_in_sentence_flow():
    text = ("The candidate said \"I built it\". Now write a Node.js program "
            "to create an HTTP server? Please include code.")
    q = extract_question(text)
    assert "Node.js" in q and q.endswith("?")


def test_looks_like_code_unchanged():
    code = "def f():\n    return 1\nprint(f())"
    assert looks_like_code(code) is True
    assert looks_like_code("Write a Node.js program.") is False