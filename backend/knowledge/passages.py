"""Passage-level selection inside an ORIGINAL knowledge document.

RAG must quote the candidate's real text — never a truncated chunk. But a
document can be long (a resume is thousands of characters), and pasting the
whole thing into the prompt wastes the budget and buries the relevant part.

These helpers cut a document into passage units that keep the original
wording and order, so selected passages can be re-assembled *verbatim*:
the model reads the candidate's actual sentences, not a paraphrase and not
a fragment that starts mid-thought.

Design notes:
  * A passage never starts or ends mid-sentence. A single sentence longer
    than ``max_chars`` becomes its own passage rather than being cut — a
    broken sentence in the prompt teaches the model to write broken
    sentences.
  * :func:`assemble_passages` slices the ORIGINAL string by offset, so the
    result is provably the document's own text (a test asserts this).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# A sentence ends at . ! ? optionally followed by closing quotes/brackets and
# then whitespace. The lookbehind keeps the terminator with its sentence.
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])[\"')\]]*(?=\s)")
# Common abbreviations that must NOT end a sentence ("e.g." / "i.e." / "Dr.").
_ABBREVIATIONS = {
    "e.g", "i.e", "etc", "vs", "mr", "mrs", "ms", "dr", "jr", "sr", "st",
    "fig", "no", "inc", "ltd", "co", "corp", "dept", "approx", "min", "max",
}
# A word is a run of letters/digits, optionally joined by . - _ so that
# "node.js", "body-parser" and "fastapi" survive as single searchable terms.
_WORD = re.compile(r"[A-Za-z0-9]+(?:[.\-_][A-Za-z0-9]+)*")


@dataclass(frozen=True)
class Passage:
    """One passage unit of a document, with its offsets in the original text."""

    text: str
    start: int
    end: int
    index: int


def split_sentences(text: str) -> list[str]:
    """Split text into sentences, keeping terminators and paragraph breaks.

    Newlines are hard boundaries (a resume's bullet list must not be glued
    into one sentence), and a known abbreviation never ends a sentence.
    """
    if not text:
        return []
    out: list[str] = []
    # Hard-split on newlines first, then on sentence terminators.
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if not stripped:
            continue
        pieces = _SENTENCE_BREAK.split(stripped)
        buf = ""
        for piece in pieces:
            if not piece:
                continue
            piece = piece.strip()
            if not piece:
                continue
            candidate = f"{buf} {piece}".strip() if buf else piece
            # "e.g." style: keep going if the sentence so far ends with an
            # abbreviation or a single initial.
            tail = candidate.rstrip(".!?\"')]").split()
            last = tail[-1].lower() if tail else ""
            if last in _ABBREVIATIONS or (len(last) == 1 and last.isalpha()):
                buf = candidate
                continue
            out.append(candidate)
            buf = ""
        if buf:
            out.append(buf)
    return out


def split_passages(
    text: str,
    max_chars: int = 320,
    min_chars: int = 40,
) -> list[Passage]:
    """Cut ``text`` into sentence-aligned passages of at most ``max_chars``.

    Consecutive sentences are grouped until the next one would overflow the
    budget. ``min_chars`` only controls how eagerly the *last* small
    remainder is merged back into the previous passage — it never merges a
    passage that is already full.
    """
    if not text or not text.strip():
        return []
    max_chars = max(1, int(max_chars))

    passages: list[Passage] = []
    cursor = 0            # offset of the current passage start in `text`
    buf = ""              # accumulated text of the current passage
    buf_start = 0

    def _flush(end_offset: int):
        nonlocal buf, buf_start
        body = buf.strip()
        if not body:
            buf = ""
            return
        # Locate the passage verbatim inside the original text so its offsets
        # are exact (and the text is provably unmodified).
        start = text.find(body, buf_start)
        if start < 0:
            start = buf_start
        passages.append(Passage(text=body, start=start,
                                end=start + len(body), index=len(passages)))
        buf = ""
        buf_start = end_offset

    for sentence in split_sentences(text):
        if not buf:
            buf_start = text.find(sentence, cursor)
            if buf_start < 0:
                buf_start = cursor
            cursor = buf_start + len(sentence)
        if buf and len(buf) + 1 + len(sentence) > max_chars:
            _flush(cursor)
            buf_start = text.find(sentence, cursor)
            if buf_start < 0:
                buf_start = cursor
            cursor = buf_start + len(sentence)
        buf = f"{buf} {sentence}".strip() if buf else sentence
    if buf:
        _flush(cursor)

    # Merge a tiny trailing passage back into its predecessor — but never
    # past the size budget, or the merged passage would no longer be a
    # passage the caller asked for.
    if len(passages) >= 2 and len(passages[-1].text) < min_chars:
        last = passages[-1]
        prev = passages[-2]
        merged_text = f"{prev.text} {last.text}"
        if len(merged_text) <= max_chars:
            passages[-2] = Passage(
                text=merged_text,
                start=prev.start,
                end=last.end,
                index=prev.index,
            )
            passages.pop()
    for i, p in enumerate(passages):
        passages[i] = Passage(text=p.text, start=p.start, end=p.end, index=i)
    return passages


def assemble_passages(
    text: str,
    passages: list[Passage],
    selected: list[int],
) -> str:
    """Re-assemble the selected passages IN DOCUMENT ORDER, verbatim.

    Gaps between non-contiguous passages are marked with " … " so the model
    can tell that something was skipped (and does not read two unrelated
    fragments as one continuous claim).
    """
    if not selected:
        return ""
    chosen = [p for p in passages if p.index in set(selected)]
    if not chosen:
        return ""
    chosen.sort(key=lambda p: p.index)
    parts: list[str] = []
    prev_end: int | None = None
    for p in chosen:
        body = text[p.start:p.end].strip() or p.text.strip()
        if prev_end is not None and p.start > prev_end:
            parts.append("…")
        parts.append(body)
        prev_end = p.end
    return " ".join(parts)


def passage_terms(text: str) -> set[str]:
    """Lowercased searchable terms of a passage (used for scoring)."""
    out: set[str] = set()
    for raw in _WORD.findall(text or ""):
        low = raw.lower().strip("._-")
        if len(low) >= 2:
            out.add(low)
    return out
