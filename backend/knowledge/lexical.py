"""Lexical (BM25) retrieval over the knowledge base.

Dense embeddings answer "what is this *about*?" — they are weak on exact
names ("Helmet", "body-parser", "Kiyra", "qwen3.5"). BM25 answers "does this
text literally contain these words?", so fusing the two rankings with RRF
retrieves the right document for both kinds of question.

The index is built in-process from the SQL chunk rows: a candidate's
knowledge base is a personal document set (hundreds of chunks), so a full
scan is a few milliseconds and keeps the whole RAG path free of extra
services.
"""
from __future__ import annotations

import math
import re
from collections import Counter

# A word is a run of letters/digits, optionally joined by . - _ so that
# "node.js", "body-parser" and "fastapi" survive as single searchable terms.
_WORD = re.compile(r"[A-Za-z0-9]+(?:[.\-_][A-Za-z0-9]+)*")

# Tiny stop-word list. Deliberately short: interview questions are short and
# over-aggressive stopping throws away the only meaningful word.
_STOP = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "do", "does", "did", "of", "to", "in", "on", "at", "for", "with", "and",
    "or", "but", "if", "then", "than", "so", "as", "by", "from", "that",
    "this", "these", "those", "it", "its", "i", "you", "we", "they", "me",
    "my", "your", "our", "their", "can", "could", "would", "should", "will",
    "shall", "may", "might", "must", "have", "has", "had", "there", "here",
    "what", "which", "who", "whom", "whose", "when", "where", "why", "how",
    "tell", "explain", "describe", "give", "show", "about", "your", "me",
}

# BM25 parameters (the textbook defaults; they work well on short docs).
_K1 = 1.5
_B = 0.75


def tokenize(text: str) -> list[str]:
    """Lowercase searchable terms of ``text``.

    A dotted/hyphenated term is emitted whole *and* split, so a query for
    "node" still matches a document that only says "node.js".
    """
    out: list[str] = []
    for raw in _WORD.findall(text or ""):
        low = raw.lower().strip("._-")
        if not low or low in _STOP or len(low) < 2:
            continue
        out.append(low)
        if any(sep in raw for sep in (".", "-", "_")):
            for part in re.split(r"[.\-_]", low):
                if len(part) >= 2 and part not in _STOP:
                    out.append(part)
    return out


class BM25Index:
    """A tiny BM25 index over an in-memory corpus."""

    def __init__(self, corpus: list[tuple[object, str]]):
        self._keys: list[object] = []
        self._tf: list[Counter] = []
        self._len: list[int] = []
        df: Counter = Counter()
        for key, text in corpus:
            terms = tokenize(text)
            self._keys.append(key)
            self._tf.append(Counter(terms))
            self._len.append(len(terms))
            df.update(set(terms))
        self._df = df
        self._n = len(self._keys)
        self._avg_len = (sum(self._len) / self._n) if self._n else 0.0

    def score(self, query: str) -> list[tuple[object, float]]:
        """BM25 score for every document, sorted best-first.

        Documents with a zero score are dropped.
        """
        terms = tokenize(query)
        if not terms or not self._n:
            return []
        qtf = Counter(terms)
        scored: list[tuple[object, float]] = []
        for i, key in enumerate(self._keys):
            tf = self._tf[i]
            dl = self._len[i] or 1
            score = 0.0
            for term, qcount in qtf.items():
                f = tf.get(term, 0)
                if not f:
                    continue
                df = self._df.get(term, 0)
                idf = math.log(1.0 + (self._n - df + 0.5) / (df + 0.5))
                denom = f + _K1 * (1.0 - _B + _B * dl / (self._avg_len or 1.0))
                score += idf * (f * (_K1 + 1.0) / denom) * (1.0 + 0.1 * (qcount - 1))
            if score > 0:
                scored.append((key, score))
        scored.sort(key=lambda kv: -kv[1])
        return scored

    def max_score(self, query: str) -> float:
        """The best score for ``query`` (used to normalise to 0..1)."""
        scored = self.score(query)
        return scored[0][1] if scored else 0.0


def normalize(scored: list[tuple[object, float]]) -> dict[object, float]:
    """Scale a score list to 0..1 against its own best score."""
    if not scored:
        return {}
    top = scored[0][1] or 1.0
    return {k: (v / top if top else 0.0) for k, v in scored}
