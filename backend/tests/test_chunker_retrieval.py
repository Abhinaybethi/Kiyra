"""Tests for text chunking and document parsing."""
import pytest
from knowledge.chunker import chunk_text, chunk_document
from services.document_parser import validate_file, extract_text, _clean_text
from realtime.transcription import detect_question


def test_chunk_text_basic():
    text = "Word " * 600
    chunks = list(chunk_text(text, chunk_size=100, overlap=10))
    assert len(chunks) > 1
    assert all(len(c.split()) <= 100 for c in chunks)


def test_chunk_document():
    text = "Paragraph one with some information. Paragraph two with technical experience."
    chunks = chunk_document(text, metadata={"type": "resume"})
    assert len(chunks) >= 1
    assert chunks[0]["metadata"]["type"] == "resume"


def test_validate_file_security():
    # Path traversal protection test
    cleaned = validate_file("../../../etc/passwd.pdf", b"pdf content")
    assert ".." not in cleaned
    assert "/" not in cleaned

    # Invalid extension
    with pytest.raises(ValueError, match="Unsupported file type"):
        validate_file("virus.exe", b"bad code")


def test_clean_text():
    raw = "Hello   world!\n\n\n\nNew paragraph."
    cleaned = _clean_text(raw)
    assert cleaned == "Hello world!\n\nNew paragraph."


def test_detect_question():
    is_q, conf = detect_question("Can you explain how React hooks work under the hood?")
    assert is_q is True
    assert conf >= 0.5

    is_not_q, conf2 = detect_question("I have been working with Python and FastAPI for three years.")
    assert is_not_q is False
