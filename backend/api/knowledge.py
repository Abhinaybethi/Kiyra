"""Knowledge Base REST endpoints for candidate RAG management."""
from __future__ import annotations

from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from db.database import get_db
from db.models import CandidateProfile, KnowledgeDocument, KnowledgeChunk
from knowledge.chunker import chunk_document
from knowledge.embeddings import embed_batch, embed_text, embedding_dimension
from knowledge.vectorstore import add_chunks, delete_document_chunks, query_collection
from config import settings

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])


class CreateKnowledgeDocRequest(BaseModel):
    title: str
    content: str
    source_type: str = "manual"  # manual, star_story, project, system_design
    metadata: Optional[dict] = None


class SearchKnowledgeRequest(BaseModel):
    query: str
    top_k: int = 5
    min_score: float = 0.0


def _get_active_profile(db: Session) -> CandidateProfile:
    profile = db.query(CandidateProfile).first()
    if not profile:
        profile = CandidateProfile(
            user_id=1,
            name="Candidate",
            target_role="Software Engineer",
        )
        db.add(profile)
        db.commit()
        db.refresh(profile)
    return profile


@router.get("")
def list_knowledge_documents(db: Session = Depends(get_db)):
    """List all knowledge base documents with chunk stats."""
    profile = _get_active_profile(db)
    docs = (
        db.query(KnowledgeDocument)
        .filter_by(profile_id=profile.id)
        .order_by(KnowledgeDocument.created_at.desc())
        .all()
    )
    return [
        {
            "id": d.id,
            "title": d.title,
            "source_type": d.source_type,
            "source_id": d.source_id,
            "content_preview": d.content[:200] + ("..." if len(d.content) > 200 else ""),
            "chunk_count": len(d.chunks),
            "metadata": d.meta_data,
            "created_at": d.created_at.isoformat() if d.created_at else None,
        }
        for d in docs
    ]


@router.post("")
async def create_knowledge_document(
    payload: CreateKnowledgeDocRequest,
    db: Session = Depends(get_db),
):
    """Add a new custom document to the candidate knowledge base and embed into ChromaDB."""
    profile = _get_active_profile(db)

    if not payload.title.strip() or not payload.content.strip():
        raise HTTPException(status_code=400, detail="Title and content are required")

    # 1. Save document in SQLite
    doc = KnowledgeDocument(
        profile_id=profile.id,
        source_type=payload.source_type,
        title=payload.title.strip(),
        content=payload.content.strip(),
        meta_data=payload.metadata or {},
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)

    # 2. Chunk text
    chunks = chunk_document(
        payload.content,
        chunk_size=256,
        overlap=30,
        metadata={"source_type": payload.source_type, "title": payload.title},
    )

    # 3. Generate embeddings locally
    chunk_texts = [c["content"] for c in chunks]
    try:
        embeddings = embed_batch(chunk_texts)
        embedding_ids = add_chunks(profile.id, chunks, embeddings, document_id=doc.id)
    except Exception as e:
        embeddings = []
        embedding_ids = []

    # 4. Save chunks to SQLite
    for i, c in enumerate(chunks):
        kc = KnowledgeChunk(
            document_id=doc.id,
            content=c["content"],
            chunk_index=c["chunk_index"],
            embedding_id=embedding_ids[i] if i < len(embedding_ids) else None,
            meta_data=c.get("metadata"),
        )
        db.add(kc)

    db.commit()

    return {
        "id": doc.id,
        "title": doc.title,
        "source_type": doc.source_type,
        "chunk_count": len(chunks),
        "created_at": doc.created_at.isoformat(),
    }


@router.delete("/{doc_id}")
def delete_knowledge_document(doc_id: int, db: Session = Depends(get_db)):
    """Delete a knowledge document and its vector embeddings."""
    profile = _get_active_profile(db)
    doc = db.query(KnowledgeDocument).filter_by(id=doc_id, profile_id=profile.id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Knowledge document not found")

    try:
        delete_document_chunks(profile.id, doc.id)
    except Exception:
        pass  # non-fatal

    db.delete(doc)
    db.commit()
    return {"ok": True, "deleted_id": doc_id}


@router.post("/search")
async def search_knowledge(
    payload: SearchKnowledgeRequest,
    db: Session = Depends(get_db),
):
    """Test semantic retrieval on the candidate knowledge base."""
    profile = _get_active_profile(db)
    if not payload.query.strip():
        return {"query": payload.query, "results": []}

    try:
        query_embedding = embed_text(payload.query)
        results = query_collection(
            profile.id,
            query_embedding,
            top_k=payload.top_k,
        )
        filtered = [
            r for r in results if r["score"] >= payload.min_score
        ]
        return {
            "query": payload.query,
            "total_matches": len(filtered),
            "results": filtered,
        }
    except Exception as e:
        return {"query": payload.query, "total_matches": 0, "results": [], "error": str(e)}


@router.get("/stats")
def get_knowledge_stats(db: Session = Depends(get_db)):
    """Get vector store and knowledge base statistics."""
    profile = _get_active_profile(db)
    doc_count = db.query(KnowledgeDocument).filter_by(profile_id=profile.id).count()
    chunk_count = (
        db.query(KnowledgeChunk)
        .join(KnowledgeDocument)
        .filter(KnowledgeDocument.profile_id == profile.id)
        .count()
    )

    try:
        dim = embedding_dimension()
    except Exception:
        dim = 384

    return {
        "profile_id": profile.id,
        "total_documents": doc_count,
        "total_chunks": chunk_count,
        "embedding_model": "all-MiniLM-L6-v2",
        "embedding_dimension": dim,
        "vector_store": "ChromaDB (Persistent)",
        "storage_path": settings.chroma_dir,
    }
