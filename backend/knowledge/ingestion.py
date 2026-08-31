"""Knowledge base ingestion pipeline."""
from __future__ import annotations

from sqlalchemy.orm import Session

from db.models import KnowledgeDocument, KnowledgeChunk
from knowledge.chunker import chunk_document
from knowledge.embeddings import embed_batch
from knowledge.vectorstore import add_chunks, delete_document_chunks


async def ingest_document(
    db: Session,
    profile_id: int,
    source_type: str,
    source_id: int,
    title: str,
    content: str,
    metadata: dict = None,
) -> KnowledgeDocument:
    """Full ingestion pipeline: chunk → embed → store in ChromaDB + SQL."""

    # Remove existing document of same source
    existing = db.query(KnowledgeDocument).filter_by(
        profile_id=profile_id, source_type=source_type, source_id=source_id
    ).first()
    if existing:
        delete_document_chunks(profile_id, existing.id)
        db.delete(existing)
        db.flush()

    # Create document record
    doc = KnowledgeDocument(
        profile_id=profile_id,
        source_type=source_type,
        source_id=source_id,
        title=title,
        content=content,
        meta_data=metadata or {},
    )
    db.add(doc)
    db.flush()

    # Chunk
    chunks = chunk_document(content, metadata={"source_type": source_type, "title": title})
    if not chunks:
        db.commit()
        return doc

    # Embed
    texts = [c["content"] for c in chunks]
    embeddings = embed_batch(texts)

    # Store in ChromaDB
    chroma_ids = add_chunks(profile_id, chunks, embeddings, doc.id)

    # Store chunk records in SQL
    for chunk, chroma_id in zip(chunks, chroma_ids):
        db.add(KnowledgeChunk(
            document_id=doc.id,
            content=chunk["content"],
            chunk_index=chunk["chunk_index"],
            embedding_id=chroma_id,
            meta_data=chunk.get("metadata"),
        ))

    db.commit()
    return doc
