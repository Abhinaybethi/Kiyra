"""ChromaDB vector store integration."""
from __future__ import annotations

import os
import shutil
import sqlite3
import threading
import time
import uuid
from typing import Optional

import chromadb
from chromadb.config import Settings as ChromaSettings

from config import settings as app_settings


_client: Optional[chromadb.PersistentClient] = None
_client_lock = threading.Lock()


def _store_readable(path: str) -> bool:
    """Cheap pre-flight check on the on-disk store.

    A ``chroma.sqlite3`` written by an incompatible Chroma build (or truncated
    by an interrupted write) makes the Rust backend raise ``unsupported file
    format`` while constructing its bindings — and once that happens the
    shared-system singleton is poisoned for the rest of the process. Probing
    the file with the stdlib sqlite3 *before* Chroma touches it lets us move a
    broken store aside cleanly. A missing file just means "fresh store".
    """
    db = os.path.join(path, "chroma.sqlite3")
    if not os.path.exists(db):
        return True
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            con.execute("SELECT count(*) FROM sqlite_master").fetchone()
            return True
        finally:
            con.close()
    except Exception:
        return False


def _reset_store(path: str) -> None:
    """Move an unreadable store aside (never delete — it may be recoverable)."""
    backup = f"{path}.corrupt-{int(time.time())}"
    try:
        if os.path.exists(path):
            shutil.move(path, backup)
        os.makedirs(path, exist_ok=True)
        print(f"[RAG] vector store unreadable — moved to {backup} and recreated "
              f"empty (knowledge can be re-indexed from the SQL chunks).")
    except Exception as e:
        print(f"[RAG] vector store reset failed: {e}")


def get_chroma_client() -> chromadb.PersistentClient:
    global _client
    if _client is not None:
        return _client
    with _client_lock:
        if _client is not None:
            return _client
        path = app_settings.chroma_dir
        os.makedirs(path, exist_ok=True)
        if not _store_readable(path):
            _reset_store(path)
        _client = chromadb.PersistentClient(
            path=path,
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
