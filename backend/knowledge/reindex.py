"""Rebuild ChromaDB collections from the durable SQL knowledge base.

The SQL tables are the source of truth for knowledge content; ChromaDB is a
derived vector index. If the on-disk store is reset (see
``vectorstore.get_chroma_client``) or otherwise lost, every collection can be
rebuilt here by re-embedding the stored chunk text — no re-upload required.
"""
from __future__ import annotations

from db.database import SessionLocal
from db.models import KnowledgeChunk, KnowledgeDocument
from knowledge.embeddings import embed_batch
from knowledge.vectorstore import add_chunks, get_collection


def _reindex_profile(profile_id: int, items: list[tuple[KnowledgeChunk, KnowledgeDocument]]) -> int:
    """Re-embed the SQL chunks for one profile into its (empty) collection."""
    by_doc: dict[int, list[tuple[KnowledgeChunk, KnowledgeDocument]]] = {}
    for chunk, doc in items:
        by_doc.setdefault(doc.id, []).append((chunk, doc))

    total = 0
    for doc_id, group in by_doc.items():
        chunks = [
            {
                "content": c.content,
                "chunk_index": c.chunk_index,
                "metadata": {
                    **(c.meta_data or {}),
                    "source_type": d.source_type,
                    "title": d.title,
                },
            }
            for c, d in group
        ]
        embeddings = embed_batch([c["content"] for c in chunks])
        add_chunks(profile_id, chunks, embeddings, doc_id)
        total += len(chunks)
    return total


def reindex_empty_collections(force: bool = False) -> dict[int, int]:
    """Re-index profiles whose Chroma collection is empty but has SQL chunks.

    Returns ``{profile_id: chunk_count}`` for profiles that were (re)built.
    Best-effort — never raises, so it is safe to call from startup.
    """
    rebuilt: dict[int, int] = {}
    db = SessionLocal()
    try:
        rows = (
            db.query(KnowledgeChunk, KnowledgeDocument)
            .join(KnowledgeDocument, KnowledgeChunk.document_id == KnowledgeDocument.id)
            .order_by(KnowledgeChunk.document_id, KnowledgeChunk.chunk_index)
            .all()
        )
        by_profile: dict[int, list[tuple[KnowledgeChunk, KnowledgeDocument]]] = {}
        for chunk, doc in rows:
            by_profile.setdefault(doc.profile_id, []).append((chunk, doc))

        for profile_id, items in by_profile.items():
            try:
                collection = get_collection(profile_id)
                if collection.count() > 0 and not force:
                    continue
                if force and collection.count() > 0:
                    collection.delete(where={"document_id": {"$gte": 0}})
                rebuilt[profile_id] = _reindex_profile(profile_id, items)
            except Exception as e:
                print(f"[RAG] re-index failed for profile {profile_id}: {e}")
    except Exception as e:
        print(f"[RAG] re-index scan failed: {e}")
    finally:
        db.close()

    if rebuilt:
        print(f"[RAG] re-indexed knowledge from SQL: {rebuilt}")
    return rebuilt


if __name__ == "__main__":
    import sys

    result = reindex_empty_collections(force="--force" in sys.argv)
    print(f"reindexed: {result}")
