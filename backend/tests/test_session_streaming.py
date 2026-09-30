"""Behavior tests for the real-time session state machine (streaming + cancel)."""
import asyncio
import json
from types import SimpleNamespace

import numpy as np
import pytest

import realtime.session as session_mod
from realtime.session import (
    EventType,
    LiveSession,
    RealtimeState,
)
from realtime.question_detector import QuestionDetector
from realtime.transcript import TranscriptBuffer
from ai.provider import ProviderUnavailableError


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


class StubReceiver:
    speech_seconds = 0.0

    def __init__(self):
        self.voiced = False

    def snapshot(self):
        return np.zeros(16000, dtype=np.float32)


def make_session(provider):
    ws = FakeWebSocket()
    session = SimpleNamespace(
        id=7,
        mode="realtime",
        profile=SimpleNamespace(id=3, name="Test Candidate"),
    )
    sess = LiveSession.__new__(LiveSession)
    sess.ws = ws
    sess.session = session
    sess.db = SimpleNamespace(
        add=lambda *a, **k: None,
        commit=lambda *a, **k: None,
        query=lambda *a, **k: [_ for _ in []],
        filter_by=lambda *a, **k: None,
    )
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


def event_types(ws: FakeWebSocket) -> list[str]:
    return [e["type"] for e in ws.sent]


def generated_events(ws: FakeWebSocket) -> list[dict]:
    return [e for e in ws.sent if e["type"] == EventType.ANSWER_GENERATED.value]


@pytest.mark.asyncio
async def test_generate_answer_ws_note_lands_in_user_prompt(monkeypatch):
    """Screenshot flow: /extract question + note → same WS pipeline → prompt.

    The OCR'd screenshot question enters the EXACT SAME path as a manual
    question (generate_answer WS message → _trigger_question → memory/RAG/
    ContextBuilder/streamed answer); the optional `note` (code format hint)
    is appended to the user prompt and never replaces the question.
    """
    provider = FakeProvider()
    sess = make_session(provider)
    monkeypatch.setattr(
        session_mod, "retrieve_context_sync",
        lambda *a, **k: ("", {"embed_ms": 1.0, "search_ms": 1.0}),
    )
    seen: dict = {}

    async def fake_complete(messages, stream=False, **kwargs):
        seen["user"] = messages[1]["content"]
        return provider._stream()

    monkeypatch.setattr(provider, "complete", fake_complete)

    await sess._handle_text_message(json.dumps({
        "type": "generate_answer",
        "payload": {
            "question": "Write a Java program to reverse a string",
            "source": "screenshot",
            "note": "SCREENSHOT_NOTE_MARKER answer with a fenced code block",
        },
    }))
    await asyncio.sleep(0.6)

    assert "SCREENSHOT_NOTE_MARKER" in seen.get("user", "")
    assert "Write a Java program to reverse a string" in seen.get("user", "")
    types = event_types(sess.ws)
    assert EventType.QUESTION_DETECTED.value in types
    assert EventType.ANSWER_GENERATED.value in types


@pytest.mark.asyncio
async def test_streaming_answer_emits_full_event_sequence(monkeypatch):
    provider = FakeProvider()
    sess = make_session(provider)
    monkeypatch.setattr(
        session_mod, "retrieve_context_sync",
        lambda *a, **k: ("", {"embed_ms": 1.0, "search_ms": 1.0}),
    )

    await sess._trigger_question("can you explain closures", 0.9, -1)
    await asyncio.sleep(0.4)

    types = event_types(sess.ws)
    assert EventType.QUESTION_DETECTED.value in types
    assert EventType.ANSWER_GENERATING.value in types
    assert EventType.ANSWER_STREAMED.value in types
    assert EventType.ANSWER_GENERATED.value in types

    generated = next(e for e in sess.ws.sent if e["type"] == EventType.ANSWER_GENERATED.value)
    assert generated["payload"]["answer"] == "first second third fourth"
    assert generated["payload"]["latency_ms"] >= 0


@pytest.mark.asyncio
async def test_new_question_cancels_stale_stream(monkeypatch):
    class SlowProvider(FakeProvider):
        token_delay = 0.05

        async def _stream(self):
            for i in range(8):  # finishes in ~0.4s if left alone
                await asyncio.sleep(self.token_delay)
                yield f"token-{i} "

    provider = SlowProvider()
    sess = make_session(provider)
    monkeypatch.setattr(
        session_mod, "retrieve_context_sync",
        lambda *a, **k: ("", {"embed_ms": 1.0, "search_ms": 1.0}),
    )

    await sess._trigger_question("question one", 0.9, -1)
    await asyncio.sleep(0.15)  # q1 generating (slow)
    await sess._trigger_question("question two", 0.9, -1)
    await asyncio.sleep(1.0)  # q2 (fast) fully streams

    ids = {}
    for e in sess.ws.sent:
        if e["type"] in (EventType.ANSWER_GENERATING.value, EventType.ANSWER_CANCELLED.value,
                         EventType.ANSWER_GENERATED.value):
            ids.setdefault(e["type"], []).append(e["payload"].get("request_id"))

    # Two distinct requests were issued
    assert len(ids[EventType.ANSWER_GENERATING.value]) == 2
    # The first generation was cancelled, the second completed
    assert EventType.ANSWER_CANCELLED.value in ids
    assert len(ids[EventType.ANSWER_CANCELLED.value]) >= 1
    assert len(ids[EventType.ANSWER_GENERATED.value]) == 1
    # Cancelled id (first) differs from the completed id (second)
    cancelled_ids = set(ids[EventType.ANSWER_CANCELLED.value])
    completed_ids = set(ids[EventType.ANSWER_GENERATED.value])
    assert cancelled_ids.isdisjoint(completed_ids)
    # Current request id resolves to the completed (latest) request
    assert sess._current_request_id in completed_ids or sess._current_request_id is None


@pytest.mark.asyncio
async def test_provider_unavailable_sends_recoverable_error(monkeypatch):
    provider = FakeProvider()
    provider.error = ProviderUnavailableError("Bonsai inference server is unavailable")
    sess = make_session(provider)
    monkeypatch.setattr(
        session_mod, "retrieve_context_sync",
        lambda *a, **k: ("", {"embed_ms": 1.0, "search_ms": 1.0}),
    )

    await sess._trigger_question("can you explain closures", 0.9, -1)
    await asyncio.sleep(0.3)

    errs = [e for e in sess.ws.sent if e["type"] == EventType.SESSION_ERROR.value]
    assert errs
    assert errs[0]["payload"]["code"] == "provider_unavailable"
    assert errs[0]["payload"]["recoverable"] is True
    # Pipeline returns to listening after the recoverable failure
    assert sess._state == RealtimeState.LISTENING


@pytest.mark.asyncio
async def test_generate_answer_text_message(monkeypatch):
    provider = FakeProvider()
    sess = make_session(provider)
    monkeypatch.setattr(
        session_mod, "retrieve_context_sync",
        lambda *a, **k: ("", {"embed_ms": 1.0, "search_ms": 1.0}),
    )

    await sess._handle_text_message(json.dumps({
        "type": "generate_answer",
        "payload": {"question": "what is a deadlock"},
    }))
    await asyncio.sleep(0.4)

    assert EventType.ANSWER_GENERATED.value in event_types(sess.ws)
    qd = next(e for e in sess.ws.sent if e["type"] == EventType.QUESTION_DETECTED.value)
    assert qd["payload"]["question"] == "what is a deadlock"


@pytest.mark.asyncio
async def test_garbage_audio_frame_sets_audio_error():
    provider = FakeProvider()
    sess = make_session(provider)
    await sess._process_frame(1.0, b"not-a-wav")
    assert sess._state == RealtimeState.AUDIO_ERROR


def _fake_partial_result(text):
    return SimpleNamespace(text=text, duration=1.0, latency_ms=10, confidence=0.9)


def _stub_utterance(seconds=1.0):
    return SimpleNamespace(
        audio=np.zeros(int(16000 * seconds), dtype=np.float32),
        duration=seconds,
        start_ts=0.0,
    )


@pytest.mark.asyncio
async def test_cutoff_partial_does_not_speculate(monkeypatch):
    """A partial that visibly cut off (trailing ellipsis) must NOT trigger
    speculation — answering a truncated question wastes a generation."""
    provider = FakeProvider()
    sess = make_session(provider)
    sess._receiver = StubReceiver()
    monkeypatch.setattr(
        session_mod, "transcribe_with_lock",
        lambda *a, **k: _fake_partial_result("What is the difference between a pro..."),
    )
    monkeypatch.setattr(
        session_mod, "retrieve_context_sync",
        lambda *a, **k: ("", {"embed_ms": 1.0, "search_ms": 1.0}),
    )

    await sess._run_partial()
    await asyncio.sleep(0.05)

    assert EventType.TRANSCRIPT_PARTIAL.value in event_types(sess.ws)
    assert EventType.QUESTION_POTENTIAL.value not in event_types(sess.ws)
    assert sess._spec_task is None
    assert EventType.ANSWER_GENERATED.value not in event_types(sess.ws)


@pytest.mark.asyncio
async def test_aggressive_early_release_fires_at_speech_end(monkeypatch):
    """When the utterance ends, a buffered speculative answer is released IMMEDIATELY
    (before the final full-utterance STT) — then confirmed once the transcript lands."""
    provider = FakeProvider()
    sess = make_session(provider)
    sess._receiver = StubReceiver()
    monkeypatch.setattr(
        session_mod, "transcribe_with_lock",
        lambda *a, **k: _fake_partial_result("What is the difference between REST and GraphQL?"),
    )
    monkeypatch.setattr(
        session_mod, "retrieve_context_sync",
        lambda *a, **k: ("", {"embed_ms": 1.0, "search_ms": 1.0}),
    )

    await sess._run_partial()
    await asyncio.sleep(0.15)
    assert sess._spec_task and not sess._spec_task.done()
    assert not sess._spec_released

    await sess._handle_utterance(_stub_utterance(), 0.0)
    await asyncio.sleep(0.4)

    assert sess._spec_early_released
    assert sess._spec_released
    assert EventType.ANSWER_STREAMED.value in event_types(sess.ws)
    # The full pipeline still ran (existing features intact)
    assert EventType.TRANSCRIPT_FINAL.value in event_types(sess.ws)
    assert EventType.QUESTION_DETECTED.value in event_types(sess.ws)
    gens = generated_events(sess.ws)
    assert len(gens) == 1
    assert gens[0]["payload"]["answer"] == "first second third fourth"


@pytest.mark.asyncio
async def test_early_released_answer_superseded_on_divergence(monkeypatch):
    """If the final transcript differs from the early-released guess, the early
    answer is superseded by a fresh authoritative generation."""
    provider = FakeProvider()
    sess = make_session(provider)
    sess._receiver = StubReceiver()
    monkeypatch.setattr(
        session_mod, "transcribe_with_lock",
        lambda *a, **k: _fake_partial_result("What is the difference between REST and GraphQL?"),
    )
    monkeypatch.setattr(
        session_mod, "retrieve_context_sync",
        lambda *a, **k: ("", {"embed_ms": 1.0, "search_ms": 1.0}),
    )

    await sess._run_partial()
    await asyncio.sleep(0.15)

    # Speech ends → early release happens inside the real handle path.
    sess._transcripts.current_utterance = "What is your favorite color?"
    sess._spec_released = True  # simulate the early release
    sess._spec_early_released = True
    await sess._analyze_transcript(_fake_partial_result("What is your favorite color?"))
    await asyncio.sleep(0.4)

    gens = generated_events(sess.ws)
    assert len(gens) == 1  # the authoritative answer
    assert gens[0]["payload"]["answer"] == "first second third fourth"
    gens_ev = [e for e in sess.ws.sent if e["type"] == EventType.ANSWER_GENERATING.value]
    assert "favorite color" in gens_ev[-1]["payload"]["question"]
    assert EventType.ANSWER_CANCELLED.value in event_types(sess.ws)


@pytest.mark.asyncio
async def test_partial_script_starts_speculation_but_hides_answer(monkeypatch):
    """A high-confidence partial question starts speculative generation and the
    generated tokens are BUFFERED — nothing reaches the UI until validation."""
    provider = FakeProvider()
    sess = make_session(provider)
    sess._receiver = StubReceiver()
    sess._detector = QuestionDetector()
    monkeypatch.setattr(
        session_mod, "transcribe_with_lock",
        lambda *a, **k: _fake_partial_result("What is the difference between REST and GraphQL"),
    )
    monkeypatch.setattr(
        session_mod, "retrieve_context_sync",
        lambda *a, **k: ("", {"embed_ms": 1.0, "search_ms": 1.0}),
    )
    monkeypatch.setattr(session_mod.settings, "realtime_spec_max_ms", 300)

    await sess._run_partial()
    await asyncio.sleep(0.08)

    types = event_types(sess.ws)
    assert EventType.TRANSCRIPT_PARTIAL.value in types
    assert EventType.QUESTION_POTENTIAL.value in types
    # Generating is signalled, but NO answer.streamed / answer.generated yet
    assert EventType.ANSWER_GENERATING.value in types
    assert EventType.ANSWER_STREAMED.value not in types
    assert EventType.ANSWER_GENERATED.value not in types
    # Even though the FakeProvider finished streaming, the completed buffer is
    # HELD, waiting for final validation (not discarded, not shown).
    assert sess._spec_task is not None and not sess._spec_task.done()
    assert not sess._spec_released
    assert len(sess._spec_tokens) == len(FakeProvider.stream_tokens)
    assert sess._spec_sent == 0

    # Never validated ⇒ abandoned quietly after the cap; nothing ever shown.
    await asyncio.sleep(0.4)
    assert sess._spec_task is None  # finished + cleaned up
    assert EventType.ANSWER_STREAMED.value not in event_types(sess.ws)
    assert EventType.ANSWER_GENERATED.value not in event_types(sess.ws)


@pytest.mark.asyncio
async def test_final_question_validates_and_releases_speculation(monkeypatch):
    """Final transcript similar to the speculative question releases the buffer:
    the pre-generated answer bursts to the UI right away."""
    provider = FakeProvider()
    sess = make_session(provider)
    sess._receiver = StubReceiver()
    monkeypatch.setattr(
        session_mod, "transcribe_with_lock",
        lambda *a, **k: _fake_partial_result("What is the difference between REST and GraphQL"),
    )
    monkeypatch.setattr(
        session_mod, "retrieve_context_sync",
        lambda *a, **k: ("", {"embed_ms": 1.0, "search_ms": 1.0}),
    )

    await sess._run_partial()
    await asyncio.sleep(0.15)  # speculation buffering
    assert sess._spec_task and not sess._spec_task.done()

    # Final transcript lands — current_utterance mirrors the full question.
    sess._transcripts.current_utterance = "What is the difference between REST and GraphQL?"
    await sess._analyze_transcript(_fake_partial_result("What is the difference between REST and GraphQL?"))
    await asyncio.sleep(0.4)  # release flush + live stream finish

    assert sess._spec_validated
    assert EventType.ANSWER_STREAMED.value in event_types(sess.ws)
    gens = generated_events(sess.ws)
    assert len(gens) == 1
    assert gens[0]["payload"]["answer"] == "first second third fourth"
    assert gens[0]["payload"].get("speculative") is True


@pytest.mark.asyncio
async def test_divergent_final_question_cancels_speculation(monkeypatch):
    """A final question that diverges from the guess kills the buffered
    speculation and generates a fresh authoritative answer instead."""
    provider = FakeProvider()
    sess = make_session(provider)
    sess._receiver = StubReceiver()
    monkeypatch.setattr(
        session_mod, "transcribe_with_lock",
        lambda *a, **k: _fake_partial_result("What is the difference between REST and GraphQL"),
    )
    monkeypatch.setattr(
        session_mod, "retrieve_context_sync",
        lambda *a, **k: ("", {"embed_ms": 1.0, "search_ms": 1.0}),
    )

    await sess._run_partial()
    await asyncio.sleep(0.15)
    assert sess._spec_task and not sess._spec_task.done()

    sess._transcripts.current_utterance = "What is your favorite color"
    await sess._analyze_transcript(_fake_partial_result("What is your favorite color"))
    await asyncio.sleep(0.4)

    assert not sess._spec_validated
    gens = generated_events(sess.ws)
    assert len(gens) == 1  # exactly one answer: the authoritative one
    # The fresh generation answered the FINAL question, not the guess
    gens_ev = [e for e in sess.ws.sent if e["type"] == EventType.ANSWER_GENERATING.value]
    assert "favorite color" in gens_ev[-1]["payload"]["question"]
    assert "REST" not in gens_ev[-1]["payload"]["question"]
    assert gens[0]["payload"]["answer"] == "first second third fourth"
    assert EventType.ANSWER_CANCELLED.value in event_types(sess.ws)


@pytest.mark.asyncio
async def test_non_question_final_cancels_speculation(monkeypatch):
    provider = FakeProvider()
    sess = make_session(provider)
    sess._receiver = StubReceiver()
    monkeypatch.setattr(
        session_mod, "transcribe_with_lock",
        lambda *a, **k: _fake_partial_result("What is the difference between REST and GraphQL"),
    )
    monkeypatch.setattr(
        session_mod, "retrieve_context_sync",
        lambda *a, **k: ("", {"embed_ms": 1.0, "search_ms": 1.0}),
    )

    await sess._run_partial()
    await asyncio.sleep(0.15)
    assert sess._spec_task and not sess._spec_task.done()

    # Final transcript is a statement, not a question → buffered answer discarded
    sess._transcripts.current_utterance = "the code uses a singleton pattern"
    await sess._analyze_transcript(_fake_partial_result("the code uses a singleton pattern"))
    await asyncio.sleep(0.3)

    assert not sess._spec_validated
    assert sess._spec_task is None or sess._spec_task.done()
    assert EventType.ANSWER_GENERATED.value not in event_types(sess.ws)
    assert EventType.ANSWER_STREAMED.value not in event_types(sess.ws)


@pytest.mark.asyncio
async def test_ingest_drops_oldest_frames_on_overflow():
    provider = FakeProvider()
    sess = make_session(provider)
    sess._frame_queue = asyncio.Queue(maxsize=4)
    for _ in range(20):
        await sess._ingest_frame(b"\x00" * 8000)
    assert sess._dropped_frames == 16
    assert sess._frame_queue.qsize() == 4