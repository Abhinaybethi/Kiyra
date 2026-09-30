"""Layered, dependency-free question detection for the real-time pipeline.

Strategy:
  1. Cheap deterministic signal extraction (starts-with, question mark, wh-words,
     interview patterns, phrasing) → confidence score.
  2. Deduplication: suppress near-duplicate questions within a sliding window.
  3. Cooldown: ignore repeat firing right after an answer started, but never
     block clearly distinct high-confidence follow-ups (rapid consecutive
     questions keep working).

`detect(text)` is a pure scoring function usable in tests and by other modules.
`QuestionDetector.detect` additionally applies dedup + cooldown state.
"""
from __future__ import annotations

import re
import time
import unicodedata
from collections import deque


# Question word/phrase starts (weight = signal strength)
QUESTION_STARTS: list[tuple[str, float]] = [
    ("walk me through", 0.5),
    ("can you explain", 0.5),
    ("could you explain", 0.5),
    ("what's the difference", 0.5),
    ("whats the difference", 0.5),
    ("what is the difference", 0.5),
    ("how would you", 0.45),
    ("how does", 0.5),
    ("how do", 0.4),
    ("why does", 0.45),
    ("why is", 0.45),
    ("can you", 0.4),
    ("could you", 0.4),
    ("would you", 0.4),
    ("explain the difference", 0.5),
    ("tell me about", 0.4),
    ("tell me a", 0.35),
    ("describe", 0.35),
    ("define", 0.35),
    ("what", 0.3),
    ("how", 0.3),
    ("why", 0.3),
    ("when", 0.3),
    ("where", 0.3),
    ("who", 0.3),
    ("which", 0.25),
    ("have you", 0.2),
    ("do you", 0.2),
    ("did you", 0.2),
    ("are you", 0.2),
    ("what is", 0.35),
    ("what are", 0.35),
]

# Phrases found anywhere that strongly indicate a question
CONTAINS_BOOSTS = [
    "difference between",
    "how would you",
    "what would",
    "would you",
    "could you",
    "explain",
    "describe",
    "tell me",
    "what happens",
    "what do you",
    "why do",
    "why is",
    "how does",
    "how do",
]

# Filler prefixes that reduce confidence (spoken English)
WEAKEN_PREFIXES = [
    "so", "um", "uh", "okay", "ok", "right", "well", "and", "yes", "no",
]

# ASCII-fold a transcript so punctuation/case variations are normalized
_FOLD_RE = re.compile(r"[^a-z0-9\s]")
_WS_RE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    return _WS_RE.sub(" ", _FOLD_RE.sub(" ", text.lower())).strip()


def _tokens(text: str) -> list[str]:
    return _normalize(text).split()


def similarity(a: str, b: str) -> float:
    """Token Jaccard overlap in [0, 1] — used for dedup and prefetch reuse."""
    ta, tb = set(_tokens(a)), set(_tokens(b))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _strip_filler(text_lower: str) -> str:
    words = text_lower.split()
    while words and words[0] in WEAKEN_PREFIXES:
        words = words[1:]
    return " ".join(words)


def analyze(text: str) -> dict:
    """Score a transcription fragment and return detailed signals."""
    cleaned = (text or "").strip()
    if len(cleaned) < 5:
        return {"is_question": False, "confidence": 0.0, "score": 0.0, "signals": {}}

    text_lower = cleaned.lower()
    text_lower = _strip_filler(text_lower)
    trimmed = text_lower

    score = 0.0
    signals: dict[str, float] = {}

    # 1) Explicit question mark — strongest single signal
    if cleaned.rstrip().endswith("?"):
        score += 0.5
        signals["question_mark"] = 1.0

    # 2) Starts with a recognized question phrase
    for phrase, weight in QUESTION_STARTS:
        if trimmed.startswith(phrase):
            score += weight
            signals[f"start:{phrase}"] = weight
            break

    # 3) Contains a boosting phrase anywhere (weaker by design)
    for phrase in CONTAINS_BOOSTS:
        if phrase in trimmed:
            score += 0.1
            signals[f"contains:{phrase}"] = 0.1
            break

    # 4) Length heuristic — questions are normally 4–40 words
    word_count = len(trimmed.split())
    if 4 <= word_count <= 40:
        score += 0.1
    elif word_count > 60:
        score -= 0.25  # long monologue, unlikely to be an immediate question
    signals["word_count"] = float(word_count)

    # 5) Single-sentence structure — ignore dots inside words (acronyms like
    #    "Node.js", initials like "Dr. Smith") via the whitespace/EOL lookahead.
    sentence_count = max(1, len(re.findall(r"[.!?](?:\s+|$)", cleaned)))
    if sentence_count <= 1:
        score += 0.05
    elif sentence_count > 2:
        score -= 0.1
    signals["sentences"] = float(sentence_count)

    # 6) Second-person / interview tone ("you", "your")
    if re.search(r"\b(your|you)\b", trimmed):
        score += 0.1
        signals["interviewee_voice"] = 0.1

    # 7) "difference between" emphasis
    if "difference between" in trimmed:
        score += 0.15
        signals["difference"] = 0.15

    confidence = min(max(score, 0.0), 1.0)
    is_q = confidence >= 0.4 and sentence_count <= 1

    return {
        "is_question": bool(is_q),
        "confidence": round(confidence, 3),
        "score": round(score, 3),
        "signals": signals,
    }


def detect(text: str) -> tuple[bool, float]:
    """Pure question detection — returns (is_question, confidence)."""
    result = analyze(text)
    return result["is_question"], result["confidence"]


def detect_partial(text: str) -> dict:
    """Early question detection on an *incomplete* transcript (no '?' yet).

    Returns the `analyze` result plus `stable_prefix` (enough words are proven
    speech to justify speculative work). Partials never touch the stateful
    dedup/cooldown window — only finalized utterances do.
    """
    result = analyze(text or "")
    result["partial"] = True
    words = _tokens(text or "")
    result["stable_prefix"] = len(words) >= 3
    return result


def _words(text: str) -> set[str]:
    """Punctuation-stripped word set (question marks, hyphens, apostrophes)."""
    return set(re.findall(r"[a-z0-9']+", _normalize(text)))


def partial_validation_score(partial: str, final: str) -> float:
    """Strict gate for releasing a speculative answer.

    A speculative question is only safe to release once the final transcript is
    in, and only if the partial is *exactly* the same question with at most an
    additive tail: every word the partial heard must survive verbatim in the
    final, and the partial must already cover most of it. A single misheard
    word ("pro" vs "process") or a drifted question scores 0.0 → the buffered
    answer is discarded and a fresh authoritative one is generated.
    """
    tp = _words(partial)
    tf = _words(final)
    if not tp or len(tp) < 5:  # too little speech to have heard the question
        return 0.0
    if len(tp) < 0.55 * len(tf):  # partial is far from the full question
        return 0.0
    if not tp.issubset(tf):  # phantom / misheard tokens ⇒ question drifted
        return 0.0
    return 1.0


_TRAILING_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "to", "of", "for", "with", "in",
    "on", "at", "by", "from", "as", "than", "so", "then", "there", "that",
    "this", "these", "those", "is", "are", "was", "were", "do", "does",
    "did", "have", "has", "you", "your", "it", "its", "how", "what", "why",
    "which", "when", "while", "if", "not", "no", "yes", "etc", "and more",
})


def partial_spec_eligible(text: str) -> bool:
    """Is this partial transcript 'complete enough' to gamble speculative
    generation + early release on it?

    Rejects transcripts that visibly cut off mid-phrase: whisper's trailing
    ellipsis ('...'), a dangling stopword (the question is clearly not done),
    or too few words to be a full question. Speeding on such partials wastes
    a generation and risks answering a truncated question.
    """
    t = (text or "").strip()
    if not t:
        return False
    if t.endswith("...") or t.endswith("…") or t.endswith(". . ."):
        return False
    words = t.rstrip("?.").strip().split()
    # Whisper happily appends '?' to mid-sentence fragments (e.g.
    # "Can you explain how a react?"). Requiring a real chunk of speech
    # makes a trailing '?' a strong completeness signal instead of a trap.
    if len(words) < 8:
        return False
    last = words[-1].strip(".,!?\"'").lower()
    if last in _TRAILING_STOPWORDS:
        return False
    return True


class QuestionDetector:
    """Stateful detector with near-duplicate suppression and cooldown."""

    def __init__(
        self,
        cooldown_seconds: float = 3.0,
        dedup_window: int = 24,
        dedup_threshold: float = 0.78,
        high_confidence_bypass: float = 0.8,
    ):
        self.cooldown_seconds = cooldown_seconds
        self.dedup_window = max(1, dedup_window)
        self.dedup_threshold = dedup_threshold
        self.high_confidence_bypass = high_confidence_bypass
        self._recent: deque[tuple[str, float]] = deque(maxlen=self.dedup_window)
        self._last_fire_ts: float = 0.0

    def _is_duplicate(self, text: str) -> bool:
        for prev, _ts in self._recent:
            if similarity(prev, text) >= self.dedup_threshold:
                return True
        return False

    def reset(self):
        self._recent.clear()
        self._last_fire_ts = 0.0

    def detect(self, text: str, now: float = None) -> tuple[bool, float]:
        """Dedup + cooldown-aware detection.

        Returns (True, confidence) only for genuinely new questions.
        High-confidence questions (>candidate_confidence) bypass cooldown so
        rapid consecutive questions are still answered.
        """
        now = now if now is not None else time.time()
        is_q, confidence = detect(text)
        if not is_q:
            return False, confidence

        if self._is_duplicate(text):
            return False, confidence

        in_cooldown = (now - self._last_fire_ts) < self.cooldown_seconds
        if in_cooldown and confidence < self.high_confidence_bypass:
            return False, confidence

        self._recent.append((text, now))
        self._last_fire_ts = now
        return True, confidence