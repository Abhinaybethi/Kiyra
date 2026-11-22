"""Hybrid RAG retrieval — dense + lexical, fused with RRF, answered from the
ORIGINAL document text.

RAG is a *fast path*, never a blocker: on any failure it returns empty context
(and the answer path simply runs without RAG). Retrieval timing is captured so
the real-time pipeline can report honest per-stage latency.

How this layer works (and why):

  * HYBRID SEARCH. Dense embeddings answer "what is this about?" but are weak
    on exact names ("Helmet", "body-parser", "Kiyra"). BM25 answers "does this
    text literally contain these words?". Both rankings are produced and
    fused with Reciprocal Rank Fusion (RRF), so a document that is
    semantically close OR literally contains the query term can win.

  * ENTITY-AWARE QUERY EXPANSION. A follow-up ("what was the hardest problem
    in it?") carries no searchable terms of its own. The resolved session topic
    and the candidate's own project/skill names are folded into the query, so
    a pronoun question still finds the right document.

  * ORIGINAL TEXT, NOT TRUNCATED CHUNKS. The final context is cut from the
    document's REAL text (``KnowledgeDocument.content``), never from a chunk
    fragment. A long document (a resume) is split into sentence-aligned
    passages, the relevant ones are selected, and they are re-assembled
    VERBATIM and IN DOCUMENT ORDER — the model reads the candidate's real
    sentences, with " … " marking what was skipped.

  * MMR DIVERSIFICATION. Selected passages are picked to be relevant but not
    near-duplicates, so the context carries several distinct facts instead of
    three paraphrases of the same one.

  * MOST-RELEVANT-LAST. The context block is ordered so the strongest passage
    sits closest to the question (the model reads the question last), which is
    where a small local model attends most reliably.
"""
from __future__ import annotations

import re
import time
from typing import Optional

from config import settings
from knowledge.embeddings import embed_text
from knowledge.lexical import BM25Index, normalize, tokenize
from knowledge.passages import (
    Passage,
    assemble_passages,
    passage_terms,
    split_passages,
)
from knowledge.vectorstore import query_collection

# RRF damping constant. 60 is the value from the original RRF paper and is
# deliberately large: it flattens the difference between rank 1 and rank 10
# so one strong lexical hit is not drowned out by a dense ranking.
_RRF_K = 60

# A lexical candidate must reach this fraction of the best BM25 score to
# count as a real match on its own (i.e. without a decent cosine score).
_LEX_MIN_NORM = 0.45

# Keyword bonus weight added to cosine similarity for exact term matches
# (kept for the per-candidate score; the ranking itself uses RRF).
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

# Words that signal the question refers back to something ("what about it?").
_ANAPHORA = {
    "it", "its", "that", "this", "they", "them", "those", "these", "he",
    "she", "same", "above", "previous", "earlier", "previously", "before",
}

# "Tell me about yourself" and friends. These questions contain almost no
# searchable content ("yourself" is the only non-stop-word), so similarity
# search returns nothing — but the candidate's own resume is exactly the
# right context. The intro fallback below supplies it.
_INTRO_RE = re.compile(
    r"\b(tell me about yourself|introduce yourself|introduce yourself|"
    r"walk me through your|about your (resume|background|experience|skills|"
    r"profile|projects?)|your (resume|background|profile)|who are you|"
    r"about yourself|tell me about you)\b",
    re.I,
)


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


def _chunk_key(meta: dict) -> tuple:
    """A stable key for a chunk across the dense and lexical rankings.

    The dense side returns Chroma metadata (``document_id`` + ``chunk_index``);
    the lexical side reads SQL rows that carry the same pair, so the two
    rankings can be fused on a common key.
    """
    meta = meta or {}
    did = meta.get("document_id")
    idx = meta.get("chunk_index")
    try:
        did = int(did)
    except (TypeError, ValueError):
        did = -1
    try:
        idx = int(idx)
    except (TypeError, ValueError):
        idx = -1
    return (did, idx)


def _expand_query(query: str, topic: str = "", db=None,
                  profile_id: Optional[int] = None) -> tuple[str, list[str]]:
    """Expand the query with the entities it refers to.

    Returns ``(expanded_query, expansion_terms)``.

    * A referential question ("what was the hardest problem in it?") is
      anchored to the session's resolved topic — without it the query has no
      searchable content at all.
    * The candidate's own project and skill names are added when the question
      mentions them, so "how did you build Kiyra" also matches documents that
      only mention "Kiyra" in a body of text.
    """
    query = (query or "").strip()
    expanded = query
    terms: list[str] = []

    q_lower = " ".join(tokenize(query))
    # The anaphora check runs on the RAW words: "it" / "that" / "this" are
    # stop-words, so they never survive tokenisation.
    raw_words = set(re.findall(r"[a-z]+", query.lower()))
    if topic and topic.strip():
        topic_terms = tokenize(topic)
        if topic_terms and raw_words & _ANAPHORA:
            expanded = f"{query} {topic.strip()}"
            terms.extend(topic_terms)

    if db is not None and profile_id:
        entities = _profile_entities(db, profile_id)
        for name, aliases in entities:
            low = name.lower()
            if low and low in query.lower():
                extra = [a for a in aliases if a and a.lower() not in q_lower]
                if extra:
                    expanded = f"{expanded} {' '.join(extra)}".strip()
                    terms.extend(extra)
    return expanded, terms


def _profile_entities(db, profile_id: int) -> list[tuple[str, list[str]]]:
    """The candidate's project / skill names with their aliases.

    Never raises — an empty list simply disables entity expansion.
    """
    out: list[tuple[str, list[str]]] = []
    try:
        from db.models import Project, Skill

        for p in db.query(Project).filter_by(profile_id=profile_id).all():
            aliases: list[str] = [p.name or ""]
            tech = p.technologies or ""
            try:
                import json
                if isinstance(tech, str):
                    tech_list = json.loads(tech)
                else:
                    tech_list = list(tech or [])
            except Exception:
                tech_list = []
            aliases.extend(str(t) for t in tech_list)
            out.append((p.name or "", [a for a in aliases if a]))

        for s in db.query(Skill).filter_by(profile_id=profile_id).all():
            out.append((s.name or "", [s.name or ""]))
    except Exception:
        return out
    return out


def _lexical_candidates(db, profile_id: int, query: str,
                         limit: int = 24) -> tuple[list[dict], int]:
    """BM25 rank over the profile's chunks.

    Returns ``(hits, corpus_size)`` where each hit is
    ``{key, document_id, chunk_index, content, score}``.
    """
    if db is None:
        return [], 0
    try:
        from db.models import KnowledgeChunk, KnowledgeDocument

        rows = (
            db.query(KnowledgeChunk, KnowledgeDocument)
            .join(KnowledgeDocument,
                  KnowledgeDocument.id == KnowledgeChunk.document_id)
            .filter(KnowledgeDocument.profile_id == profile_id)
            .all()
        )
        if not rows:
            return [], 0
        corpus = []
        info = {}
        for chunk, doc in rows:
            key = (doc.id, chunk.chunk_index)
            corpus.append((key, f"{doc.title or ''}\n{chunk.content or ''}"))
            info[key] = {
                "key": key,
                "document_id": doc.id,
                "chunk_index": chunk.chunk_index,
                "content": chunk.content or "",
                "title": doc.title or "",
                "source_type": doc.source_type or "unknown",
            }
        index = BM25Index(corpus)
        scored = index.score(query)
        norms = normalize(scored)
        hits = []
        for key, score in scored[:limit]:
            hit = dict(info.get(key, {}))
            if not hit:
                continue
            hit["score"] = norms.get(key, 0.0)
            hit["bm25"] = score
            hits.append(hit)
        return hits, len(corpus)
    except Exception:
        return [], 0


def _fuse(dense: list[dict], lexical: list[dict],
          rrf_k: int = _RRF_K) -> list[dict]:
    """Reciprocal Rank Fusion of the dense and lexical rankings.

    Each list contributes ``1/(k + rank)`` to a candidate's score, so a
    document that both rankings like beats a document only one of them does.
    """
    fused: dict[tuple, dict] = {}
    order: list[tuple] = []

    def _bump(rank: int, key: tuple, payload: dict, source: str):
        entry = fused.get(key)
        if entry is None:
            entry = {
                "key": key,
                "document_id": key[0],
                "chunk_index": key[1],
                "content": "",
                "dense_score": 0.0,
                "lexical_score": 0.0,
                "keyword_overlap": 0.0,
                "rrf": 0.0,
                "sources": [],
            }
            fused[key] = entry
            order.append(key)
        entry["rrf"] += 1.0 / (rrf_k + rank + 1)
        if source not in entry["sources"]:
            entry["sources"].append(source)
        if payload:
            for field in ("content", "dense_score", "lexical_score",
                          "keyword_overlap", "title", "source_type"):
                value = payload.get(field)
                if value not in (None, "", 0.0):
                    entry[field] = value

    for rank, r in enumerate(dense):
        key = _chunk_key(r.get("metadata"))
        _bump(rank, key, {
            "content": r.get("content", ""),
            "dense_score": r.get("score", 0.0),
        }, "dense")
    for rank, r in enumerate(lexical):
        key = (r.get("document_id", -1), r.get("chunk_index", -1))
        _bump(rank, key, {
            "content": r.get("content", ""),
            "lexical_score": r.get("score", 0.0),
            "title": r.get("title", ""),
            "source_type": r.get("source_type", ""),
        }, "lexical")

    out = [fused[k] for k in order]
    out.sort(key=lambda e: -e["rrf"])
    return out


def _passage_relevance(passages: list[Passage], query: str,
                        anchors: list[dict]) -> list[float]:
    """Score each passage of a document against the query.

    Two signals are combined:
      * BM25 of the passage within its own document (how well the passage
        itself matches the question), and
      * an ANCHOR bonus for passages that overlap a chunk the dense retriever
        already liked (the embedding found the neighbourhood; the passage
        says it in the document's own words).
    """
    if not passages:
        return []
    index = BM25Index([(p.index, p.text) for p in passages])
    scored = index.score(query)
    norms = normalize(scored)
    relevance = [0.0] * len(passages)

    for pidx, score in norms.items():
        if pidx < len(relevance):
            relevance[pidx] = score

    anchor_terms = [passage_terms(a.get("content", "")) for a in anchors]
    for i, p in enumerate(passages):
        terms = passage_terms(p.text)
        if not terms:
            continue
        best = 0.0
        for at in anchor_terms:
            if not at:
                continue
            overlap = len(terms & at) / max(1, min(len(terms), len(at)))
            best = max(best, overlap)
        if best > 0:
            relevance[i] = min(1.0, relevance[i] + 0.6 * best)
    return relevance


def _mmr_select(relevance: list[float], passages: list[Passage],
                k: int, lam: float = 0.7) -> list[int]:
    """Maximal Marginal Relevance selection.

    Picks passages that are relevant but not near-duplicates of each other,
    so the context carries several distinct facts.
    """
    n = len(passages)
    if k <= 0 or n == 0:
        return []
    term_sets = [passage_terms(p.text) for p in passages]
    selected: list[int] = []
    remaining = list(range(n))
    while remaining and len(selected) < k:
        best_idx = None
        best_val = -1e9
        for i in remaining:
            redundancy = 0.0
            for j in selected:
                a, b = term_sets[i], term_sets[j]
                if not a or not b:
                    continue
                union = len(a | b)
                if union:
                    redundancy = max(redundancy, len(a & b) / union)
            val = lam * relevance[i] - (1.0 - lam) * redundancy
            if val > best_val:
                best_val = val
                best_idx = i
        if best_idx is None:
            break
        selected.append(best_idx)
        remaining.remove(best_idx)
    return selected


def _select_excerpt(doc: dict, query: str, anchors: list[dict],
                    passages_per_doc: int, mmr_lambda: float,
                    prefer_start: bool = False) -> tuple[str, int]:
    """Cut the document's REAL text down to the passages that matter.

    Short documents are returned whole (there is nothing to select, and the
    complete Q&A answer is what the model needs). Long documents get
    sentence-aligned passage selection, re-assembled verbatim in document
    order.

    ``prefer_start`` keeps the opening passages instead of scoring them: it
    is used for an intro question, where the query has no terms to score
    against and the head of a resume (name, objective, skills) is what an
    introduction is made of.

    Returns ``(excerpt, passage_count)``.
    """
    content = doc.get("content") or ""
    if not content.strip():
        return "", 0
    short_limit = getattr(settings, "rag_short_doc_chars", 1200)
    if len(content) <= short_limit:
        return content, 0

    passages = split_passages(
        content,
        max_chars=getattr(settings, "rag_passage_max_chars", 320),
        min_chars=getattr(settings, "rag_passage_min_chars", 40),
    )
    if not passages:
        return content, 0
    if prefer_start and not anchors:
        selected = list(range(min(passages_per_doc, len(passages))))
    else:
        relevance = _passage_relevance(passages, query, anchors)
        selected = _mmr_select(relevance, passages, passages_per_doc,
                               mmr_lambda)
    if not selected:
        return content, 0
    excerpt = assemble_passages(content, passages, selected)
    return excerpt, len(selected)


def _personal_intro_documents(db, profile_id: int) -> list[dict]:
    """The candidate's own documents — the context for an intro question.

    "Tell me about yourself" has no searchable terms, so similarity search
    returns nothing; the resume is exactly what the answer needs.
    """
    if db is None:
        return []
    try:
        from db.models import KnowledgeDocument
        rows = (
            db.query(KnowledgeDocument)
            .filter(
                KnowledgeDocument.profile_id == profile_id,
                KnowledgeDocument.source_type.in_(("resume", "project")),
            )
            .all()
        )
        return [
            {
                "id": r.id,
                "content": r.content or "",
                "source_type": r.source_type or "unknown",
                "title": r.title or "",
            }
            for r in rows
            if (r.content or "").strip()
        ]
    except Exception:
        return []


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
    topic: str = "",
) -> tuple[str, dict]:
    """Retrieve relevant context with question-type-aware routing.

    Returns (formatted_context, metrics). Never raises — retrieval failure is
    non-fatal: empty context + error metric.

    When ``db`` is provided, the full original document content is fetched from
    SQL for every matched chunk (via its ``document_id`` metadata), so the
    model receives the candidate's real text instead of fragmented chunk
    snippets. Long documents are narrowed to their relevant passages, still
    verbatim and still in document order.
    """
    metrics = {
        "top_k": top_k or settings.realtime_top_k,
        "embed_ms": 0.0,
        "search_ms": 0.0,
        "rerank_ms": 0.0,
        "db_fetch_ms": 0.0,
        "passage_ms": 0.0,
        "chunks": 0,
        "documents": 0,
        "passages": 0,
        "dense_hits": 0,
        "lexical_hits": 0,
        "corpus_chunks": 0,
        "expansion_terms": 0,
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
        # ── 1. Expand the query with the entities it refers to ──────────────
        expanded, expansion_terms = _expand_query(query, topic, db, profile_id)
        metrics["expansion_terms"] = len(expansion_terms)

        emb_start = time.monotonic()
        query_embedding = embed_text(expanded)
        metrics["embed_ms"] = round((time.monotonic() - emb_start) * 1000)

        # ── 2. Dense retrieval ───────────────────────────────────────────────
        search_start = time.monotonic()
        fetch_k = min(eff_top_k * 3, 24)
        dense = query_collection(profile_id, query_embedding, top_k=fetch_k)
        metrics["search_ms"] = round((time.monotonic() - search_start) * 1000)
        metrics["dense_hits"] = len(dense)

        # ── 3. Lexical retrieval (BM25) ─────────────────────────────────────
        lex_start = time.monotonic()
        lexical, corpus_size = _lexical_candidates(db, profile_id, expanded,
                                                   limit=fetch_k)
        metrics["lexical_hits"] = len(lexical)
        metrics["corpus_chunks"] = corpus_size

        # ── 4. Fuse the two rankings with RRF ───────────────────────────────
        rerank_start = time.monotonic()
        for r in dense:
            overlap = _keyword_overlap(expanded, r.get("content", ""))
            r["hybrid_score"] = r.get("score", 0.0) + (_KEYWORD_BONUS * overlap)
            r["keyword_overlap"] = overlap
        fused = _fuse(dense, lexical)

        # Keep a candidate when it is a decent semantic match OR a strong
        # exact-term match (a low cosine with a high BM25 is exactly the
        # case the old dense-only layer threw away).
        relevant = []
        for entry in fused:
            dense_ok = entry["dense_score"] >= eff_min_score
            lex_ok = entry["lexical_score"] >= _LEX_MIN_NORM
            if dense_ok or lex_ok:
                relevant.append(entry)
        metrics["rerank_ms"] = round((time.monotonic() - rerank_start) * 1000)
        metrics["chunks"] = len(relevant)

        # An intro question ("tell me about yourself") has no searchable
        # terms, so nothing clears the score floor — but the candidate's own
        # documents are exactly the right context. Keep going for it.
        is_intro = bool(_INTRO_RE.search(query or ""))
        if not relevant and not is_intro:
            return "", metrics

        # ── 5. Group by document and keep the best documents ────────────────
        by_doc: dict[int, list[dict]] = {}
        for entry in relevant:
            did = entry.get("document_id")
            if did is None or did < 0:
                continue
            by_doc.setdefault(int(did), []).append(entry)
        if not by_doc and not is_intro:
            return "", metrics

        doc_rank = []
        for did, entries in by_doc.items():
            best = max(entries, key=lambda e: e["rrf"])
            # A document that matched on several independent chunks is a
            # stronger signal than one that matched a single chunk twice.
            score = best["rrf"] + 0.05 * (len(entries) - 1)
            doc_rank.append((did, score, entries))
        doc_rank.sort(key=lambda item: -item[1])
        selected_docs = doc_rank[:eff_top_k]

        # ── 5b. Personal-intro fallback ─────────────────────────────────────
        # "Tell me about yourself" carries no searchable terms, so similarity
        # search finds nothing and the candidate gets a generic answer with no
        # grounding at all. When the question is an intro question, pull in
        # the candidate's own documents (resume / projects) explicitly.
        intro_docs: set[int] = set()
        if is_intro:
            have = {did for did, _, _ in selected_docs}
            for doc in _personal_intro_documents(db, profile_id):
                if doc["id"] in have:
                    continue
                # Scored above everything else: the intro context IS the
                # answer's grounding, so it must sit closest to the question.
                selected_docs.append((doc["id"], 10.0, []))
                have.add(doc["id"])
                intro_docs.add(doc["id"])

        metrics["documents"] = len(selected_docs)

        # ── 6. Fetch the TRUE original document text ────────────────────────
        db_fetch_start = time.monotonic()
        full_docs = _fetch_full_documents(
            db, profile_id, [did for did, _, _ in selected_docs])
        metrics["db_fetch_ms"] = round((time.monotonic() - db_fetch_start) * 1000)

        # ── 7. Select passages and format the blocks ────────────────────────
        passage_start = time.monotonic()
        passages_per_doc = getattr(settings, "rag_passages_per_doc", 3)
        mmr_lambda = getattr(settings, "rag_mmr_lambda", 0.7)
        blocks: list[tuple[float, str]] = []
        total_passages = 0
        for did, score, entries in selected_docs:
            doc = full_docs.get(did)
            if doc is None:
                # No original text available — fall back to the chunk text
                # rather than dropping the hit entirely.
                best = entries[0]
                block_content = best.get("content", "")
                block_source = best.get("source_type", "unknown")
                block_title = best.get("title", "")
                n_pass = 0
            else:
                block_content, n_pass = _select_excerpt(
                    doc, expanded, entries,
                    passages_per_doc * 2 if did in intro_docs else passages_per_doc,
                    mmr_lambda,
                    prefer_start=(did in intro_docs),
                )
                block_source = doc["source_type"]
                block_title = doc.get("title") or ""
            total_passages += n_pass
            if not block_content.strip():
                continue
            title_prefix = f" [{block_title}]" if block_title else ""
            block = f"[{block_source.upper()}{title_prefix}]: {block_content}"
            blocks.append((score, block))

        metrics["passage_ms"] = round((time.monotonic() - passage_start) * 1000)
        metrics["passages"] = total_passages

        # ── 8. Order: least relevant first, strongest LAST (nearest the
        #       question, where a small local model attends best) ───────────
        blocks.sort(key=lambda item: item[0])

        parts: list[str] = []
        used = 0
        for _score, block in blocks:
            if used and used + len(block) > eff_cap:
                continue
            parts.append(block)
            used += len(block)
            if used >= eff_cap:
                break
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
    topic: str = "",
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
        topic=topic,
    )
    return context
