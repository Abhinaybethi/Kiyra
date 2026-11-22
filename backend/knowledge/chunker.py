"""Text chunking for knowledge base ingestion.

Chunks are sentence-aware: a chunk boundary is placed where a sentence ends,
never in the middle of one. The old whitespace-window chunker cut mid-sentence
("...building reliable applications, solving engineer|ing problems"), which
produced chunks that read as fragments — bad embeddings, and bad context when
a chunk is shown to the model.

The text of a chunk is the document's own text (line structure preserved), so
a retrieved chunk can always be traced back to the original wording.
"""
from __future__ import annotations

import re
from typing import Iterator

from knowledge.passages import split_sentences

# A run of spaces/tabs (but not newlines) is collapsed to one space.
_SPACES = re.compile(r"[ \t]+")


def _clean(text: str) -> str:
    """Collapse horizontal whitespace, keep the line structure."""
    lines = [_SPACES.sub(" ", line).rstrip() for line in (text or "").splitlines()]
    return "\n".join(lines).strip()


def chunk_text(
    text: str,
    chunk_size: int = 512,
    overlap: int = 50,
) -> Iterator[str]:
    """Split text into overlapping, sentence-aligned chunks.

    ``chunk_size`` and ``overlap`` are WORD counts. A chunk is filled with
    whole sentences until the next one would overflow the budget; the last
    few sentences (worth ~``overlap`` words) are carried into the next chunk
    so a fact that straddles a boundary is still retrievable.

    A single sentence longer than ``chunk_size`` words is split on word
    boundaries — the only place a sentence is ever cut.
    """
    text = _clean(text)
    if not text:
        return

    chunk_size = max(20, int(chunk_size))
    overlap = max(0, min(int(overlap), chunk_size - 1))

    buf: list[str] = []
    buf_words = 0

    def _flush() -> Iterator[str]:
        nonlocal buf, buf_words
        if not buf:
            return
        body = "\n".join(buf).strip()
        if body:
            yield body
        # Carry the tail of this chunk into the next one.
        carry: list[str] = []
        carry_words = 0
        for prev in reversed(buf):
            pw = len(prev.split())
            if carry_words + pw > overlap:
                break
            carry.insert(0, prev)
            carry_words += pw
        buf = carry
        buf_words = carry_words

    for unit in split_sentences(text):
        if not unit:
            continue
        words = len(unit.split())
        if words > chunk_size:
            # Oversized sentence: flush what we have, then window it.
            for piece in _flush():
                yield piece
            step = max(1, chunk_size - overlap)
            unit_words = unit.split()
            i = 0
            while i < len(unit_words):
                piece = " ".join(unit_words[i:i + chunk_size])
                if piece.strip():
                    yield piece
                i += step
            buf = []
            buf_words = 0
            continue
        if buf and buf_words + words > chunk_size:
            for piece in _flush():
                yield piece
        buf.append(unit)
        buf_words += words

    if buf:
        body = "\n".join(buf).strip()
        if body:
            yield body


def chunk_document(
    text: str,
    metadata: dict = None,
    chunk_size: int = 512,
    overlap: int = 50,
) -> list[dict]:
    """Returns list of {content, chunk_index, metadata} dicts."""
    chunks = []
    for i, chunk in enumerate(chunk_text(text, chunk_size=chunk_size,
                                         overlap=overlap)):
        chunks.append({
            "content": chunk,
            "chunk_index": i,
            "metadata": metadata or {},
        })
    return chunks
