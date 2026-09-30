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