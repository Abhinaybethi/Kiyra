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


# ─────────────────────────────────────────────────────────────────────────────
# Contrast retry: a low-contrast capture (dark theme / HDR / washed out) must
# not fail with a false "No text detected" — it is retried on an
# equalized copy.
# ─────────────────────────────────────────────────────────────────────────────

def test_extract_text_retries_with_contrast_enhancement(monkeypatch):
    import numpy as np
    from vision import ocr as ocr_mod

    calls = {"n": 0}

    def fake_engine(img):
        calls["n"] += 1
        if calls["n"] == 1:
            return (None, 0.0)  # raw pass finds nothing (low contrast)
        return ([[[[0, 0], [1, 0], [1, 1], [0, 1]], "What is a deadlock?", 0.98]], 0.1)

    monkeypatch.setattr(ocr_mod, "_get_engine", lambda: fake_engine)
    monkeypatch.setattr(ocr_mod, "_decode_image",
                        lambda b: np.zeros((4, 4, 3), dtype=np.uint8))

    out = ocr_mod.extract_text(b"fake")
    assert out["text"] == "What is a deadlock?"
    assert calls["n"] == 2, "must retry exactly once on the enhanced image"


def test_extract_text_raises_when_still_empty(monkeypatch):
    import numpy as np
    from vision import ocr as ocr_mod

    monkeypatch.setattr(ocr_mod, "_get_engine", lambda: (lambda img: (None, 0.0)))
    monkeypatch.setattr(ocr_mod, "_decode_image",
                        lambda b: np.zeros((4, 4, 3), dtype=np.uint8))

    with pytest.raises(RuntimeError, match="No text detected"):
        ocr_mod.extract_text(b"fake")