"""Rebuild the knowledge index (chunks + embeddings) from the original docs.

Run this after changing the chunker or the retrieval layer:

    cd backend && uv run python scripts\\reindex_knowledge.py

What it does:
  1. Restores any document whose stored content was truncated, by pulling the
     full original text back from its source (Resume.raw_text / JobDescription
     .raw_text). This is what makes "RAG must fetch the true original
     document content" true even for documents ingested by an older build.
  2. Deletes the old chunks from ChromaDB + SQL.
  3. Re-chunks every document with the sentence-aware chunker, re-embeds and
     stores the new chunks.

Safe to re-run: it rebuilds the index from the documents every time.
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from db.database import SessionLocal  # noqa: E402
from db.models import (  # noqa: E402
    CandidateProfile,
    JobDescription,
    KnowledgeChunk,
    KnowledgeDocument,
    Resume,
)
from knowledge.chunker import chunk_document  # noqa: E402
from knowledge.embeddings import embed_batch  # noqa: E402
from knowledge.vectorstore import add_chunks, delete_document_chunks  # noqa: E402


def _restore_full_text(db) -> int:
    """Pull the original text back from the source table when it was cut."""
    restored = 0
    docs = db.query(KnowledgeDocument).all()
    for doc in docs:
        source = doc.source_type
        original = None
        if source == "resume" and doc.source_id:
            row = db.query(Resume).filter_by(id=doc.source_id).first()
            original = (row.raw_text or "") if row else None
        elif source == "job_description" and doc.source_id:
            row = db.query(JobDescription).filter_by(id=doc.source_id).first()
            original = (row.raw_text or "") if row else None

        if original and len(original) > len(doc.content or ""):
            before = len(doc.content or "")
            doc.content = original
            restored += 1
            print(f"  restored {source} doc {doc.id}: "
                  f"{before} -> {len(original)} chars")
    db.commit()
    return restored


def main() -> int:
    db = SessionLocal()
    try:
        profile = db.query(CandidateProfile).first()
        if profile is None:
            print("No candidate profile found — nothing to reindex.")
            return 1
        print(f"profile {profile.id} ({profile.name})")

        restored = _restore_full_text(db)
        print(f"restored {restored} truncated document(s)")

        docs = db.query(KnowledgeDocument).filter_by(
            profile_id=profile.id).all()
        print(f"reindexing {len(docs)} document(s)")

        total_chunks = 0
        for doc in docs:
            # Drop the old chunks (Chroma + SQL).
            try:
                delete_document_chunks(profile.id, doc.id)
            except Exception as e:
                print(f"  chroma delete failed for doc {doc.id}: {e}")
            db.query(KnowledgeChunk).filter_by(document_id=doc.id).delete()

            content = doc.content or ""
            chunks = chunk_document(
                content,
                metadata={"source_type": doc.source_type, "title": doc.title},
                chunk_size=256,
                overlap=40,
            )
            if not chunks:
                db.commit()
                print(f"  doc {doc.id} [{doc.source_type}] {doc.title!r}: empty")
                continue

            embeddings = embed_batch([c["content"] for c in chunks])
            chroma_ids = add_chunks(profile.id, chunks, embeddings, doc.id)
            for i, chunk in enumerate(chunks):
                db.add(KnowledgeChunk(
                    document_id=doc.id,
                    content=chunk["content"],
                    chunk_index=chunk["chunk_index"],
                    embedding_id=chroma_ids[i] if i < len(chroma_ids) else None,
                    meta_data=chunk.get("metadata"),
                ))
            db.commit()
            total_chunks += len(chunks)
            print(f"  doc {doc.id} [{doc.source_type}] {doc.title!r}: "
                  f"{len(content)} chars -> {len(chunks)} chunk(s)")

        print(f"done — {total_chunks} chunks")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
