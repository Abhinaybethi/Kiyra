"""Phase-3 tests: session memory, follow-up resolution, retrieval cache,
sectioned context builder, prompt rules, failure-safety, persistence and
[PERF] stage profiling for the realtime path.

Maps to the Phase-3 verification plan (TEST 1–14).
"""
from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import numpy as np
import pytest

import realtime.session as session_mod
from config import settings
from db.models import (
    CandidateProfile,
    InterviewSession,
    Project,
    SessionMemoryState,
    SessionRetrieval,
    SessionTurn,
    Skill,
    User,
)
from realtime.memory import (
    ContextBuilder,
    Resolution,
    RetrievalCache,
    SessionMemory,
    Turn,
    alias,
    extract_tech_tokens,
)
from realtime.perf import DURATIONS, STAGES
from realtime.prompts import realtime_system
from realtime.question_detector import QuestionDetector
from realtime.transcript import TranscriptBuffer
from realtime.session import EventType, LiveSession, RealtimeState


# ── Fakes (mirrors test_session_streaming) ─────────────────────────────────

class FakeWebSocket:
    def __init__(self):
        self.sent: list[dict] = []
        self.closed = False

    async def send_text(self, text: str):
        self.sent.append(json.loads(text))

    async def close(self):
        self.closed = True


class FakeProvider:
    model_name = "test-model"
    error = None
    stream_tokens = ["first ", "second ", "third ", "fourth "]
    token_delay = 0.005

    async def _stream(self):
        for tok in self.stream_tokens:
            await asyncio.sleep(self.token_delay)
            yield tok

    async def complete(self, messages, stream=False, **kwargs):
        if self.error:
            raise self.error
        if stream:
            return self._stream()
        return "".join(self.stream_tokens)


def _make_session_row():
    return SimpleNamespace(
        id=7,
        mode="realtime",
        profile=SimpleNamespace(id=3, name="Test Candidate"),
    )


def make_session(provider, db=None, session_row=None):
    session = session_row or _make_session_row()
    if db is None:
        db = SimpleNamespace(
            add=lambda *a, **k: None,
            commit=lambda *a, **k: None,
            query=lambda *a, **k: [_ for _ in []],
            filter_by=lambda *a, **k: None,
        )
    sess = LiveSession.__new__(LiveSession)
    sess.ws = FakeWebSocket()
    sess.session = session
    sess.db = db
    sess.provider = provider
    sess._running = True
    sess._state = RealtimeState.IDLE
    sess._pending_question = None
    sess._frame_queue = asyncio.Queue(maxsize=64)
    sess._receiver = object()
    sess._transcripts = TranscriptBuffer()
    sess._detector = QuestionDetector()
    sess._request_seq = 0
    sess._current_request_id = None
    sess._answer_task = None
    sess._prefetch_task = None
    sess._prefetch_question = None
    sess._prefetch_context = None
    sess._last_analysis = None
    sess._active_project_id = None
    sess._partial_task = None
    sess._last_partial_speech = 0.0
    sess._partial_count = 0
    sess._spec_task = None
    sess._spec_question = None
    sess._spec_final_question = None
    sess._spec_request_id = None
    sess._spec_released = False
    sess._spec_validated = False
    sess._spec_similarity = 0.0
    sess._spec_tokens = []
    sess._spec_sent = 0
    sess._spec_early_released = False
    sess._questions_detected = 0
    sess._answers_generated = 0
    sess._stt_count = 0
    sess._dropped_frames = 0
    sess._start_time = 0.0
    sess._audio_worker_task = None
    sess._last_queue_log = 0.0
    sess._last_queue_delay_ms = 0.0
    sess._budget_warns = {}
    return sess


def event_types(ws) -> list[str]:
    return [e["type"] for e in ws.sent]


# ── TEST 1: deterministic follow-up resolution ─────────────────────────────

def test_followup_pronoun_resolved_against_active_topic():
    mem = SessionMemory()
    mem.record_turn(Turn(
        question="Tell me about your Friday project",
        answer="I built a realtime chat system with websockets.",
        topic="Friday project",
        entities=["Friday project"],
    ))
    res = mem.resolve("Why did you choose that approach?")
    assert res.followup is True
    assert res.referent == "Friday project"
    assert "Friday project" in res.resolved_question
    assert "that approach" not in res.resolved_question.lower()
    assert res.original_question == "Why did you choose that approach?"
    # deterministic: identical input → identical resolution
    again = mem.resolve("Why did you choose that approach?")
    assert again.resolved_question == res.resolved_question
    assert again.followup is True


# ── TEST 2: abbreviation + tech-token entity matching ──────────────────────

def test_abbreviation_and_tech_token_entities():
    mem = SessionMemory(entities=["Friday"])
    res = mem.resolve("Why did F.R.I.D.A.Y. fail under load?")
    assert "Friday" in res.referenced_entities

    tech = SessionMemory().resolve(
        "How did you use faster-whisper for transcription?")
    assert tech.referenced_entities == ["faster-whisper"]
    assert tech.topic == "faster-whisper"
    assert tech.followup is False

    tokens = extract_tech_tokens(
        "I used faster-whisper with node.js on Windows")
    assert "faster-whisper" in tokens
    assert "node.js" in tokens
    assert alias("F.R.I.D.A.Y.") == "friday"


# ── TEST 3: an independent question is not a follow-up ─────────────────────

def test_independent_question_is_not_followup():
    mem = SessionMemory(entities=["Friday"])
    mem.record_turn(Turn(
        question="Tell me about Friday",
        answer="A realtime chat app.",
        topic="Friday",
    ))
    res = mem.resolve("What is a thread in operating systems?")
    assert res.followup is False
    assert res.resolved_question == res.original_question
    assert res.referent == ""


def test_expletive_it_is_not_a_followup_and_is_never_rewritten():
    """Regression: "What is RAG and how does **it** work in general?" uses
    "it" as a dummy subject, not as a reference back to the previous turn.
    The old rule flagged it as a follow-up and rewrote it with the active
    topic — "…how does project and work in general?" — so the model answered
    a generic question with the previous project answer."""
    mem = SessionMemory(entities=["Friday"])
    mem.record_turn(Turn(
        question="Can you explain my project?",
        answer="I used FastAPI for the backend.",
        topic="FastAPI",
    ))

    res = mem.resolve("What is RAG and how does it work in general?")
    assert res.followup is False
    assert res.referent == ""
    assert res.resolved_question == res.original_question
    assert "FastAPI" not in res.resolved_question
    # A self-contained question must not inherit the previous topic either.
    assert res.topic == ""

    # …but a genuine referential follow-up still resolves.
    real = mem.resolve("Why did you choose it?")
    assert real.followup is True
    assert real.referent == "FastAPI"
    assert "FastAPI" in real.resolved_question


def test_pronoun_reaches_its_own_in_question_subject_not_the_old_topic():
    """Regression: "…and how do you refresh **it**?" after "the access token"
    refers to the token, but the resolver overwrote "it" with the previous
    session topic ("node.js"), so the whole question and answer veered onto
    the wrong subject."""
    mem = SessionMemory(entities=["node.js"])
    mem.record_turn(Turn(
        question="write a node.js function that reads a JSON file",
        answer="I built a Node.js function.",
        topic="node.js",
    ))

    res = mem.resolve("How long should the access token expiry be "
                      "and how do you refresh it?")
    assert res.followup is True
    assert res.referent == "", (
        "'it' must resolve inside the question, not steal the old topic")
    assert res.resolved_question == res.original_question
    assert "node.js" not in res.resolved_question

    # A bare pronoun with no in-question subject still reaches back.
    bare = mem.resolve("Why did you choose it?")
    assert bare.referent == "node.js"
    assert "node.js" in bare.resolved_question


def test_topic_fallback_never_ends_on_a_connector():
    """Regression: the fallback label kept connectors mid-question and
    produced "project and" / "rag and", which read as a broken referent."""
    fallback = session_mod._topic_fallback
    assert fallback(
        "Can you explain my project? and tell me what architecture you used. "
        "and why you selected Fast API for the backend?"
    ) == "project"
    assert fallback("What is rag and how does it work in general?") == "rag"
    # The documented example in the docstring must keep working.
    assert fallback("Can you explain how the retry path works?") == \
        "retry path works"
    assert fallback("Tell me about the retry path") == "retry path"


# ── TEST 4: profile entities from DB + tech tokens as candidates ───────────

def test_profile_entities_loaded_from_db(db_session):
    user = db_session.query(User).filter_by(username="local").first()
    profile = CandidateProfile(user_id=user.id, name="Jane Doe")
    db_session.add(profile)
    db_session.commit()
    db_session.add_all([
        Project(profile_id=profile.id, name="Friday",
                description="realtime chat system"),
        Skill(profile_id=profile.id, name="Python"),
    ])
    db_session.commit()

    sess = make_session(
        FakeProvider(),
        db=db_session,
        session_row=SimpleNamespace(
            id=1, mode="live",
            profile=SimpleNamespace(id=profile.id, name="Jane Doe"),
        ),
    )
    names = sess._load_profile_entities()
    assert "Friday" in names and "Python" in names

    mem = sess._get_memory()
    assert any(alias(n) == "friday" for n in mem.entity_candidates())
    res = mem.resolve("Tell me about F.R.I.D.A.Y")
    assert res.referenced_entities == ["Friday"]
    assert res.followup is False


# ── TEST 5: active-topic switch + follow-up reuses the latest topic ────────

def test_active_topic_switch_then_followup_reuses_latest():
    mem = SessionMemory(entities=["Friday", "faster-whisper"])

    r1 = mem.resolve("Tell me about your Friday project")
    assert r1.referenced_entities == ["Friday"]
    mem.record_turn(Turn(question=r1.original_question,
                         answer="I built a realtime chat system.",
                         topic=r1.topic, entities=r1.referenced_entities))
    assert mem.active_topic == "Friday"

    r2 = mem.resolve("How did you use faster-whisper there?")
    assert r2.referenced_entities == ["faster-whisper"]
    mem.record_turn(Turn(question=r2.original_question,
                         answer="It cut transcription latency in half.",
                         topic=r2.topic, entities=r2.referenced_entities))
    assert mem.active_topic == "faster-whisper"

    r3 = mem.resolve("Why did you choose it?")
    assert r3.followup is True
    assert r3.referent == "faster-whisper"
    assert "faster-whisper" in r3.resolved_question


# ── TEST 6: compact summary deterministic + bounded ────────────────────────

def test_compact_summary_deterministic_and_bounded():
    mem = SessionMemory(max_turns=4, summary_chars=1200)
    for i in range(10):
        mem.record_turn(Turn(
            question=f"Question number {i} about the project?",
            answer=f"Answer {i}. " + "detail " * 60,
            topic="Friday",
        ))
    assert len(mem.turns) == 4
    first = mem.rebuild_summary()
    second = mem.rebuild_summary()
    assert first == second  # deterministic
    assert "Active topic: Friday" in first
    assert "Question number 9" in first  # latest turns survive
    assert "Question number 5" not in first  # old turns dropped

    small = SessionMemory(max_turns=4, summary_chars=300)
    for i in range(10):
        small.record_turn(Turn(
            question=f"Question number {i} about the project?",
            answer=f"Answer {i}. " + "detail " * 60,
            topic="Friday",
        ))
    assert len(small.compact_summary) <= 300


# ── TEST 7: retrieval cache hit (same topic) skips retrieval ───────────────

@pytest.mark.asyncio
async def test_retrieval_cache_hit_same_topic_skips_retrieval(monkeypatch):
    sess = make_session(FakeProvider())
    calls = {"n": 0}

    def fake_retrieve(*args, **kwargs):
        calls["n"] += 1
        return "[PROJECT]: friday realtime chat", {"chunks": 1, "error": None}

    monkeypatch.setattr(session_mod, "retrieve_context_sync", fake_retrieve)
    res = Resolution(original_question="How did you reduce latency?",
                     resolved_question="How did you reduce latency?",
                     topic="Friday")

    ctx1, note1 = await sess._bounded_rag("How did you reduce latency?",
                                          resolution=res)
    ctx2, note2 = await sess._bounded_rag("how did you reduce latency",
                                          resolution=res)
    assert note1 == "query"
    assert note2 == "cache"
    assert ctx1 == ctx2
    assert calls["n"] == 1


# ── TEST 8: cache misses on topic change / dissimilar query ────────────────

@pytest.mark.asyncio
async def test_retrieval_cache_misses_on_topic_change_and_query_change(
        monkeypatch):
    sess = make_session(FakeProvider())
    calls = {"n": 0}

    def fake_retrieve(*args, **kwargs):
        calls["n"] += 1
        return "[PROJECT]: friday realtime chat", {"chunks": 1, "error": None}

    monkeypatch.setattr(session_mod, "retrieve_context_sync", fake_retrieve)
    friday = Resolution(original_question="How did you reduce latency?",
                        resolved_question="How did you reduce latency?",
                        topic="Friday")

    await sess._bounded_rag("How did you reduce latency?", resolution=friday)
    assert calls["n"] == 1

    # same query, different topic → never reused
    hiring = Resolution(original_question="How did you reduce latency?",
                        resolved_question="How did you reduce latency?",
                        topic="Hiring process")
    _, note = await sess._bounded_rag("How did you reduce latency?",
                                      resolution=hiring)
    assert note == "query"
    assert calls["n"] == 2

    # same topic, dissimilar query → fresh retrieval
    _, note = await sess._bounded_rag(
        "What is your greatest weakness?", resolution=friday)
    assert note == "query"
    assert calls["n"] == 3


# ── TEST 9: cache TTL expiry ───────────────────────────────────────────────

def test_retrieval_cache_ttl_expiry():
    clock = {"now": 1000.0}
    cache = RetrievalCache(ttl_s=30.0, clock=lambda: clock["now"])
    cache.put("Friday", "How did you reduce latency?", "CTX", {"chunks": 1})
    assert cache.get("Friday", "How did you reduce latency?") is not None
    clock["now"] += 29
    assert cache.get("Friday", "How did you reduce latency?") is not None
    clock["now"] += 2  # past the TTL
    assert cache.get("Friday", "How did you reduce latency?") is None
    assert len(cache) == 0  # expired entry pruned


# ── TEST 10: context builder sections + knowledge cap + resolution shown ───

def test_previous_answer_appears_at_most_once_in_the_prompt():
    """Regression: an earlier answer used to be inlined TWICE (PREVIOUS
    CONTEXT via the compact summary *and* RECENT CONVERSATION). An 8B model
    anchored on the repeated text and restated or continued the previous
    answer instead of answering the new question."""
    marker = "ZZUNIQUEANSWERTOKENZZ built the ingestion pipeline with Kafka"
    mem = SessionMemory(entities=["Friday"])
    mem.record_turn(Turn(question="Explain your ingestion pipeline",
                         answer=marker, topic="ingestion"))
    prompt = ContextBuilder.build(
        question="What are the advantages of that choice?",
        candidate_name="Jane Doe",
        context="",
        memory=mem,
        resolution=Resolution.identity("What are the advantages of that choice?"),
    )
    assert prompt.count(marker) == 1, (
        "an earlier answer must be present exactly once, as reference")

    # PREVIOUS CONTEXT carries the question trail, not the answer body.
    previous = prompt.split("PREVIOUS CONTEXT:\n", 1)[1].split("\n\n", 1)[0]
    assert marker not in previous
    assert "Explain your ingestion pipeline" in previous

    # RECENT CONVERSATION keeps it, labelled as reference only.
    recent = prompt.split("RECENT CONVERSATION:\n", 1)[1].split("\n\n", 1)[0]
    assert marker in recent
    assert "reference only" in recent

    # The rules must forbid reuse without inviting consistency-by-copying.
    assert "never reuse" in prompt
    assert "start from the beginning" in prompt


def test_prompt_rules_do_not_encourage_restating_earlier_answers():
    """The old wording ("stay consistent with the answers already given")
    actively invited the model to copy its previous answer."""
    from realtime.memory import _ANSWER_REQUIREMENTS
    rules = _ANSWER_REQUIREMENTS
    assert "stay consistent with the answers already given" not in rules
    assert "Stay factually consistent" in rules
    assert "never reuse, quote, paraphrase, or continue them" in rules

    system = realtime_system("short")
    assert "stay consistent with the answers you already gave" not in system
    assert "never restate or continue an earlier answer" in system


def test_context_builder_sections_and_knowledge_cap():
    mem = SessionMemory(entities=["Friday"])
    mem.record_turn(Turn(question="Tell me about Friday",
                         answer="Realtime chat app.", topic="Friday"))
    res = mem.resolve("Why did you choose that approach?")
    big_context = "[PROJECT]: " + "x" * 3000
    prompt = ContextBuilder.build(
        question=res.original_question,
        candidate_name="Jane Doe",
        context=big_context,
        memory=mem,
        resolution=res,
    )
    for label in ("CANDIDATE:", "CURRENT QUESTION:", "ACTIVE TOPIC:",
                  "PREVIOUS CONTEXT:", "RELEVANT KNOWLEDGE:",
                  "RECENT CONVERSATION:", "ANSWER REQUIREMENTS:"):
        assert label in prompt, label

    knowledge = prompt.split("RELEVANT KNOWLEDGE:\n", 1)[1].split("\n\n", 1)[0]
    assert len(knowledge) <= settings.realtime_max_context_chars

    previous = prompt.split("PREVIOUS CONTEXT:\n", 1)[1].split("\n\n", 1)[0]
    assert len(previous) <= mem.summary_chars

    assert "Jane Doe" in prompt
    assert res.followup and "(resolved: " in prompt
    assert "Friday" in prompt  # active topic surfaced


# ── TEST 11: prompt rules — first person, follow-ups, grounding ────────────

def test_system_prompt_followup_voice_grounding_rules():
    system = realtime_system("short")
    assert "first person" in system
    assert "FOLLOW-UPS" in system
    assert "PREVIOUS CONTEXT" in system
    assert "RECENT CONVERSATION" in system
    assert "NEVER invent" in system
    assert "Under 180 words" in system

    prompt = ContextBuilder.build(
        question="Why did you choose that approach?",
        candidate_name="Jane Doe",
        context="",
        memory=None,
        resolution=None,
    )
    assert "first person" in prompt
    # Still asks for continuity, but no longer phrases it as "stay consistent
    # with the answers you already gave", which invited the model to restate
    # a previous answer instead of writing a fresh one.
    assert "factually consistent" in prompt
    assert "stay consistent" not in prompt
    assert "never contradict" in system
    assert "according to the resume" in prompt  # …as a prohibition


def test_system_prompt_coding_override_only_when_requires_code():
    plain = realtime_system("short")
    coding = realtime_system("short", requires_code=True)
    assert "CODING QUESTION OVERRIDE" not in plain
    assert "CODING QUESTION OVERRIDE" in coding
    assert "MUST contain a fenced code block" in coding
    assert "plain text only" in coding


# ── TEST 12: answer survives retrieval + persistence failures ──────────────

@pytest.mark.asyncio
async def test_answer_survives_retrieval_and_persist_failures(
        monkeypatch, capsys):
    class BrokenDB:
        def query(self, *args, **kwargs):
            raise RuntimeError("db exploded")

        def add(self, *args, **kwargs):
            raise RuntimeError("db exploded")

        def commit(self, *args, **kwargs):
            raise RuntimeError("db exploded")

        def rollback(self, *args, **kwargs):
            pass

    def boom(*args, **kwargs):
        raise RuntimeError("embedding exploded")

    monkeypatch.setattr(session_mod, "retrieve_context_sync", boom)
    sess = make_session(FakeProvider())
    sess.db = BrokenDB()

    await sess._trigger_question("can you explain closures", 0.9, -1)
    await asyncio.sleep(0.4)

    assert EventType.ANSWER_GENERATED.value in event_types(sess.ws)
    out = capsys.readouterr().out
    assert "[RAG] retrieval failed" in out
    assert "[MEMORY] restore failed" in out
    assert "[MEMORY] persist failed" in out
    assert sess._state == RealtimeState.LISTENING


# ── TEST 13: turn + memory + retrieval persisted, restored on resume ───────

@pytest.mark.asyncio
async def test_turn_memory_and_retrieval_persisted_and_restored(monkeypatch,
                                                                db_session):
    user = db_session.query(User).filter_by(username="local").first()
    profile = CandidateProfile(user_id=user.id, name="Jane Doe")
    db_session.add(profile)
    db_session.commit()
    db_session.add(Project(profile_id=profile.id, name="Friday",
                           description="realtime chat system"))
    db_session.commit()
    row = InterviewSession(profile_id=profile.id, mode="live")
    db_session.add(row)
    db_session.commit()

    monkeypatch.setattr(
        session_mod, "retrieve_context_sync",
        lambda *a, **k: ("[PROJECT]: friday realtime chat",
                         {"chunks": 1, "error": None}),
    )
    sess = make_session(FakeProvider(), db=db_session, session_row=row)
    await sess._trigger_question("Why did you use faster-whisper?", 0.9, -1)
    await asyncio.sleep(0.4)
    assert EventType.ANSWER_GENERATED.value in event_types(sess.ws)

    turns = db_session.query(SessionTurn).filter_by(session_id=row.id).all()
    assert len(turns) == 1
    turn = turns[0]
    assert turn.question == "Why did you use faster-whisper?"
    assert turn.answer
    assert turn.topic == "faster-whisper"
    assert "faster-whisper" in (turn.entities or [])

    state = db_session.query(SessionMemoryState).filter_by(
        session_id=row.id).first()
    assert state is not None
    assert state.active_topic == "faster-whisper"
    assert state.compact_summary

    retrievals = db_session.query(SessionRetrieval).filter_by(
        session_id=row.id).all()
    assert len(retrievals) == 1
    assert retrievals[0].context

    # Resume: a fresh session object restores topic + turns from the DB.
    resumed = make_session(FakeProvider(), db=db_session, session_row=row)
    mem = resumed._get_memory()
    assert mem.active_topic == "faster-whisper"
    assert len(mem.turns) == 1
    assert mem.turns[0].question == "Why did you use faster-whisper?"


# ── TEST 14: [PERF] stages recorded for the full voice path ────────────────

@pytest.mark.asyncio
async def test_perf_stages_recorded_for_voice_path(monkeypatch, capsys):
    sess = make_session(FakeProvider())
    monkeypatch.setattr(
        session_mod, "retrieve_context_sync",
        lambda *a, **k: ("[PROJECT]: friday chat", {"chunks": 1, "error": None}),
    )

    class FakeResult:
        text = "Can you explain how React hooks work under the hood?"
        duration = 1.0
        latency_ms = 42.0

    monkeypatch.setattr(session_mod, "transcribe_with_lock",
                        lambda *a, **k: FakeResult())
    # Keep detection deterministic — this test is about timing stages.
    sess._detector = SimpleNamespace(detect=lambda text: (True, 0.95))

    sess._vad_end_mono = time.monotonic() - 0.35  # audio ended 350ms ago
    utt = SimpleNamespace(audio=np.zeros(16000, dtype=np.float32),
                          duration=1.0, start_ts=time.time())
    await sess._handle_utterance(utt, time.monotonic())
    await asyncio.sleep(0.5)

    assert EventType.ANSWER_GENERATED.value in event_types(sess.ws)
    perf = sess._perf
    assert perf is not None
    report = perf.report()

    missing = [stage for stage, val in report["stages"].items()
               if val is None]
    assert missing == [], "stages not recorded: %s" % missing
    for key in DURATIONS:
        assert report["durations"][key] is not None, key

    assert report["stages"]["audio_end"] < 50  # t0 == audio end
    assert report["stages"]["vad_finalization"] >= 300  # trailing-silence wait
    assert set(report["stages"]) == set(STAGES)

    out = capsys.readouterr().out
    assert "[PERF] request=" in out
    assert "\ntotal=" in out or "total=" in out
    assert "stages: audio_end=" in out
