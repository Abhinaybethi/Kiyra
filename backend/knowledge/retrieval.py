"""Semantic context retrieval from the knowledge base.

RAG is a *fast path*, never a blocker: on any failure it returns empty context
(and the answer path simply runs without RAG). Retrieval timing is captured so
the real-time pipeline can report honest per-stage latency.
"""
from __future__ import annotations

import time
from typing import Optional

from config import settings
from knowledge.embeddings import embed_text
from knowledge.vectorstore import query_collection


def _dedupe(results: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out = []
    for r in results:
        key = (r.get("content") or "").strip().lower()[:240]
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def retrieve_context_sync(
    profile_id: Optional[int],
    query: str,
    top_k: Optional[int] = None,
    min_score: float = 0.3,
    max_context_chars: int = None,
) -> tuple[str, dict]:
    """Retrieve relevant context chunks. Returns (formatted_context, metrics).

    Never raises — retrieval failure is non-fatal: empty context + error metric.
    """
    metrics = {
        "top_k": top_k or settings.rag_top_k,
        "embed_ms": 0.0,
        "search_ms": 0.0,
        "chunks": 0,
        "error": None,
    }
    if not profile_id or not query:
        return "", metrics

    try:
        emb_start = time.monotonic()
        query_embedding = embed_text(query)
        metrics["embed_ms"] = round((time.monotonic() - emb_start) * 1000)

        search_start = time.monotonic()
        results = query_collection(
            profile_id, query_embedding, top_k=metrics["top_k"]
        )
        metrics["search_ms"] = round((time.monotonic() - search_start) * 1000)

        relevant = _dedupe([r for r in results if r["score"] >= min_score])
        metrics["chunks"] = len(relevant)
        if not relevant:
            return "", metrics

        cap = max_context_chars or settings.rag_max_context_chars
        parts: list[str] = []
        used = 0
        for r in relevant:
            source = r["metadata"].get("source_type", "unknown")
            block = f"[{source.upper()}]: {r['content']}"
            if used + len(block) > cap and parts:
                break
            parts.append(block)
            used += len(block)

        return "\n\n".join(parts), metrics
    except Exception as e:
        metrics["error"] = str(e)
        return "", metrics


async def retrieve_context(
    profile_id: Optional[int],
    query: str,
    top_k: int = 3,
    min_score: float = 0.3,
) -> str:
    """Async retrieval keeping the old call contract (plain string)."""
    context, _metrics = await __import__("asyncio").to_thread(
        retrieve_context_sync,
        profile_id,
        query,
        top_k=top_k,
        min_score=min_score,
    )
    return context