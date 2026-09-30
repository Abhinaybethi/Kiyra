"""Session memory for live interview assistance.

Components (all deterministic — no extra LLM call, no new infrastructure):

* :class:`Turn` / :class:`SessionMemory` — bounded per-session conversation
  state: recent turns, active topic, important entities, compact summary.
* :class:`ContextResolver` — follow-up / pronoun resolution over that state
  ("Why did you choose that approach?" → resolved against the active topic so
  retrieval and the prompt see the real referent). The original question is
  always preserved for display and persistence.
* :class:`RetrievalCache` — per-session, TTL'd, topic-scoped RAG cache.
  Reuse requires a topic match AND an equal/highly-similar query; anything
  else falls back to a fresh retrieval (correctness over reuse).
* :class:`ContextBuilder` — sectioned user prompt: CANDIDATE / CURRENT
  QUESTION / ACTIVE TOPIC / PREVIOUS CONTEXT / RELEVANT KNOWLEDGE /
  RECENT CONVERSATION / ANSWER REQUIREMENTS.

Failure rule: every helper degrades to identity resolution / uncached
retrieval / minimal prompt — never to a failed answer.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from config import settings
from realtime.question_detector import similarity
from realtime.question_analyzer import QuestionAnalysis, coding_prompt_block

# ── Text helpers ───────────────────────────────────────────────────────────

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_TECH_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9]+(?:[.\-+#][A-Za-z0-9]+)+")


def normalize(text: str) -> str:
    """Lowercase, punctuation-free, whitespace-collapsed form of ``text``."""
    return _NON_ALNUM.sub(" ", (text or "").lower()).strip()


def alias(text: str) -> str:
    """Separator-free key ("F.R.I.D.A.Y" → "friday", "faster-whisper" →
    "fasterwhisper") used for entity matching and cache keys."""
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def extract_tech_tokens(text: str) -> list[str]:
    """Hyphen/dot/plus-bearing tokens (faster-whisper, Node.js, C++, …)."""
    return _TECH_TOKEN.findall(text or "")


def _one_line(text: str) -> str:
    return " ".join((text or "").split())


def _clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# ── Follow-up detection ────────────────────────────────────────────────────

_LEAD_PRONOUNS = {"it", "that", "this", "they", "them", "those", "these", "he", "she"}
_ANAPHORA = re.compile(
    r"\b(it|its|they|them|their|those|these|again|same|earlier|previous|previously|above)\b",
    re.IGNORECASE,
)
_DEMONSTRATIVE = re.compile(
    r"\b(?:that|this|those|these)\s+"
    r"(?:approach|project|system|framework|solution|design|tool|model|"
    r"pipeline|way|idea|technology|stack|choice|decision)\b",
    re.IGNORECASE,
)
# The exact cue span each rule matched (used for referent substitution).
_LEAD_RE = re.compile(r"^\s*(it|that|this|they|them|those|these|he|she)\b", re.IGNORECASE)


@dataclass
class Resolution:
    """Result of resolving one question against session memory."""
    original_question: str
    resolved_question: str
    followup: bool = False
    referenced_entities: list[str] = field(default_factory=list)
    referent: str = ""       # what anaphora resolved to ("" = none)
    topic: str = ""          # active topic as seen from this question

    @classmethod
    def identity(cls, question: str) -> "Resolution":
        return cls(original_question=question, resolved_question=question)


class ContextResolver:
    """Deterministic follow-up resolution (never calls a model)."""

    def __init__(self, memory: "SessionMemory"):
        self.memory = memory

    # ── Entity helpers ───────────────────────────────────────────────────

    def referenced_entities(self, question: str) -> list[str]:
        q_alias = alias(question)
        if not q_alias:
            return []
        out: list[str] = []
        for name in self.memory.entity_candidates():
            a = alias(name)
            if len(a) >= 3 and a in q_alias and name not in out:
                out.append(name)
        return out

    @staticmethod
    def is_followup(question: str, has_state: bool) -> bool:
        if not has_state:
            return False
        norm = normalize(question)
        if not norm:
            return False
        words = norm.split()
        if words and words[0] in _LEAD_PRONOUNS:
            return True
        if _LEAD_RE.match(question.strip()):
            return True
        if _DEMONSTRATIVE.search(norm):
            return True
        return bool(_ANAPHORA.search(norm))

    # ── Resolution ───────────────────────────────────────────────────────

    def resolve(self, question: str) -> Resolution:
        try:
            return self._resolve(question)
        except Exception:
            return Resolution.identity(question)

    def _resolve(self, question: str) -> Resolution:
        original = (question or "").strip()
        has_state = bool(self.memory.turns or self.memory.active_topic)
        followup = self.is_followup(original, has_state)
        referenced = self.referenced_entities(original)
        # Explicit tech mentions in the question are first-class entities
        # (faster-whisper, node.js, …) even without a profile row.
        known = {alias(r) for r in referenced}
        for token in extract_tech_tokens(original):
            if alias(token) not in known:
                referenced.append(token)
                known.add(alias(token))

        referent = ""
        if referenced:
            referent = referenced[0]
        elif followup:
            referent = self.memory.active_topic

        resolved = original
        if followup and referent:
            resolved = self._substitute(original, referent)

        topic = referenced[0] if referenced else self.memory.active_topic

        return Resolution(
            original_question=original,
            resolved_question=resolved or original,
            followup=followup,
            referenced_entities=referenced,
            referent=referent,
            topic=topic,
        )

    @staticmethod
    def _substitute(question: str, referent: str) -> str:
        """Replace the first anaphoric cue with the referent."""
        m = _DEMONSTRATIVE.search(question) or _ANAPHORA.search(question) \
            or _LEAD_RE.match(question.strip())
        if not m:
            return question
        start, end = m.span()
        return question[:start] + referent + question[end:]


# ── Turn + memory ──────────────────────────────────────────────────────────

@dataclass
class Turn:
    """One answered exchange. ``ts`` is wall clock for storage only —
    never used for profiling or ordering inside a live session."""
    question: str
    answer: str
    resolved_question: str = ""
    topic: str = ""
    entities: list[str] = field(default_factory=list)
    followup: bool = False
    ts: float = 0.0


class SessionMemory:
    """Bounded conversation state for one live interview session."""

    MAX_ENTITIES = 32

    def __init__(
        self,
        session_id: Optional[int] = None,
        *,
        max_turns: int = 6,
        summary_chars: int = 700,
        recent_turns: int = 3,
        entities: Optional[list[str]] = None,
        cache: Optional["RetrievalCache"] = None,
    ):
        self.session_id = session_id
        self.max_turns = max(1, int(max_turns))
        self.summary_chars = max(120, int(summary_chars))
        self.recent_turns = max(1, int(recent_turns))
        self.turns: list[Turn] = []
        self.active_topic = ""
        self.important_entities: list[str] = []
        self.compact_summary = ""
        self.cache = cache if cache is not None else RetrievalCache()
        if entities:
            self.add_entities(entities)

    # ── Resolution ───────────────────────────────────────────────────────

    def resolve(self, question: str) -> Resolution:
        return ContextResolver(self).resolve(question)

    def entity_candidates(self) -> list[str]:
        out = list(self.important_entities)
        for t in self.turns:
            if t.topic and t.topic not in out:
                out.append(t.topic)
            for e in t.entities:
                if e and e not in out:
                    out.append(e)
        return out

    def add_entities(self, names) -> None:
        for name in names or []:
            name = (name or "").strip()
            if not name:
                continue
            a = alias(name)
            if any(alias(existing) == a for existing in self.important_entities):
                continue
            self.important_entities.append(name)
        if len(self.important_entities) > self.MAX_ENTITIES:
            self.important_entities = self.important_entities[-self.MAX_ENTITIES:]

    # ── Turn recording ───────────────────────────────────────────────────

    def record_turn(self, turn: Turn) -> None:
        self.turns.append(turn)
        if len(self.turns) > self.max_turns:
            self.turns = self.turns[-self.max_turns:]
        if turn.topic:
            self.active_topic = turn.topic
        # Learn tech names that appeared in the answer for later matching.
        self.add_entities(
            list(turn.entities)
            + ([turn.topic] if turn.topic else [])
            + extract_tech_tokens(turn.answer)
        )
        self.rebuild_summary()

    def rebuild_summary(self) -> str:
        """Deterministic compact summary of the conversation so far."""
        lines: list[str] = []
        if self.active_topic:
            lines.append("Active topic: %s" % _one_line(self.active_topic))
        for i, t in enumerate(self.turns, 1):
            lines.append("%d. Q: %s" % (i, _clip(t.question, 160)))
            lines.append("   A: %s" % _clip(t.answer, 200))
        if self.important_entities:
            lines.append("Entities: " + ", ".join(
                _clip(e, 40) for e in self.important_entities[:12]))
        text = "\n".join(lines)
        if len(text) > self.summary_chars:
            text = text[: self.summary_chars - 1].rstrip() + "…"
        self.compact_summary = text
        return text

    # ── Prompt blocks ────────────────────────────────────────────────────

    def summary_block(self) -> str:
        return self.compact_summary or "(none)"

    def conversation_block(self) -> str:
        recent = self.turns[-self.recent_turns:]
        if not recent:
            return "(none)"
        lines: list[str] = []
        for t in recent:
            lines.append("- Q: %s" % _clip(t.question, 160))
            lines.append("  A: %s" % _clip(t.answer, 240))
        return "\n".join(lines)


# ── Retrieval cache ────────────────────────────────────────────────────────

@dataclass
class _CacheEntry:
    topic_key: str
    query: str
    context: str
    metrics: dict
    at: float


class RetrievalCache:
    """Per-session RAG cache: topic-scoped, TTL'd, similarity-checked."""

    def __init__(
        self,
        ttl_s: float = 120.0,
        max_entries: int = 16,
        hit_sim: float = 0.85,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.ttl_s = float(ttl_s)
        self.max_entries = max(1, int(max_entries))
        self.hit_sim = float(hit_sim)
        self._clock = clock
        self._entries: list[_CacheEntry] = []

    def get(self, topic: str, query: str) -> Optional[tuple[str, dict]]:
        """Return (context, metrics) on a topic+query match, else None."""
        if not query:
            return None
        now = self._clock()
        self._entries = [e for e in self._entries if now - e.at <= self.ttl_s]
        topic_key = alias(topic or "")
        q_norm = normalize(query)
        for entry in reversed(self._entries):  # newest first
            if entry.topic_key != topic_key:
                continue  # topic changed → never reuse across topics
            if normalize(entry.query) == q_norm or \
                    similarity(entry.query, query) >= self.hit_sim:
                metrics = dict(entry.metrics)
                metrics["cache_hit"] = True
                return entry.context, metrics
        return None

    def put(self, topic: str, query: str, context: str, metrics: Optional[dict]) -> None:
        if not query:
            return
        topic_key = alias(topic or "")
        q_key = alias(query)
        self._entries = [
            e for e in self._entries
            if not (e.topic_key == topic_key and alias(e.query) == q_key)
        ]
        self._entries.append(_CacheEntry(
            topic_key=topic_key,
            query=query,
            context=context or "",
            metrics=dict(metrics or {}),
            at=self._clock(),
        ))
        while len(self._entries) > self.max_entries:
            self._entries.pop(0)

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)


# ── Context / prompt assembly ──────────────────────────────────────────────

_ANSWER_REQUIREMENTS = """\
- Answer in first person as the candidate ("I built…"); never say
  "according to the resume", "the candidate", or "based on the documents".
- Ground every personal claim in RELEVANT KNOWLEDGE or PREVIOUS CONTEXT;
  never invent projects, technologies, metrics, or experience.
- If CURRENT QUESTION reaches back to an earlier turn, answer the referent
  using ACTIVE TOPIC and RECENT CONVERSATION, and stay consistent with the
  answers already given this session.
- Plain speakable text only: no headings, citations, or meta commentary.
  Follow the length rules from the system prompt."""


class ContextBuilder:
    """Assembles the sectioned user prompt for the realtime answer path.

    Split into :meth:`memory_sections` (context/side-state assembly — timed as
    the *context* stage) and :meth:`assemble` (final prompt composition —
    timed as the *prompt* stage); :meth:`build` is the one-shot convenience.
    """

    @staticmethod
    def memory_sections(memory: Optional["SessionMemory"],
                        resolution: Optional[Resolution]) -> dict[str, str]:
        """Bodies for ACTIVE TOPIC / PREVIOUS CONTEXT / RECENT CONVERSATION."""
        try:
            memory = memory or SessionMemory()
            resolution = resolution or Resolution.identity("")
            topic = (resolution.referent or resolution.topic
                     or memory.active_topic or "(none yet)")
            return {
                "ACTIVE TOPIC": topic,
                "PREVIOUS CONTEXT": memory.summary_block(),
                "RECENT CONVERSATION": memory.conversation_block(),
            }
        except Exception:
            return {
                "ACTIVE TOPIC": "(none yet)",
                "PREVIOUS CONTEXT": "(none)",
                "RECENT CONVERSATION": "(none)",
            }

    @staticmethod
    def assemble(
        *,
        question: str,
        candidate_name: str,
        context: str,
        memory: Optional["SessionMemory"],
        resolution: Optional[Resolution] = None,
        sections: Optional[dict[str, str]] = None,
        analysis: Optional[QuestionAnalysis] = None,
    ) -> str:
        try:
            memory = memory or SessionMemory()
            resolution = resolution or Resolution.identity(question)
            sections = sections or ContextBuilder.memory_sections(memory, resolution)
            # Knowledge budget: retrieval already caps, this enforces it too.
            cap = settings.realtime_max_context_chars
            if context and len(context) > cap:
                context = context[:cap].rstrip()
            current = resolution.original_question or question
            if resolution.followup and resolution.resolved_question != current:
                current_block = "%s\n(resolved: %s)" % (
                    current, resolution.resolved_question)
            else:
                current_block = current
            ordered = [
                ("CANDIDATE", candidate_name or "the candidate"),
                ("CURRENT QUESTION", current_block or "(none)"),
                ("ACTIVE TOPIC", sections.get("ACTIVE TOPIC", "(none yet)")),
                ("PREVIOUS CONTEXT", sections.get("PREVIOUS CONTEXT", "(none)")),
                ("RELEVANT KNOWLEDGE",
                 (context or "").strip() or "(no retrieved context)"),
                ("RECENT CONVERSATION",
                 sections.get("RECENT CONVERSATION", "(none)")),
                ("ANSWER REQUIREMENTS", _ANSWER_REQUIREMENTS),
            ]
            # Strict coding mode: appended ONLY when requires_code is True, so
            # behavioral/project/technical questions keep the humanized format
            # and the prompt is never overloaded with coding instructions.
            block = coding_prompt_block(analysis)
            if block:
                ordered.append(("ANSWER FORMAT & TASK", block))
            return "\n\n".join(f"{label}:\n{body}" for label, body in ordered)
        except Exception:
            # Degrade to the minimal legacy prompt — never fail an answer.
            name = candidate_name or "the candidate"
            block = (f"\n\nCandidate context (use only if directly relevant):\n{context}"
                     if context else "")
            return (f"Interviewer question: {question}\n"
                    f"Candidate: {name}{block}\n\nGive the answer.")

    @staticmethod
    def build(
        *,
        question: str,
        candidate_name: str,
        context: str,
        memory: Optional["SessionMemory"],
        resolution: Optional[Resolution] = None,
        analysis: Optional[QuestionAnalysis] = None,
    ) -> str:
        """One-shot: memory sections + full sectioned prompt."""
        sections = ContextBuilder.memory_sections(memory, resolution)
        return ContextBuilder.assemble(
            question=question,
            candidate_name=candidate_name,
            context=context,
            memory=memory,
            resolution=resolution,
            sections=sections,
            analysis=analysis,
        )
