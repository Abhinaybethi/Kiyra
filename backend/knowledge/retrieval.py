"""Semantic context retrieval from the knowledge base.

RAG is a *fast path*, never a blocker: on any failure it returns empty context
(and the answer path simply runs without RAG). Retrieval timing is captured so
the real-time pipeline can report honest per-stage latency.

Enhancements (this session):
  * Question-type-aware top_k / context_chars routing (coding pulls more
    chunks for code examples; conceptual stays lean).
  * Hybrid scoring: cosine similarity + keyword overlap bonus, so exact-term
    matches (e.g. "Helmet", "body-parser") surface even when embedding
    similarity is middling.
  * Re-ranking: after hybrid scoring, results are re-ranked so the exact
    question match (if any) comes first.
  * Per-type min_score floors: coding/debugging tolerate lower similarity
    because code snippets embed differently from natural language.
  * Full original content: after finding relevant chunks via ChromaDB, the
    complete original document content is fetched from SQL (KnowledgeDocument)
    instead of returning fragmented chunk text. This gives the model the full
    Q&A answer rather than a truncated chunk snippet.
"""
from __future__ import annotations

import re
import time
from typing import Optional

from config import settings
from knowledge.embeddings import embed_text
from knowledge.vectorstore import query_collection

# Keyword bonus weight added to cosine similarity for exact term matches.
_KEYWORD_BONUS = 0.08
# Per-type similarity floors — code snippets embed sparser than prose.
_MIN_SCORE_BY_TYPE = {
    "conceptual": 0.25,
    "coding": 0.18,
    "behavioral": 0.25,
    "debugging": 0.20,
    "sql": 0.22,
    "default": 0.25,
}
# Question-type → source_type preference (None = no preference).
_SOURCE_PREF_BY_TYPE = {
    "coding": "qa_pair",
    "debugging": "qa_pair",
    "sql": "qa_pair",
    "behavioral": None,
    "conceptual": None,
}


def _keyword_overlap(query: str, content: str) -> float:
    """Fraction of query content-words that appear in the chunk (0..1)."""
    qwords = set(re.findall(r"[a-z0-9]+", (query or "").lower()))
    cwords = set(re.findall(r"[a-z0-9]+", (content or "").lower()))
    if not qwords:
        return 0.0
    return len(qwords & cwords) / len(qwords)


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


def _fetch_full_documents(
    db,
    profile_id: int,
    document_ids: list[int],
) -> dict[int, dict]:
    """Fetch full original document content from SQL for the given document IDs.

    Returns {document_id: {"content": ..., "source_type": ..., "title": ...}}.
    Never raises — returns {} on any failure.
    """
    if db is None:
        return {}
    try:
        from db.models import KnowledgeDocument
        rows = (
            db.query(KnowledgeDocument)
            .filter(
                KnowledgeDocument.profile_id == profile_id,
                KnowledgeDocument.id.in_(document_ids),
            )
            .all()
        )
        out: dict[int, dict] = {}
        for row in rows:
            out[row.id] = {
                "content": row.content or "",
                "source_type": row.source_type or "unknown",
                "title": row.title or "",
            }
        return out
    except Exception:
        return {}


def retrieve_context_sync(
    profile_id: Optional[int],
    query: str,
    top_k: Optional[int] = None,
    min_score: float = 0.25,
    max_context_chars: int = None,
    qtype: str = "conceptual",
    db=None,
) -> tuple[str, dict]:
    """Retrieve relevant context with question-type-aware routing.

    Returns (formatted_context, metrics). Never raises — retrieval failure is
    non-fatal: empty context + error metric.

    When ``db`` is provided, the full original document content is fetched from
    SQL for every matched chunk (via its ``document_id`` metadata), so the
    model receives complete Q&A answers instead of fragmented chunk snippets.
    """
    metrics = {
        "top_k": top_k or settings.realtime_top_k,
        "embed_ms": 0.0,
        "search_ms": 0.0,
        "rerank_ms": 0.0,
        "db_fetch_ms": 0.0,
        "chunks": 0,
        "error": None,
        "qtype": qtype,
    }
    if not profile_id or not query:
        return "", metrics

    # Per-type overrides.
    eff_top_k = top_k or settings.realtime_top_k_by_type.get(
        qtype, settings.realtime_top_k)
    eff_min_score = _MIN_SCORE_BY_TYPE.get(
        qtype, _MIN_SCORE_BY_TYPE["default"])
    eff_cap = max_context_chars or settings.realtime_max_context_chars_by_type.get(
        qtype, settings.realtime_max_context_chars)

    try:
        emb_start = time.monotonic()
        query_embedding = embed_text(query)
        metrics["embed_ms"] = round((time.monotonic() - emb_start) * 1000)

        search_start = time.monotonic()
        # Fetch extra candidates so keyword re-ranking has room to work.
        fetch_k = min(eff_top_k * 3, 24)
        results = query_collection(
            profile_id, query_embedding, top_k=fetch_k)
        metrics["search_ms"] = round((time.monotonic() - search_start) * 1000)

        # Hybrid scoring: cosine similarity + keyword overlap bonus.
        rerank_start = time.monotonic()
        for r in results:
            overlap = _keyword_overlap(query, r.get("content", ""))
            r["hybrid_score"] = r["score"] + (_KEYWORD_BONUS * overlap)
            r["keyword_overlap"] = overlap
        # Re-rank by hybrid score, but promote exact question matches first.
        results.sort(key=lambda r: (-r["hybrid_score"], -r["score"]))
        metrics["rerank_ms"] = round((time.monotonic() - rerank_start) * 1000)

        relevant = _dedupe([r for r in results
                            if r["hybrid_score"] >= eff_min_score])
        relevant = relevant[:eff_top_k]
        metrics["chunks"] = len(relevant)
        if not relevant:
            return "", metrics

        # ── Fetch full original document content from SQL ─────────────────
        # Each chunk's metadata carries ``document_id`` (set at ingestion time
        # in vectorstore.add_chunks). We look up the parent KnowledgeDocument
        # and return its complete content instead of the truncated chunk.
        full_docs: dict[int, dict] = {}
        doc_ids: list[int] = []
        for r in relevant:
            did = r.get("metadata", {}).get("document_id")
            if did is not None and did not in doc_ids:
                doc_ids.append(int(did))

        db_fetch_start = time.monotonic()
        if db is not None and doc_ids:
            full_docs = _fetch_full_documents(db, profile_id, doc_ids)
        metrics["db_fetch_ms"] = round((time.monotonic() - db_fetch_start) * 1000)

        parts: list[str] = []
        used = 0
        seen_titles: set[str] = set()
        for r in relevant:
            meta = r.get("metadata", {}) or {}
            did = meta.get("document_id")
            source = meta.get("source_type", "unknown")

            if did is not None and int(did) in full_docs:
                # Use the FULL original document content.
                doc = full_docs[int(did)]
                block_content = doc["content"]
                block_source = doc["source_type"]
                block_title = doc.get("title") or ""
            else:
                # Fall back to the chunk content if no DB or doc not found.
                block_content = r.get("content", "")
                block_source = source
                block_title = ""

            # Avoid duplicating the same document if multiple chunks matched.
            dedupe_key = f"{block_source}:{block_title}:{block_content[:120]}"
            if dedupe_key in seen_titles:
                continue
            seen_titles.add(dedupe_key)

            title_prefix = f" [{block_title}]" if block_title else ""
            block = f"[{block_source.upper()}{title_prefix}]: {block_content}"
            if used + len(block) > eff_cap and parts:
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
    min_score: float = 0.25,
    qtype: str = "conceptual",
    db=None,
) -> str:
    """Async retrieval keeping the old call contract (plain string)."""
    context, _metrics = await __import__("asyncio").to_thread(
        retrieve_context_sync,
        profile_id,
        query,
        top_k=top_k,
        min_score=min_score,
        max_context_chars=None,
        qtype=qtype,
        db=db,
    )
    return context
