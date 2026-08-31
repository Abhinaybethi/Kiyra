"""Text chunking for knowledge base ingestion."""
from __future__ import annotations

import re
from typing import Iterator


def chunk_text(
    text: str,
    chunk_size: int = 512,
    overlap: int = 50,
) -> Iterator[str]:
    """Split text into overlapping chunks by token-approximate word count."""
    # Normalize whitespace
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return

    words = text.split()
    if not words:
        return

    start = 0
    while start < len(words):
        end = min(start + chunk_size, len(words))
        chunk = " ".join(words[start:end])
        if chunk.strip():
            yield chunk
        if end >= len(words):
            break
        start = end - overlap  # overlap


def chunk_document(
    text: str,
    metadata: dict = None,
    chunk_size: int = 512,
    overlap: int = 50,
) -> list[dict]:
    """Returns list of {content, chunk_index, metadata} dicts."""
    chunks = []
    for i, chunk in enumerate(chunk_text(text, chunk_size=chunk_size, overlap=overlap)):
        chunks.append({
            "content": chunk,
            "chunk_index": i,
            "metadata": metadata or {},
        })
    return chunks
