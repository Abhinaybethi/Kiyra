"""ChromaDB vector store integration."""
from __future__ import annotations

import os
import uuid
from typing import Optional

import chromadb
from chromadb.config import Settings as ChromaSettings

from config import settings as app_settings


_client: Optional[chromadb.PersistentClient] = None


def get_chroma_client() -> chromadb.PersistentClient:
    global _client
    if _client is None:
        os.makedirs(app_settings.chroma_dir, exist_ok=True)
        _client = chromadb.PersistentClient(
            path=app_settings.chroma_dir,
            settings=ChromaSettings(anonymized_telemetry=False),
        )
    return _client


def get_collection(profile_id: int) -> chromadb.Collection:
    """One ChromaDB collection per candidate profile."""
    client = get_chroma_client()
    collection_name = f"profile_{profile_id}"
    return client.get_or_create_collection(
        name=collection_name,
        metadata={"hnsw:space": "cosine"},
    )


def add_chunks(
    profile_id: int,
    chunks: list[dict],  # {content, chunk_index, metadata}
    embeddings: list[list[float]],
    document_id: int,
) -> list[str]:
    """Add chunks with precomputed embeddings to ChromaDB. Returns doc IDs."""
    if not chunks or not embeddings:
        return []

    collection = get_collection(profile_id)
    ids = [str(uuid.uuid4()) for _ in chunks]
    documents = [c["content"] for c in chunks]
    metadatas = [
        {**c.get("metadata", {}), "document_id": document_id, "chunk_index": c["chunk_index"]}
        for c in chunks
    ]

    collection.add(
        ids=ids,
        embeddings=embeddings,
        documents=documents,
        metadatas=metadatas,
    )
    return ids


def query_collection(
    profile_id: int,
    query_embedding: list[float],
    top_k: int = 5,
    where: Optional[dict] = None,
) -> list[dict]:
    """Query ChromaDB and return ranked results."""
    collection = get_collection(profile_id)

    kwargs = {
        "query_embeddings": [query_embedding],
        "n_results": min(top_k, collection.count() or 1),
        "include": ["documents", "distances", "metadatas"],
    }
    if where:
        kwargs["where"] = where

    results = collection.query(**kwargs)

    output = []
    if results["documents"] and results["documents"][0]:
        for doc, dist, meta in zip(
            results["documents"][0],
            results["distances"][0],
            results["metadatas"][0],
        ):
            output.append({
                "content": doc,
                "score": 1 - dist,  # cosine distance → similarity
                "metadata": meta,
            })
    return output


def delete_document_chunks(profile_id: int, document_id: int) -> None:
    """Remove all chunks for a document."""
    collection = get_collection(profile_id)
    collection.delete(where={"document_id": document_id})
