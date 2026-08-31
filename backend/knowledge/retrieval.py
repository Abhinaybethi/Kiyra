"""Semantic context retrieval from the knowledge base."""
from __future__ import annotations

from typing import Optional

from knowledge.embeddings import embed_text
from knowledge.vectorstore import query_collection


async def retrieve_context(
    profile_id: Optional[int],
    query: str,
    top_k: int = 3,
    min_score: float = 0.3,
) -> str:
    """Retrieve relevant context chunks for a query. Returns formatted string."""
    if not profile_id or not query:
        return ""

    try:
        query_embedding = embed_text(query)
        results = query_collection(profile_id, query_embedding, top_k=top_k)

        relevant = [r for r in results if r["score"] >= min_score]
        if not relevant:
            return ""

        parts = []
        for r in relevant:
            source = r["metadata"].get("source_type", "unknown")
            parts.append(f"[{source.upper()}]: {r['content']}")

        return "\n\n".join(parts)
    except Exception:
        return ""  # retrieval failure is non-fatal
