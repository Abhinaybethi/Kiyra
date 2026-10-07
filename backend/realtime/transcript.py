"""Transcript aggregation — joins utterance fragments into deduplicated, bounded history.

The LLM/answer path must never see fragmented or duplicated partials. This
buffer normalizes whitespace, folds case, detects repeated tokens, keeps
timestamps, and maintains a bounded roll of finalized transcripts.
"""
from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

_WS_RE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Normalize whitespace and strip noise tokens from a fragment."""
    t = _WS_RE.sub(" ", (text or "")).strip().replace(" ,", ",").replace(" .", ".")
    return " ".join(t.split())


@dataclass
class TranscriptEntry:
    text: str
    timestamp: float
    speaker: str = "interviewer"
    is_question: bool = False
    confidence: float = 0.0
    segment_id: Optional[int] = None


class TranscriptBuffer:
    """Bounded, deduplicated transcript history with utterance roll-up."""

    def __init__(self, max_entries: int = 64, duplicate_threshold: float = 0.9):
        self.max_entries = max(1, max_entries)
        self.duplicate_threshold = duplicate_threshold
        self.entries: deque[TranscriptEntry] = deque(maxlen=self.max_entries)
        self.current_utterance: str = ""
        self.last_segment_ts: Optional[float] = None

    def add_fragment(self, text: str, speaker: str = "interviewer", ts: float = None) -> Optional[TranscriptEntry]:
        """Add a transcribed fragment, rolling it into the current utterance.

        Returns the merged entry only when the fragment is *new* content
        (deduplication prevents repeats like ``"can you explain ... can you
        explain ..."`` being treated as two questions).
        """
        ts = time.time() if ts is None else ts
        cleaned = normalize(text)
        if not cleaned or len(cleaned) < 3:
            return None

        merged = self._merge_into_utterance(cleaned)
        self.last_segment_ts = ts
        if merged is None:
            return None

        entry = TranscriptEntry(text=merged, timestamp=ts, speaker=speaker)
        self.entries.append(entry)
        return entry

    def _merge_into_utterance(self, fragment: str) -> Optional[str]:
        cur = self.current_utterance
        if not cur:
            self.current_utterance = fragment
            return fragment

        # If fragment's core is already fully contained → duplicate, drop.
        if self._is_dup(cur, fragment):
            return None

        # Merge: prefer junction-aware concat (add space only if needed).
        junction = "" if cur.endswith((" ", "?", ".", ",", ":", ";")) else " "
        merged = cur + junction + fragment
        self.current_utterance = merged
        return merged

    @staticmethod
    def _is_dup(a: str, b: str) -> bool:
        if not a or not b:
            return False
        short, long = (a, b) if len(a) < len(b) else (b, a)
        # Substring containment (normalized tokens) means b adds nothing new.
        if short in long:
            return True
        ta, tb = set(short.split()), set(long.split())
        return bool(ta) and len(ta & tb) / len(ta) >= 0.9

    def finalize_utterance(self) -> str:
        """Freeze the current utterance and reset the roll-up scratch state."""
        finalized = self.current_utterance
        if finalized:
            self.entries.append(TranscriptEntry(text=finalized, timestamp=self.last_segment_ts or time.time()))
        self.current_utterance = ""
        return finalized

    def latest(self) -> Optional[TranscriptEntry]:
        return self.entries[-1] if self.entries else None

    def recent(self, n: int = 8) -> list[TranscriptEntry]:
        return list(self.entries)[-n:]

    def as_text(self, max_entries: int = 8) -> str:
        """Recent transcript as a single text block (used for context)."""
        return "\n".join(e.text for e in list(self.entries)[-max_entries:])


# ─────────────────────────────────────────────────────────────────────────────
# Interview Listening Mode — the turn buffer
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TurnSegment:
    """One finalized STT segment inside the current turn."""
    text: str
    segment_id: str
    index: int
    timestamp: float = 0.0


class InterviewTranscriptBuffer:
    """Accumulates every finalized STT segment of ONE interview question.

    This is the object that makes the question boundary *user-controlled*
    instead of VAD-controlled. VAD still segments audio (that is what makes
    STT cheap), but each finalized segment is merely **appended here**. Nothing
    is submitted until :meth:`snapshot` is called by an explicit SEND.

    Guarantees:

    * ``append`` is idempotent per ``segment_id`` AND content-aware: a repeated
      partial ("what is rag" twice) never grows the buffer.
    * ``pause``/``resume`` gate new text without losing what is already there.
    * ``clear`` starts a fresh turn; ``snapshot`` freezes the turn for sending
      without clearing it (so a failed generation can be retried).
    """

    def __init__(self, min_useful_chars: int = 8,
                 duplicate_threshold: float = 0.9):
        self.min_useful_chars = max(1, int(min_useful_chars))
        self.duplicate_threshold = duplicate_threshold
        self._segments: list[TurnSegment] = []
        self._seen_ids: set[str] = set()
        self._paused = False
        self._counter = 0

    # ── state ───────────────────────────────────────────────────────────

    @property
    def is_paused(self) -> bool:
        return self._paused

    @property
    def segment_count(self) -> int:
        return len(self._segments)

    @property
    def is_empty(self) -> bool:
        return not self.get_current()

    @property
    def can_send(self) -> bool:
        """SEND is only enabled once there is a usefully-long question."""
        return len(self.get_current()) >= self.min_useful_chars

    # ── mutation ────────────────────────────────────────────────────────

    def append(self, text: str, segment_id: str | None = None) -> Optional[TurnSegment]:
        """Append a finalized STT segment.

        Returns the stored :class:`TurnSegment`, or ``None`` when the text was
        rejected — paused, too short, a repeated ``segment_id``, or content
        already contained in the buffer. Callers treat ``None`` as "nothing
        changed", which is what keeps one utterance from being appended twice
        when a final STT lands after a partial.
        """
        if self._paused:
            return None

        cleaned = normalize(text)
        if not cleaned or len(cleaned) < 3:
            return None

        sid = segment_id or f"auto-{self._counter}"
        if sid in self._seen_ids:
            return None

        current = self.get_current()
        # A segment already contained in the turn adds nothing (this is the
        # "what is rag" / "what is rag" guard). Only reject when the fragment
        # is not merely a prefix — a real continuation must still be allowed.
        if current and self._is_redundant(current, cleaned):
            self._seen_ids.add(sid)
            return None

        self._counter += 1
        seg = TurnSegment(text=cleaned, segment_id=sid,
                          index=len(self._segments),
                          timestamp=time.time())
        self._segments.append(seg)
        self._seen_ids.add(sid)
        return seg

    def _is_redundant(self, current: str, fragment: str) -> bool:
        """True when `fragment` contributes nothing new to `current`."""
        cur_norm = current.rstrip("?.! ").lower()
        frag_norm = fragment.rstrip("?.! ").lower()
        if not frag_norm:
            return True
        if cur_norm.endswith(frag_norm) or frag_norm in cur_norm:
            return True
        # Token-overlap guard for near-identical repeats.
        cur_tokens, frag_tokens = set(cur_norm.split()), set(frag_norm.split())
        if not frag_tokens:
            return True
        if cur_tokens and len(cur_tokens & frag_tokens) / len(frag_tokens) >= self.duplicate_threshold:
            return True
        return False

    def get_current(self) -> str:
        """The turn's transcript so far, as one normalized question string."""
        if not self._segments:
            return ""
        return normalize(" ".join(s.text for s in self._segments))

    def snapshot(self) -> str:
        """Freeze and return the current transcript (does NOT clear it)."""
        return self.get_current()

    def clear(self) -> None:
        """Start a fresh turn."""
        self._segments.clear()
        self._seen_ids.clear()
        self._paused = False

    def pause(self) -> None:
        """Stop accepting new transcript text, keeping what is buffered."""
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    def segments(self) -> list[TurnSegment]:
        return list(self._segments)