"""Tests for Knowledge Base RAG endpoints, Agent Orchestration, and Provider Resilience."""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from db.models import CandidateProfile, KnowledgeDocument, InterviewSession, QuestionType
from ai.provider import OllamaProvider, OpenAICompatibleProvider, get_provider


def test_knowledge_base_crud_and_search(client):
    # 1. Get stats
    stats_res = client.get("/api/knowledge/stats")
    assert stats_res.status_code == 200
    stats = stats_res.json()
    assert "total_documents" in stats
    assert "total_chunks" in stats

    # 2. Add Knowledge Item
    payload = {
        "title": "Scaling PostgreSQL for 10M DAU",
        "content": (
            "Situation: The primary database CPU spiked to 98% during marketing campaigns.\n"
            "Task: Resolve query bottlenecks without downtime.\n"
            "Action: Implemented table partitioning by date, added read replicas, and cached user sessions in Redis.\n"
            "Result: Database CPU dropped to 35% and throughput doubled."
        ),
        "source_type": "star_story",
        "metadata": {"tags": ["database", "postgresql", "redis"]},
    }

    create_res = client.post("/api/knowledge", json=payload)
    assert create_res.status_code == 200
    created_doc = create_res.json()
    assert created_doc["title"] == "Scaling PostgreSQL for 10M DAU"
    assert created_doc["chunk_count"] >= 1
    doc_id = created_doc["id"]

    # 3. List documents
    list_res = client.get("/api/knowledge")
    assert list_res.status_code == 200
    docs = list_res.json()
    assert len(docs) >= 1
    assert any(d["id"] == doc_id for d in docs)

    # 4. Search Simulator
    search_payload = {
        "query": "Tell me about resolving database bottlenecks",
        "top_k": 3,
        "min_score": 0.0,
    }
    with patch("api.knowledge.query_collection") as mock_query:
        mock_query.return_value = [
            {
                "content": "Implemented table partitioning by date, added read replicas",
                "score": 0.88,
                "metadata": {"source_type": "star_story", "title": "Scaling PostgreSQL for 10M DAU"},
            }
        ]
        search_res = client.post("/api/knowledge/search", json=search_payload)
        assert search_res.status_code == 200
        search_data = search_res.json()
        assert "results" in search_data
        assert len(search_data["results"]) == 1
        assert search_data["results"][0]["score"] == 0.88

    # 5. Delete document
    del_res = client.delete(f"/api/knowledge/{doc_id}")
    assert del_res.status_code == 200
    assert del_res.json()["ok"] is True


def test_ai_provider_capabilities():
    provider = get_provider("llama3.2:3b")
    caps = provider.get_capabilities()
    assert "context_window" in caps
    assert caps["speed_tier"] == "fast"
    assert caps["supports_json"] is True


def test_suggest_answer_endpoint(client):
    payload = {
        "question": "Tell me about a time you handled a difficult technical challenge.",
        "question_type": "behavioral",
    }
    with patch("agents.answer_agent.AnswerAgent.run", new_callable=AsyncMock) as mock_run:
        mock_run.return_value.success = True
        mock_run.return_value.model_used = "mock-model"
        mock_run.return_value.data = {
            "answer": "At my previous company, I resolved a critical memory leak in our distributed ingestion pipeline.",
            "key_points": ["Identified memory leak", "Used profiling tools", "Deployed fix safely"],
            "star": {
                "situation": "Memory leak causing hourly OOM crashes",
                "task": "Fix root cause within 24 hours",
                "action": "Profiled heap allocation with pprof and fixed unclosed channels",
                "result": "Zero crashes and 40% lower memory footprint",
            },
            "confidence": 0.95,
        }
        res = client.post("/api/interviews/suggest-answer", json=payload)
        assert res.status_code == 200
        data = res.json()
        assert "answer" in data
        assert "key_points" in data
        assert len(data["key_points"]) == 3
