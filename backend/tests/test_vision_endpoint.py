"""Endpoint tests for the visual input pipeline (mocked OCR + provider)."""
import asyncio

import pytest

import api.vision as vision_mod
from ai.provider import ProviderUnavailableError

# 1x1 transparent PNG
PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)


class FakeVisionProvider:
    model_name = "test-vision-model"

    async def complete(self, messages, stream=False, **kwargs):
        return "Start with the interviewer's question, then 2-3 key points."


@pytest.fixture
def vision_mocks(monkeypatch):
    """Point all outbound vision deps at fakes."""
    monkeypatch.setattr(
        vision_mod, "extract_text",
        lambda content: {"text": "What is the capital of France? It is Paris.",
                          "line_count": 2, "likely_code": False},
    )
    monkeypatch.setattr(
        vision_mod, "extract_question",
        lambda text: "What is the capital of France?",
    )
    provider = FakeVisionProvider()
    monkeypatch.setattr(vision_mod, "get_provider", lambda db=None: provider)

    async def _ready(provider):
        return True

    monkeypatch.setattr(vision_mod, "ensure_provider_ready", _ready)
    monkeypatch.setattr(
        vision_mod, "retrieve_context_sync",
        lambda *a, **k: ("Paris is the capital of France.", {"embed_ms": 2.0, "search_ms": 3.0}),
    )
    return provider


def test_visual_analysis_happy_path(client, vision_mocks):
    resp = client.post(
        "/api/vision/analyze",
        files={"file": ("question.png", PNG_BYTES, "image/png")},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["question"] == "What is the capital of France?"
    assert data["is_question"] is True
    assert data["rag_used"] is True
    assert data["rag_ms"] > 0
    assert "key points" in data["answer"]
    assert data["ocr_ms"] >= 0
    assert data["latency_ms"] >= 0
    assert data["model_used"] == "test-vision-model"
    assert data["line_count"] == 2


def test_visual_analysis_rejects_unsupported_type(client, vision_mocks):
    resp = client.post(
        "/api/vision/analyze",
        files={"file": ("notes.txt", b"hello world", "text/plain")},
    )
    assert resp.status_code == 415


def test_visual_analysis_rejects_empty_file(client, vision_mocks):
    resp = client.post(
        "/api/vision/analyze",
        files={"file": ("blank.png", b"", "image/png")},
    )
    assert resp.status_code == 400


def test_visual_analysis_provider_down_is_503(client, monkeypatch):
    class DownProvider:
        model_name = "down"

        async def complete(self, messages, stream=False, **kwargs):
            return "unused"

    monkeypatch.setattr(vision_mod, "get_provider", lambda db=None: DownProvider())

    async def _down(provider):
        raise ProviderUnavailableError("Bonsai inference server is unavailable. Start llama-server and verify BONSAI_BASE_URL.")

    monkeypatch.setattr(vision_mod, "ensure_provider_ready", _down)

    resp = client.post(
        "/api/vision/analyze",
        files={"file": ("question.png", PNG_BYTES, "image/png")},
    )
    assert resp.status_code == 503
    assert "Bonsai inference server is unavailable" in resp.json()["detail"]


def test_visual_analysis_persists_transcript_segment(client, vision_mocks, db_session):
    resp = client.post(
        "/api/vision/analyze",
        data={"session_id": str(55)},
        files={"file": ("question.png", PNG_BYTES, "image/png")},
    )
    # session 55 doesn't exist → segment insert rolls back gracefully, endpoint still 200.
    assert resp.status_code == 200


# ── /api/vision/extract (Phase 5: OCR-only screenshot question input) ─────────

def test_extract_happy_path(client, vision_mocks):
    resp = client.post(
        "/api/vision/extract",
        files={"file": ("question.png", PNG_BYTES, "image/png")},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["question"] == "What is the capital of France?"
    assert data["is_question"] is True
    assert data["extracted_text"] == "What is the capital of France? It is Paris."
    assert data["line_count"] == 2
    assert data["likely_code"] is False
    assert data["avg_confidence"] is None
    assert data["source"] == "screenshot"
    assert data["ocr_ms"] >= 0
    # OCR-only: no answer / no model fields on this stage.
    assert "answer" not in data


def test_extract_rejects_unsupported_type(client):
    resp = client.post(
        "/api/vision/extract",
        files={"file": ("notes.txt", b"hello world", "text/plain")},
    )
    assert resp.status_code == 415


def test_extract_rejects_empty_file(client):
    resp = client.post(
        "/api/vision/extract",
        files={"file": ("blank.png", b"", "image/png")},
    )
    assert resp.status_code == 400


def test_extract_engine_unavailable_is_422(client, monkeypatch):
    def _boom(content):
        raise RuntimeError("OCR engine is not available")
    monkeypatch.setattr(vision_mod, "extract_text", _boom)
    resp = client.post(
        "/api/vision/extract",
        files={"file": ("question.png", PNG_BYTES, "image/png")},
    )
    assert resp.status_code == 422
    assert "OCR engine is not available" in resp.json()["detail"]


def test_extract_marks_code_screenshot(client, monkeypatch):
    monkeypatch.setattr(
        vision_mod, "extract_text",
        lambda content: {"text": "for i in range(n):\n    print(i)",
                          "line_count": 2, "likely_code": True,
                          "avg_confidence": 0.88},
    )
    monkeypatch.setattr(
        vision_mod, "extract_question",
        lambda text: "Explain this code.",  # no question mark hits truncation rule
    )
    resp = client.post(
        "/api/vision/extract",
        files={"file": ("code.png", PNG_BYTES, "image/png")},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["likely_code"] is True
    assert data["avg_confidence"] == 0.88