"""Hybrid RAG layer — dense+lexical fusion, passage-level selection from the
ORIGINAL document text, MMR diversification and entity-aware expansion.

The bugs these tests lock down:
  * RAG used to answer from a CHUNK FRAGMENT, so a document's real text was
    never shown to the model (and a document whose stored content had been
    truncated stayed truncated).
  * Dense-only retrieval missed exact-name matches ("Helmet", "body-parser").
  * A follow-up question with no content words ("what about it?") retrieved
    nothing at all.
  * Long documents were pasted whole, burying the relevant part.
"""
from __future__ import annotations

import pytest

from knowledge import retrieval as retrieval_mod
from knowledge.chunker import chunk_document, chunk_text
from knowledge.lexical import BM25Index, tokenize
from knowledge.passages import assemble_passages, passage_terms, split_passages


# ── helpers ─────────────────────────────────────────────────────────────────

def _make_profile(db):
    from db.models import CandidateProfile
    profile = db.query(CandidateProfile).first()
    if profile is None:
        profile = CandidateProfile(user_id=1, name="Tester",
                                   target_role="Software Engineer")
        db.add(profile)
        db.commit()
        db.refresh(profile)
    return profile


def _make_doc(db, profile, title, content, source_type="manual"):
    from db.models import KnowledgeDocument
    doc = KnowledgeDocument(profile_id=profile.id, source_type=source_type,
                            title=title, content=content)
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return doc


def _make_chunk(db, doc, content, index):
    from db.models import KnowledgeChunk
    chunk = KnowledgeChunk(document_id=doc.id, content=content,
                            chunk_index=index, meta_data={})
    db.add(chunk)
    db.commit()
    return chunk


# ── sentence-aware ingestion ────────────────────────────────────────────────

def test_chunker_never_cuts_a_sentence_in_half():
    text = ("I built the ingestion pipeline. It parses resumes in under a "
            "second. The parser handles PDF and DOCX files. "
            "The retriever then ranks the passages.")
    chunks = list(chunk_text(text, chunk_size=12, overlap=4))
    assert len(chunks) > 1
    for chunk in chunks:
        # Every chunk must start at a sentence start, not mid-sentence.
        first_word = chunk.split()[0]
        assert first_word in {
            "I", "It", "The", "The", "The", "The", "The", "The",
        } or chunk.startswith("I built") or chunk.startswith("It parses")


def test_chunker_respects_the_word_budget():
    text = " ".join(f"Sentence number {i} has some words in it." for i in range(80))
    for chunk in chunk_text(text, chunk_size=40, overlap=5):
        assert len(chunk.split()) <= 40


def test_chunker_keeps_the_original_line_structure():
    text = "BETHI ABHINAY\nSOFTWARE ENGINEER\n\nOBJECTIVE\nBuild reliable apps."
    chunks = chunk_document(text, metadata={"source_type": "resume"})
    assert chunks
    assert any("OBJECTIVE" in c["content"] for c in chunks)
    assert chunks[0]["chunk_index"] == 0
    assert chunks[0]["metadata"]["source_type"] == "resume"


def test_oversized_sentence_is_windowed_not_dropped():
    text = " ".join(["word"] * 300)
    chunks = list(chunk_text(text, chunk_size=50, overlap=5))
    assert len(chunks) > 1
    assert all(len(c.split()) <= 50 for c in chunks)
    # Nothing is lost: every word is still present somewhere.
    joined = " ".join(chunks).split()
    assert len(joined) >= 300 - 5 * len(chunks)


# ── passages ────────────────────────────────────────────────────────────────

def test_passages_keep_the_original_wording_and_order():
    text = ("Alpha sentence one. Alpha sentence two. "
            "Beta sentence three. Beta sentence four.")
    passages = split_passages(text, max_chars=60)
    assert len(passages) >= 2
    for p in passages:
        assert text[p.start:p.end].strip() == p.text.strip()
    # Document order is preserved.
    assert passages[0].start < passages[1].start
    assert passages[0].text.startswith("Alpha")
    assert passages[-1].text.endswith("Beta sentence four.")


def test_assemble_passages_is_verbatim_and_marks_the_gap():
    text = "One. Two. Three. Four. Five."
    passages = split_passages(text, max_chars=8)
    assert len(passages) >= 3
    picked = [0, passages[-1].index]
    out = assemble_passages(text, passages, picked)
    assert out.startswith("One.")
    assert out.endswith("Five.")
    assert "…" in out, "a skipped middle must be marked"
    # Everything in the output exists in the original document.
    for piece in out.replace("…", " ").split():
        assert piece.rstrip(".") in text


def test_short_document_is_never_split():
    text = "Q1: What is Node.js? A: It is a JavaScript runtime."
    passages = split_passages(text, max_chars=320)
    assert len(passages) == 1


# ── lexical retrieval ───────────────────────────────────────────────────────

def test_bm25_finds_the_exact_term_document():
    corpus = [
        (("doc", 0), "A general talk about web servers and routing"),
        (("doc", 1), "The Helmet library secures Express middleware"),
        (("doc", 2), "Unrelated notes about databases and indexes"),
    ]
    index = BM25Index(corpus)
    scored = index.score("how does Helmet work")
    assert scored
    assert scored[0][0] == ("doc", 1)


def test_tokenizer_keeps_compound_technical_names():
    tokens = tokenize("Node.js and body-parser are used")
    assert "node.js" in tokens
    assert "body-parser" in tokens
    # …and the split parts, so "node" still matches "node.js".
    assert "node" in tokens
    assert "parser" in tokens


# ── fusion ──────────────────────────────────────────────────────────────────

def test_rrf_fusion_prefers_documents_both_rankings_like():
    dense = [
        {"content": "dense a", "score": 0.9,
         "metadata": {"document_id": 1, "chunk_index": 0}},
        {"content": "dense b", "score": 0.8,
         "metadata": {"document_id": 2, "chunk_index": 0}},
    ]
    lexical = [
        {"document_id": 2, "chunk_index": 0, "content": "lexical b",
         "score": 1.0, "title": "B", "source_type": "manual"},
    ]
    fused = retrieval_mod._fuse(dense, lexical)
    top = fused[0]
    assert top["document_id"] == 2, (
        "a document ranked by BOTH dense and lexical must win")
    assert set(top["sources"]) == {"dense", "lexical"}


def test_rrf_fusion_never_lets_a_low_rank_dense_hit_win_alone():
    dense = [{"content": "x", "score": 0.4,
              "metadata": {"document_id": 7, "chunk_index": 3}}]
    lexical = [
        {"document_id": 7, "chunk_index": 3, "content": "x", "score": 1.0,
         "title": "T", "source_type": "manual"},
    ]
    fused = retrieval_mod._fuse(dense, lexical)
    assert fused[0]["document_id"] == 7
    assert fused[0]["lexical_score"] > 0


# ── query expansion ─────────────────────────────────────────────────────────

def test_expand_query_anchors_a_pronoun_question_to_the_topic(db_session):
    db = db_session
    profile = _make_profile(db)
    expanded, terms = retrieval_mod._expand_query(
        "what was the hardest problem in it?", "Kiyra project", db, profile.id)
    assert "Kiyra" in expanded
    assert terms


def test_expand_query_leaves_a_self_contained_question_alone(db_session):
    db = db_session
    profile = _make_profile(db)
    expanded, terms = retrieval_mod._expand_query(
        "What is the difference between a list and a tuple?",
        "Kiyra project", db, profile.id)
    assert expanded == "What is the difference between a list and a tuple?"
    assert terms == []


def test_expand_query_adds_project_aliases(db_session):
    from db.models import Project
    db = db_session
    profile = _make_profile(db)
    db.add(Project(profile_id=profile.id, name="Kiyra",
                   technologies='["FastAPI", "Next.js"]'))
    db.commit()
    expanded, terms = retrieval_mod._expand_query(
        "Tell me about Kiyra", "", db, profile.id)
    assert "fastapi" in expanded.lower()
    assert terms


# ── MMR ─────────────────────────────────────────────────────────────────────

def test_mmr_prefers_distinct_passages_over_duplicates():
    from knowledge.passages import Passage
    passages = [
        Passage("The API gateway rate limits requests per key.", 0, 44, 0),
        Passage("The API gateway rate limits requests per key.", 45, 89, 1),
        Passage("The queue retries failed jobs with exponential backoff.", 90,
                143, 2),
    ]
    relevance = [1.0, 0.99, 0.8]
    picked = retrieval_mod._mmr_select(relevance, passages, k=2, lam=0.7)
    assert 0 in picked
    assert 2 in picked, "the duplicate passage must lose to the distinct one"
    assert 1 not in picked


# ── end-to-end retrieval ────────────────────────────────────────────────────

def test_retrieval_returns_the_ORIGINAL_document_text(db_session, monkeypatch):
    """The context must be the document's real text, not the chunk fragment."""
    db = db_session
    profile = _make_profile(db)
    doc = _make_doc(
        db, profile, "Kiyra Notes",
        "Kiyra is a local-first AI interview copilot. "
        "It runs a FastAPI backend, a Next.js frontend and a Windows overlay. "
        "The overlay stays on top and hides from the taskbar.",
        source_type="project",
    )
    # A deliberately unhelpful chunk: the answer must come from the document.
    _make_chunk(db, doc, "chunk fragment text", 0)

    monkeypatch.setattr(retrieval_mod, "embed_text", lambda t: [0.1] * 384)
    monkeypatch.setattr(retrieval_mod, "query_collection",
                        lambda *a, **k: [{
                            "content": "chunk fragment text", "score": 0.9,
                            "metadata": {"document_id": doc.id, "chunk_index": 0},
                        }])

    context, metrics = retrieval_mod.retrieve_context_sync(
        profile.id, "What is Kiyra?", top_k=3, db=db)

    assert "local-first AI interview copilot" in context
    assert "FastAPI backend" in context
    assert "chunk fragment text" not in context
    assert metrics["documents"] == 1
    assert metrics["error"] is None


def test_retrieval_finds_a_document_by_exact_term_only(db_session, monkeypatch):
    """A low cosine + a strong exact match must still be retrieved."""
    db = db_session
    profile = _make_profile(db)
    doc = _make_doc(db, profile, "Helmet Notes",
                    "Helmet secures Express middleware with configurable "
                    "policies.", source_type="project")
    _make_chunk(db, doc, "Helmet secures Express middleware with configurable "
                         "policies.", 0)

    # The dense layer knows nothing about this document.
    monkeypatch.setattr(retrieval_mod, "embed_text", lambda t: [0.1] * 384)
    monkeypatch.setattr(retrieval_mod, "query_collection",
                        lambda *a, **k: [{
                            "content": "something else entirely", "score": 0.05,
                            "metadata": {"document_id": 999, "chunk_index": 0},
                        }])

    context, metrics = retrieval_mod.retrieve_context_sync(
        profile.id, "How does Helmet work?", top_k=3, db=db)

    assert "Helmet secures Express middleware" in context
    assert metrics["lexical_hits"] >= 1


def test_long_document_is_narrowed_to_its_relevant_passages(db_session,
                                                             monkeypatch):
    db = db_session
    profile = _make_profile(db)
    filler = ("Filler paragraph about unrelated operational details that the "
              "candidate does not need. ") * 8
    doc = _make_doc(
        db, profile, "Long Resume",
        filler + "The standout project is EduScan, which extracts fields from "
                 "invoices using a deterministic OCR pipeline. "
        + filler,
        source_type="resume",
    )
    _make_chunk(db, doc, doc.content[:120], 0)

    monkeypatch.setattr(retrieval_mod, "embed_text", lambda t: [0.1] * 384)
    monkeypatch.setattr(retrieval_mod, "query_collection",
                        lambda *a, **k: [{
                            "content": doc.content[:120], "score": 0.8,
                            "metadata": {"document_id": doc.id, "chunk_index": 0},
                        }])

    context, metrics = retrieval_mod.retrieve_context_sync(
        profile.id, "Tell me about EduScan invoice extraction",
        top_k=3, db=db)

    assert "EduScan" in context
    assert "deterministic OCR pipeline" in context
    # The whole document was NOT pasted in.
    assert len(context) < len(doc.content)
    assert metrics["passages"] >= 1


def test_most_relevant_block_is_placed_last(db_session, monkeypatch):
    """The strongest passage sits closest to the question."""
    db = db_session
    profile = _make_profile(db)
    weak = _make_doc(db, profile, "Weak",
                     "A note about unrelated things that do not matter here.",
                     source_type="manual")
    strong = _make_doc(db, profile, "Strong",
                       "The exact answer is the Helmet security middleware.",
                       source_type="manual")
    _make_chunk(db, weak, weak.content, 0)
    _make_chunk(db, strong, strong.content, 0)

    def _fake_query(profile_id, embedding, top_k=5, where=None):
        return [
            {"content": strong.content, "score": 0.9,
             "metadata": {"document_id": strong.id, "chunk_index": 0}},
            {"content": weak.content, "score": 0.3,
             "metadata": {"document_id": weak.id, "chunk_index": 0}},
        ]

    monkeypatch.setattr(retrieval_mod, "embed_text", lambda t: [0.1] * 384)
    monkeypatch.setattr(retrieval_mod, "query_collection", _fake_query)

    context, _metrics = retrieval_mod.retrieve_context_sync(
        profile.id, "What is Helmet?", top_k=3, db=db)

    blocks = context.split("\n\n")
    assert len(blocks) == 2
    assert "Helmet" in blocks[-1], "the strongest block must come last"


def test_intro_question_falls_back_to_the_candidates_own_documents(
        db_session, monkeypatch):
    """"Tell me about yourself" has no searchable terms — the resume is it."""
    db = db_session
    profile = _make_profile(db)
    _make_doc(db, profile, "My Resume",
              "BETHI ABHINAY SOFTWARE ENGINEER\n\nOBJECTIVE\nBuild reliable "
              "applications and solve engineering problems.",
              source_type="resume")
    _make_chunk(db, _make_doc(db, profile, "Other",
                               "Some unrelated notes about databases."),
                 "Some unrelated notes about databases.", 0)

    # Similarity search finds nothing at all.
    monkeypatch.setattr(retrieval_mod, "embed_text", lambda t: [0.1] * 384)
    monkeypatch.setattr(retrieval_mod, "query_collection",
                        lambda *a, **k: [{
                            "content": "unrelated", "score": 0.01,
                            "metadata": {"document_id": 999, "chunk_index": 0},
                        }])

    context, metrics = retrieval_mod.retrieve_context_sync(
        profile.id, "Tell me about yourself", top_k=4, db=db,
        qtype="behavioral")

    assert "BETHI ABHINAY" in context
    assert metrics["documents"] >= 1


def test_a_normal_question_is_not_pulled_into_the_intro_fallback(
        db_session, monkeypatch):
    db = db_session
    profile = _make_profile(db)
    _make_doc(db, profile, "My Resume", "BETHI ABHINAY SOFTWARE ENGINEER",
              source_type="resume")
    monkeypatch.setattr(retrieval_mod, "embed_text", lambda t: [0.1] * 384)
    monkeypatch.setattr(retrieval_mod, "query_collection",
                        lambda *a, **k: [{
                            "content": "unrelated", "score": 0.01,
                            "metadata": {"document_id": 999, "chunk_index": 0},
                        }])
    context, _metrics = retrieval_mod.retrieve_context_sync(
        profile.id, "What is the difference between a list and a tuple?",
        top_k=4, db=db, qtype="conceptual")
    assert "BETHI ABHINAY" not in context


def test_retrieval_never_raises_on_a_broken_db(db_session, monkeypatch):
    db = db_session
    profile = _make_profile(db)
    monkeypatch.setattr(retrieval_mod, "embed_text", lambda t: [0.1] * 384)
    monkeypatch.setattr(retrieval_mod, "query_collection",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    context, metrics = retrieval_mod.retrieve_context_sync(
        profile.id, "anything", top_k=3, db=db)
    assert context == ""
    assert metrics["error"]
