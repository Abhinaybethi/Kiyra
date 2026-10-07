"""Interview Listening Mode (Phase 5.4) — the user owns the question boundary.

The regression these tests lock down: VAD finalizes an utterance after ~300 ms
of silence and that used to become a QUESTION and trigger a generation. Now a
finalized utterance is only a *segment* appended to an open turn, and only an
explicit `turn.send` (SEND) submits.
"""
import asyncio
import json
import re
import time
from types import SimpleNamespace

import numpy as np
import pytest

import realtime.session as session_mod
from realtime.question_detector import QuestionDetector
from realtime.session import EventType, LiveSession, RealtimeState
from realtime.transcript import InterviewTranscriptBuffer, TranscriptBuffer


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

    async def _stream(self):
        for tok in ["I ", "built ", "it ", "with ", "FastAPI."]:
            await asyncio.sleep(0.005)
            yield tok

    async def complete(self, messages, stream=False, **kwargs):
        if stream:
            return self._stream()
        return "I built it with FastAPI."


class StubReceiver:
    """UtteranceReceiver double whose flush() can be armed per test."""

    speech_seconds = 0.0

    def __init__(self):
        self.voiced = False
        self.pending = None
        self.flush_calls = 0

    def snapshot(self):
        return np.zeros(16000, dtype=np.float32)

    def reset(self):
        self.voiced = False
        self.pending = None

    def flush(self):
        self.flush_calls += 1
        if not self.voiced or self.pending is None:
            return None
        utt, self.pending = self.pending, None
        self.voiced = False
        return utt


def make_utterance(text_seconds: float = 1.0):
    audio = np.ones(int(16000 * text_seconds), dtype=np.float32) * 0.05
    return SimpleNamespace(audio=audio, rms=0.05, start_ts=0.0,
                           duration=text_seconds, speech_seconds=text_seconds)


def make_session(provider=None, listening: bool = True):
    ws = FakeWebSocket()
    sess = LiveSession.__new__(LiveSession)
    sess.ws = ws
    sess.session = SimpleNamespace(
        id=7, mode="realtime",
        profile=SimpleNamespace(id=3, name="Test Candidate"))
    sess.db = SimpleNamespace(
        add=lambda *a, **k: SimpleNamespace(id=1),
        commit=lambda *a, **k: None,
        query=lambda *a, **k: SimpleNamespace(first=lambda: None),
        filter_by=lambda *a, **k: None,
    )
    sess.provider = provider or FakeProvider()
    sess._running = True
    sess._state = RealtimeState.IDLE
    sess._pending_question = None
    sess._frame_queue = asyncio.Queue(maxsize=64)
    sess._receiver = StubReceiver()
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
    sess._listening_mode = listening
    sess._listening_active = False
    sess._turn = None
    sess._turn_seq = 0
    sess._turn_submitting = False
    sess._turn_submitted_text = None
    sess._turn_start_ts = None
    sess._turn_listen_ms = 0.0
    sess._recent_questions = None
    sess._last_transcript_segment_id = None
    return sess


def stt_result(text: str):
    return SimpleNamespace(text=text, latency_ms=1.0, duration=1.0)


def events(ws):
    return [e["type"] for e in ws.sent]


def payloads(ws, type_name):
    return [e["payload"] for e in ws.sent if e["type"] == type_name]


# ─────────────────────────────────────────────────────────────────────────────
# 1-3. Continuous listening: VAD silence must NOT submit
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_vad_silence_appends_instead_of_generating(monkeypatch):
    """A finalized utterance (300 ms silence) must not generate an answer."""
    sess = make_session()
    monkeypatch.setattr(session_mod, "transcribe_with_lock",
                        lambda *a, **k: stt_result("Can you explain my project"))
    monkeypatch.setattr(session_mod, "retrieve_context_sync",
                        lambda *a, **k: ("", {"embed_ms": 1.0, "search_ms": 1.0}))

    await sess._handle_utterance(make_utterance(), 0.0)
    await asyncio.sleep(0.1)

    assert payloads(sess.ws, EventType.ANSWER_GENERATED.value) == [], \
        "VAD speech-end must never generate in listening mode"
    assert payloads(sess.ws, EventType.TURN_SUBMITTED.value) == []
    # It became a segment instead.
    segments = payloads(sess.ws, EventType.TRANSCRIPT_SEGMENT.value)
    assert len(segments) == 1
    assert segments[0]["text"] == "Can you explain my project"
    assert sess._state is RealtimeState.READY_TO_SEND


@pytest.mark.asyncio
async def test_pauses_merge_into_one_question(monkeypatch):
    """Three pauses -> three segments -> ONE question (spec  2)."""
    sess = make_session()
    pieces = [
        "Can you explain my project",
        "and tell me what architecture you used",
        "and why you selected FastAPI for the backend?",
    ]
    monkeypatch.setattr(session_mod, "transcribe_with_lock",
                        lambda *a, **k: stt_result(pieces.pop(0)))
    monkeypatch.setattr(session_mod, "retrieve_context_sync",
                        lambda *a, **k: ("", {"embed_ms": 1.0, "search_ms": 1.0}))

    for _ in range(3):
        await sess._handle_utterance(make_utterance(), 0.0)

    assert sess.turn.get_current() == (
        "Can you explain my project and tell me what architecture you used "
        "and why you selected FastAPI for the backend?")
    assert sess.turn.segment_count == 3
    assert payloads(sess.ws, EventType.ANSWER_GENERATED.value) == []


@pytest.mark.asyncio
async def test_repeated_segment_text_is_not_duplicated(monkeypatch):
    """"what is rag" twice must not become "what is what is rag what is rag"."""
    sess = make_session()
    monkeypatch.setattr(session_mod, "transcribe_with_lock",
                        lambda *a, **k: stt_result("what is rag"))

    for _ in range(3):
        await sess._handle_utterance(make_utterance(), 0.0)

    assert sess.turn.get_current() == "what is rag"
    assert sess.turn.segment_count == 1
    duplicates = [p for p in payloads(sess.ws, EventType.TRANSCRIPT_SEGMENT.value)
                  if p["duplicate"]]
    assert len(duplicates) == 2


@pytest.mark.asyncio
async def test_partial_transcript_never_becomes_the_answer(monkeypatch):
    """A partial is UI-only: it must not reach the answer path (spec  5/ 6)."""
    sess = make_session()
    monkeypatch.setattr(session_mod, "transcribe_with_lock",
                        lambda *a, **k: stt_result("Can you explain"))

    async def _partial():
        await sess._run_partial()

    sess._receiver.speech_seconds = 5.0
    sess._last_partial_speech = 0.0
    sess._receiver.voiced = True
    await _partial()
    await asyncio.sleep(0.05)

    assert payloads(sess.ws, EventType.TRANSCRIPT_PARTIAL.value), \
        "live partial should still reach the UI"
    assert payloads(sess.ws, EventType.ANSWER_GENERATED.value) == []
    assert sess.turn.get_current() == "", \
        "a partial must NOT be committed to the turn"


# ─────────────────────────────────────────────────────────────────────────────
# 4-5. SEND submits exactly once; disabled while generating
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_send_submits_exactly_one_generation(monkeypatch):
    sess = make_session()
    monkeypatch.setattr(session_mod, "transcribe_with_lock",
                        lambda *a, **k: stt_result(
                            "Can you explain my Kiyra architecture"))
    monkeypatch.setattr(session_mod, "retrieve_context_sync",
                        lambda *a, **k: ("", {"embed_ms": 1.0, "search_ms": 1.0}))
    await sess._handle_utterance(make_utterance(), 0.0)

    await sess._handle_text_message(json.dumps(
        {"type": "turn.send", "payload": {"turn_id": "t1"}}))
    await asyncio.sleep(0.5)

    submitted = payloads(sess.ws, EventType.TURN_SUBMITTED.value)
    assert len(submitted) == 1
    assert submitted[0]["question"] == "Can you explain my Kiyra architecture"
    assert len(payloads(sess.ws, EventType.ANSWER_GENERATED.value)) == 1


@pytest.mark.asyncio
async def test_repeated_send_while_generating_is_ignored(monkeypatch):
    """Spec  28: repeated clicks must not create a second generation."""
    sess = make_session()
    monkeypatch.setattr(session_mod, "transcribe_with_lock",
                        lambda *a, **k: stt_result("Why did you choose JWT"))

    release = asyncio.Event()

    async def _slow_stream():
        await release.wait()
        yield "Because it is stateless."

    async def _complete(messages, stream=False, **kwargs):
        return _slow_stream() if stream else "Because it is stateless."

    monkeypatch.setattr(sess.provider, "complete", _complete)
    monkeypatch.setattr(session_mod, "retrieve_context_sync",
                        lambda *a, **k: ("", {"embed_ms": 1.0, "search_ms": 1.0}))
    await sess._handle_utterance(make_utterance(), 0.0)

    await sess._handle_text_message(json.dumps({"type": "turn.send", "payload": {}}))
    await asyncio.sleep(0.05)
    assert payloads(sess.ws, EventType.ANSWER_GENERATING.value), "generation started"

    # Hammer SEND while the first generation is still running.
    for _ in range(4):
        await sess._handle_text_message(
            json.dumps({"type": "turn.send", "payload": {}}))

    release.set()
    await asyncio.sleep(0.4)

    assert len(payloads(sess.ws, EventType.TURN_SUBMITTED.value)) == 1, \
        "exactly one submission per SEND burst"
    assert len(payloads(sess.ws, EventType.ANSWER_GENERATED.value)) == 1, \
        "exactly one generation"
    errors = payloads(sess.ws, EventType.SESSION_ERROR.value)
    assert any(e.get("code") == "already_generating" for e in errors)


@pytest.mark.asyncio
async def test_send_with_empty_transcript_generates_nothing(monkeypatch):
    sess = make_session()
    monkeypatch.setattr(session_mod, "transcribe_with_lock",
                        lambda *a, **k: stt_result("Can you explain"))

    await sess._handle_text_message(json.dumps({"type": "turn.send", "payload": {}}))
    await asyncio.sleep(0.2)

    assert payloads(sess.ws, EventType.TURN_SUBMITTED.value) == []
    assert payloads(sess.ws, EventType.ANSWER_GENERATED.value) == []
    assert any(e.get("code") == "empty_turn"
               for e in payloads(sess.ws, EventType.SESSION_ERROR.value))
    # The turn is unfrozen so the user can keep speaking.
    assert sess.turn.is_paused is False


@pytest.mark.asyncio
async def test_send_rotates_to_a_new_turn_after_answer(monkeypatch):
    """Phase 5.4.1  5: a completed turn resets the transcript for the next one."""
    sess = make_session()
    monkeypatch.setattr(session_mod, "transcribe_with_lock",
                        lambda *a, **k: stt_result("Explain my RAG pipeline"))
    monkeypatch.setattr(session_mod, "retrieve_context_sync",
                        lambda *a, **k: ("", {"embed_ms": 1.0, "search_ms": 1.0}))

    await sess._handle_utterance(make_utterance(), 0.0)
    submitted_turn = sess._submitted_turn_id
    await sess._handle_text_message(json.dumps({"type": "turn.send", "payload": {}}))
    await asyncio.sleep(0.5)

    assert payloads(sess.ws, EventType.ANSWER_GENERATED.value), "answer happened"
    # The live transcript is clean and waiting for question 2.
    assert sess.turn.get_current() == ""
    assert sess._turn_id != submitted_turn, "a new turn must be open"
    assert sess._state is RealtimeState.LISTENING
    # …while the question itself is preserved in history.
    assert sess.question_history[-1]["question"] == "Explain my RAG pipeline"


# ─────────────────────────────────────────────────────────────────────────────
# 6-7. Final flush: bounded, non-duplicating
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_final_flush_appends_tail_without_duplicating(monkeypatch):
    """Speech in flight at SEND time is transcribed once, appended once."""
    sess = make_session()
    texts = iter(["Can you explain my project", "and why FastAPI"])
    monkeypatch.setattr(session_mod, "transcribe_with_lock",
                        lambda *a, **k: stt_result(next(texts)))
    monkeypatch.setattr(session_mod, "retrieve_context_sync",
                        lambda *a, **k: ("", {"embed_ms": 1.0, "search_ms": 1.0}))

    await sess._handle_utterance(make_utterance(), 0.0)
    # Interviewer is still talking when SEND is pressed.
    sess._receiver.voiced = True
    sess._receiver.pending = make_utterance()

    await sess._handle_text_message(json.dumps({"type": "turn.send", "payload": {}}))
    await asyncio.sleep(0.5)

    submitted = payloads(sess.ws, EventType.TURN_SUBMITTED.value)
    assert len(submitted) == 1
    question = submitted[0]["question"]
    assert question == "Can you explain my project and why FastAPI"
    assert question.count("Can you explain my project") == 1, "no duplicate tail"
    assert sess._receiver.flush_calls == 1


@pytest.mark.asyncio
async def test_final_flush_is_noop_when_nothing_pending():
    sess = make_session()
    await sess._start_listening()
    sess._receiver.voiced = False

    await sess._submit_turn(text="Why did you choose JWT?")

    # Only the explicitly supplied text is submitted.
    submitted = payloads(sess.ws, EventType.TURN_SUBMITTED.value)
    assert len(submitted) == 1
    assert submitted[0]["question"] == "Why did you choose JWT?"
    # flush() is always attempted once; it is a safe no-op with nothing buffered.
    assert sess._receiver.flush_calls == 1
    assert sess.turn.segment_count == 0, "nothing was appended"


@pytest.mark.asyncio
async def test_final_flush_timeout_is_bounded(monkeypatch):
    """A wedged STT must not hang SEND forever (spec  7)."""
    sess = make_session()
    sess._receiver.voiced = True
    sess._receiver.pending = make_utterance()

    async def _hang(*a, **k):
        await asyncio.sleep(30)

    monkeypatch.setattr(session_mod, "transcribe_with_lock", _hang)
    monkeypatch.setattr(session_mod.settings, "turn_final_flush_timeout", 0.15)

    await asyncio.wait_for(
        sess._handle_text_message(json.dumps(
            {"type": "turn.send", "payload": {"text": "Explain the retry"}})),
        timeout=3.0)

    submitted = payloads(sess.ws, EventType.TURN_SUBMITTED.value)
    assert len(submitted) == 1, "SEND completes even when the tail flush wedges"
    assert submitted[0]["question"] == "Explain the retry"


# ─────────────────────────────────────────────────────────────────────────────
# 8. Listening controls
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_start_listening_clears_the_previous_turn():
    sess = make_session()
    sess.turn.append("stale question from the last turn")
    await sess._start_listening()

    assert sess.turn.get_current() == ""
    assert sess.turn.can_send is False
    assert payloads(sess.ws, EventType.LISTENING_STARTED.value)[0]["listening"]


@pytest.mark.asyncio
async def test_stop_preserves_transcript_and_never_generates():
    sess = make_session()
    sess.turn.append("half a question")
    await sess._start_listening()
    sess.turn.append("half a question that is long enough")

    await sess._stop_listening()
    await asyncio.sleep(0.1)

    stopped = payloads(sess.ws, EventType.LISTENING_STOPPED.value)[0]
    assert stopped["generated"] is False
    assert stopped["current"] == "half a question that is long enough"
    assert payloads(sess.ws, EventType.ANSWER_GENERATED.value) == []
    assert sess.turn.is_paused is True


@pytest.mark.asyncio
async def test_mode_set_toggles_listening_mode():
    sess = make_session(listening=False)
    await sess._handle_text_message(json.dumps(
        {"type": "mode.set", "payload": {"mode": "interview_listening"}}))

    assert sess._listening_mode is True
    assert payloads(sess.ws, EventType.MODE_CHANGED.value)[0]["mode"] == \
        "interview_listening"


@pytest.mark.asyncio
async def test_legacy_auto_detect_still_generates_when_mode_is_off(monkeypatch):
    """The web client keeps its existing behaviour (no regression)."""
    sess = make_session(listening=False)
    monkeypatch.setattr(session_mod, "retrieve_context_sync",
                        lambda *a, **k: ("", {"embed_ms": 1.0, "search_ms": 1.0}))

    await sess._handle_text_message(json.dumps({
        "type": "transcript.text",
        "payload": {"text": "Can you explain how your project works?"},
    }))
    await asyncio.sleep(0.5)

    assert payloads(sess.ws, EventType.ANSWER_GENERATED.value), \
        "auto-detect mode must still answer without an explicit SEND"


# ─────────────────────────────────────────────────────────────────────────────
# 9-11. Memory / previous questions / topic panel data
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_previous_questions_and_memory_are_pushed(monkeypatch):
    sess = make_session()
    monkeypatch.setattr(session_mod, "transcribe_with_lock",
                        lambda *a, **k: stt_result("Explain my authentication"))
    monkeypatch.setattr(session_mod, "retrieve_context_sync",
                        lambda *a, **k: ("", {"embed_ms": 1.0, "search_ms": 1.0}))

    await sess._handle_utterance(make_utterance(), 0.0)
    await sess._handle_text_message(json.dumps({"type": "turn.send", "payload": {}}))
    await asyncio.sleep(0.5)

    snapshots = payloads(sess.ws, EventType.MEMORY_UPDATED.value)
    assert snapshots, "the overlay needs a memory snapshot after each turn"
    snap = snapshots[-1]
    assert snap["previous"], "previous questions must be reported"
    assert snap["previous"][-1]["current"] is True
    # Bounded: never a raw memory blob.
    assert len(snap["memory_points"]) <= 6
    assert all(len(p) <= 160 for p in snap["memory_points"])


# ─────────────────────────────────────────────────────────────────────────────
# 12. Error/cancel states are never silent
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_provider_failure_keeps_previous_answer_and_retry(monkeypatch):
    """Phase 5.4.1  19: a failed turn keeps the old answer and stays usable."""
    from ai.provider import AIProviderError

    sess = make_session()
    monkeypatch.setattr(session_mod, "transcribe_with_lock",
                        lambda *a, **k: stt_result("Explain the retry path"))

    async def _boom(messages, stream=False, **kwargs):
        raise AIProviderError("bonsai down")

    monkeypatch.setattr(sess.provider, "complete", _boom)
    await sess._handle_utterance(make_utterance(), 0.0)
    await sess._handle_text_message(json.dumps({"type": "turn.send", "payload": {}}))
    await asyncio.sleep(0.3)

    assert sess._turn_submitting is False, "a failed turn must be retryable"
    assert sess._state is RealtimeState.READY_TO_SEND, \
        "usable again, since the transcript is still there"
    assert sess.turn.get_current() == "Explain the retry path", \
        "the failed question is preserved so SEND can retry"
    errors = payloads(sess.ws, EventType.SESSION_ERROR.value)
    assert any(e.get("code") == "answer_generation_failed" for e in errors)
    # No answer was produced, so nothing may be added to history.
    assert sess.question_history == []


@pytest.mark.asyncio
async def test_empty_llm_stream_is_handled(monkeypatch):
    sess = make_session()
    monkeypatch.setattr(session_mod, "transcribe_with_lock",
                        lambda *a, **k: stt_result("Explain the empty stream"))

    async def _empty():
        if False:
            yield ""
        return
        yield  # pragma: no cover

    async def _complete(messages, stream=False, **kwargs):
        return _empty() if stream else ""

    monkeypatch.setattr(sess.provider, "complete", _complete)
    monkeypatch.setattr(session_mod, "retrieve_context_sync",
                        lambda *a, **k: ("", {"embed_ms": 1.0, "search_ms": 1.0}))
    await sess._handle_utterance(make_utterance(), 0.0)
    await sess._handle_text_message(json.dumps({"type": "turn.send", "payload": {}}))
    await asyncio.sleep(0.4)

    # Never silent: the turn must resolve to a usable, non-generating state.
    assert sess._state in (RealtimeState.LISTENING, RealtimeState.READY_TO_SEND,
                           RealtimeState.ANSWER_READY, RealtimeState.ERROR)
    assert sess._turn_submitting is False


# ─────────────────────────────────────────────────────────────────────────────
# 13. Transcript buffer unit contract
# ─────────────────────────────────────────────────────────────────────────────
def test_buffer_pause_resume_and_snapshot():
    buf = InterviewTranscriptBuffer(min_useful_chars=8)
    buf.append("what is")
    buf.append("rag")
    assert buf.get_current() == "what is rag"

    buf.pause()
    assert buf.append("ignored while paused") is None
    assert buf.get_current() == "what is rag"

    buf.resume()
    assert buf.get_current() == "what is rag"          # snapshot freezes nothing
    assert buf.snapshot() == "what is rag"
    assert buf.get_current() == "what is rag"          # not cleared by snapshot

    buf.clear()
    assert buf.get_current() == "" and buf.can_send is False


def test_buffer_dedups_by_stable_segment_id():
    buf = InterviewTranscriptBuffer()
    assert buf.append("first half of it", "seg-1") is not None
    assert buf.append("completely other words", "seg-1") is None
    assert buf.segment_count == 1
    assert buf.get_current() == "first half of it"


def test_buffer_requires_useful_length():
    buf = InterviewTranscriptBuffer(min_useful_chars=8)
    assert buf.append("ok") is None
    assert buf.can_send is False
    assert buf.append("why did you choose that") is not None
    assert buf.can_send is True


def test_buffer_normalizes_whitespace_across_segments():
    buf = InterviewTranscriptBuffer()
    buf.append("Can you   explain\n  my project")
    buf.append("  and   why FastAPI?  ")
    assert buf.get_current() == "Can you explain my project and why FastAPI?"


# ─────────────────────────────────────────────────────────────────────────────
# 14. WebSocket lifecycle
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_answer_cancel_releases_the_turn():
    sess = make_session()
    sess.turn.append("a question worth sending")
    sess._turn_submitting = True
    await sess._handle_text_message(json.dumps({"type": "turn.send", "payload": {}}))

    await sess._handle_text_message(json.dumps(
        {"type": "answer.cancel", "payload": {"reason": "user"}}))

    assert sess._turn_submitting is False
    assert payloads(sess.ws, EventType.ANSWER_CANCELLED.value)
    assert sess._state is RealtimeState.CANCELLED


# ─────────────────────────────────────────────────────────────────────────────
# 10. [KIYRA-PERF] per-turn latency, without leaking content
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_stt_timeout_is_retried_before_audio_is_discarded(monkeypatch):
    """Losing the candidate's words is unrecoverable: retry once first."""
    sess = make_session()
    calls = {"n": 0}

    def _slow(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            time.sleep(0.35)          # blows stt_timeout
        return stt_result("Explain my retry behaviour")

    monkeypatch.setattr(session_mod, "transcribe_with_lock", _slow)
    monkeypatch.setattr(session_mod.settings, "stt_timeout", 0.05)
    monkeypatch.setattr(session_mod.settings, "stt_retry_timeout", 5.0)

    await sess._handle_utterance(make_utterance(), 0.0)
    await asyncio.sleep(0.1)

    assert calls["n"] == 2, "the first timeout must be retried"
    assert sess.turn.get_current() == "Explain my retry behaviour"
    assert payloads(sess.ws, EventType.SESSION_ERROR.value) == [], \
        "a recovered timeout must not surface as an error"


@pytest.mark.asyncio
async def test_stt_gives_up_after_bounded_retries(monkeypatch):
    """Two timeouts then a clear, visible error (never a silent drop)."""
    sess = make_session()

    def _hang(*a, **k):
        time.sleep(2.0)
        return stt_result("never used")

    monkeypatch.setattr(session_mod, "transcribe_with_lock", _hang)
    monkeypatch.setattr(session_mod.settings, "stt_timeout", 0.05)
    monkeypatch.setattr(session_mod.settings, "stt_retry_timeout", 0.05)

    await sess._handle_utterance(make_utterance(), 0.0)
    await asyncio.sleep(0.4)

    errors = payloads(sess.ws, EventType.SESSION_ERROR.value)
    assert [e["code"] for e in errors] == ["stt_timeout"]
    assert errors[0]["recoverable"] is True


@pytest.mark.asyncio
async def test_perf_event_is_emitted_with_required_fields(monkeypatch):
    sess = make_session()
    monkeypatch.setattr(session_mod, "transcribe_with_lock",
                        lambda *a, **k: stt_result("Explain my RAG pipeline"))
    monkeypatch.setattr(session_mod, "retrieve_context_sync",
                        lambda *a, **k: ("", {"embed_ms": 1.0, "search_ms": 1.0}))
    sess._turn_id = "t7"

    await sess._handle_utterance(make_utterance(), 0.0)
    await sess._handle_text_message(json.dumps({"type": "turn.send", "payload": {}}))
    await asyncio.sleep(0.5)

    perf = payloads(sess.ws, EventType.PERF.value)
    assert perf, "a perf event must be emitted per turn"
    rec = perf[-1]
    for field in ("turn_id", "listen_ms", "stt_ms", "retrieval_ms", "context_ms",
                  "llm_ttft_ms", "llm_total_ms", "total_ms", "chunks",
                  "prompt_tokens", "completion_tokens"):
        assert field in rec, f"missing metric {field}"
    assert rec["turn_id"] == "t7"
    assert rec["mode"] == "interview_listening"
    assert rec["chunks"] == 5

    # Never leak the question or the answer into the metrics payload.
    blob = json.dumps(rec)
    assert "Explain my RAG pipeline" not in blob
    assert "FastAPI" not in blob

# ============================================================================
# Phase 5.4.1  continuous interview loop + question history
# ============================================================================
def _listen_and_send(sess, question: str, monkeypatch, answer: str = "Because it is fast."):
    """Drive one full turn: capture speech, SEND, wait for the answer."""
    monkeypatch.setattr(session_mod, "transcribe_with_lock",
                        lambda *a, **k: stt_result(question))
    monkeypatch.setattr(session_mod, "retrieve_context_sync",
                        lambda *a, **k: ("", {"embed_ms": 1.0, "search_ms": 1.0}))

    async def _complete(messages, stream=False, answer=answer, **kwargs):
        if stream:
            async def _gen():
                for tok in answer.split(" "):
                    yield tok + " "
            return _gen()
        return answer

    monkeypatch.setattr(sess.provider, "complete", _complete)


@pytest.mark.asyncio
async def test_answer_ready_is_not_terminal_it_auto_returns_to_listening(monkeypatch):
    """Spec 1: GENERATING -> ANSWER_READY -> LISTENING without another START."""
    sess = make_session()
    _listen_and_send(sess, "Explain my Kiyra project", monkeypatch)

    await sess._handle_utterance(make_utterance(), 0.0)
    await sess._handle_text_message(json.dumps({"type": "turn.send", "payload": {}}))
    await asyncio.sleep(0.5)

    assert payloads(sess.ws, EventType.ANSWER_GENERATED.value), "an answer was produced"
    assert sess._state is RealtimeState.LISTENING, \
        "the interview continues automatically"
    assert sess._listening_active is True


@pytest.mark.asyncio
async def test_two_consecutive_questions_need_only_one_start(monkeypatch):
    """Spec 1: the user presses START once, then just keeps talking + SENDing."""
    sess = make_session()
    monkeypatch.setattr(session_mod, "retrieve_context_sync",
                        lambda *a, **k: ("", {"embed_ms": 1.0, "search_ms": 1.0}))
    texts = iter(["Why did you use FastAPI?", "What are the advantages?"])

    async def _complete(messages, stream=False, **kwargs):
        if stream:
            async def _gen():
                yield "ok "
            return _gen()
        return "ok"

    monkeypatch.setattr(sess.provider, "complete", _complete)

    await sess._start_listening()
    starts_after_start = len(payloads(sess.ws, EventType.LISTENING_STARTED.value))

    answers = []
    for expected in ("Why did you use FastAPI?", "What are the advantages?"):
        monkeypatch.setattr(session_mod, "transcribe_with_lock",
                            lambda *a, _t=expected, **k: stt_result(_t))
        await sess._handle_utterance(make_utterance(), 0.0)
        await sess._handle_text_message(json.dumps({"type": "turn.send", "payload": {}}))
        await asyncio.sleep(0.4)
        answers.append(payloads(sess.ws, EventType.ANSWER_GENERATED.value)[-1]["answer"])

    generated = payloads(sess.ws, EventType.ANSWER_GENERATED.value)
    assert len(generated) == 2, "each SEND produced exactly one answer"
    assert sess.question_history[-1]["question"] == "What are the advantages?"
    # Only one *explicit* START; the rest are automatic re-arm notifications.
    assert starts_after_start == 1
    assert sess._state is RealtimeState.LISTENING


@pytest.mark.asyncio
async def test_rearm_does_not_reload_mic_vad_or_stt(monkeypatch):
    """Spec 5: re-arm resets turn state only - no model/audio reload."""
    sess = make_session()
    _listen_and_send(sess, "Explain the retry path", monkeypatch)

    receiver_before = sess._receiver
    worker_before = sess._audio_worker_task
    provider_before = sess.provider
    sess._audio_worker_task = asyncio.ensure_future(asyncio.sleep(30))
    worker_before = sess._audio_worker_task

    await sess._handle_utterance(make_utterance(), 0.0)
    await sess._handle_text_message(json.dumps({"type": "turn.send", "payload": {}}))
    await asyncio.sleep(0.4)

    assert sess._receiver is receiver_before, "VAD receiver must not be rebuilt"
    assert sess._audio_worker_task is worker_before, "audio worker must not restart"
    assert sess.provider is provider_before, "LLM provider must not be reloaded"
    assert sess._audio_worker_task is not None and not sess._audio_worker_task.done()

    sess._audio_worker_task.cancel()
    try:
        await sess._audio_worker_task
    except (asyncio.CancelledError, Exception):
        pass


@pytest.mark.asyncio
async def test_rearm_resets_transcript_and_sends_empty_current(monkeypatch):
    """Spec 17: the live transcript is clean for the next question."""
    sess = make_session()
    _listen_and_send(sess, "Explain my RAG pipeline", monkeypatch)

    await sess._handle_utterance(make_utterance(), 0.0)
    await sess._handle_text_message(json.dumps({"type": "turn.send", "payload": {}}))
    await asyncio.sleep(0.4)

    assert sess.turn.get_current() == ""
    assert payloads(sess.ws, EventType.LISTENING_STARTED.value)[-1]["segment_count"] == 0
    assert payloads(sess.ws, EventType.LISTENING_STARTED.value)[-1].get("auto_rearm")


@pytest.mark.asyncio
async def test_late_stt_from_a_previous_turn_is_ignored(monkeypatch, capsys):
    """Spec 8: a result owned by an older turn must never be appended."""
    sess = make_session()
    monkeypatch.setattr(session_mod, "transcribe_with_lock",
                        lambda *a, **k: stt_result("stale words from turn one"))

    await sess._start_listening()
    await sess._start_listening()          # rotate ownership
    assert sess._turn_id == "t2"

    await sess._handle_utterance(make_utterance(), 0.0, turn_id="t1")

    assert sess.turn.get_current() == "", "no stale text in the new turn"
    assert not payloads(sess.ws, EventType.ANSWER_GENERATED.value)
    assert sess.question_history == []

    # The drop must be logged with session + turn + the SEGMENT identity that
    # was allocated before the async STT hop (never transcript content).
    log = capsys.readouterr().out
    stale = [ln for ln in log.splitlines() if "stale_stt_ignored" in ln]
    assert stale, "expected a stale_stt_ignored log line"
    assert "session=" in stale[0] and "turn=t1" in stale[0]
    assert "active_turn=t2" in stale[0]
    assert re.search(r"segment=u\d", stale[0]), stale[0]
    assert "chars=" in stale[0]
    assert "words" not in stale[0], "log must not contain transcript content"


@pytest.mark.asyncio
async def test_current_turn_stt_is_still_accepted(monkeypatch):
    """The ownership gate must not reject the live turn."""
    sess = make_session()
    monkeypatch.setattr(session_mod, "transcribe_with_lock",
                        lambda *a, **k: stt_result("the live question"))

    await sess._start_listening()
    await sess._handle_utterance(make_utterance(), 0.0, turn_id=sess._turn_id)

    assert sess.turn.get_current() == "the live question"


@pytest.mark.asyncio
async def test_history_records_question_answer_topic_and_turn(monkeypatch):
    """Spec 9: every completed turn is navigable with its answer."""
    sess = make_session()
    _listen_and_send(sess, "Explain my RAG pipeline", monkeypatch, answer="It is layered.")

    await sess._start_listening()
    await sess._handle_utterance(make_utterance(), 0.0)
    await sess._handle_text_message(json.dumps({"type": "turn.send", "payload": {}}))
    await asyncio.sleep(0.4)

    assert len(sess.question_history) == 1
    entry = sess.question_history[0]
    assert entry["question"] == "Explain my RAG pipeline"
    assert entry["answer_available"] is True
    assert "It is layered." in entry["answer"]
    assert entry["turn_id"] == "t1"
    assert entry["sequence"] == 1
    assert entry["topic"], "topic must never be blank"
    assert entry["ts"] > 0


@pytest.mark.asyncio
async def test_history_payload_carries_index_and_answer(monkeypatch):
    """Spec 9: the overlay receives everything needed to render history."""
    sess = make_session()
    _listen_and_send(sess, "Explain my RAG pipeline", monkeypatch, answer="It is layered.")

    await sess._start_listening()
    await sess._handle_utterance(make_utterance(), 0.0)
    await sess._handle_text_message(json.dumps({"type": "turn.send", "payload": {}}))
    await asyncio.sleep(0.4)

    payload = payloads(sess.ws, EventType.MEMORY_UPDATED.value)[-1]
    assert payload["history_count"] >= 1
    assert payload["history_index"] == len(payload["previous"]) - 1
    entry = payload["previous"][-1]
    assert entry["turn_id"] == "t1"
    assert entry["answer_available"] is True
    assert entry["answer"]
    assert entry["current"] is True
    assert payload["topic"], "topic must not be blanked"


@pytest.mark.asyncio
async def test_stop_goes_idle_and_keeps_answer_history_and_memory(monkeypatch):
    """Spec 20: STOP is explicit and preserves everything."""
    sess = make_session()
    _listen_and_send(sess, "Explain my RAG pipeline", monkeypatch)

    await sess._handle_utterance(make_utterance(), 0.0)
    await sess._handle_text_message(json.dumps({"type": "turn.send", "payload": {}}))
    await asyncio.sleep(0.4)
    history_before = list(sess.question_history)

    await sess._handle_text_message(json.dumps({"type": "listening.stop", "payload": {}}))

    assert sess._state is RealtimeState.IDLE, "STOP is terminal until START"
    assert sess._listening_active is False
    assert sess.question_history == history_before, "history survives STOP"
    assert payloads(sess.ws, EventType.ANSWER_GENERATED.value), "answer survives STOP"


@pytest.mark.asyncio
async def test_start_after_stop_preserves_answer_history_and_memory(monkeypatch):
    """Spec 21: START does not erase the answer, history or memory."""
    sess = make_session()
    _listen_and_send(sess, "Explain my RAG pipeline", monkeypatch)

    await sess._handle_utterance(make_utterance(), 0.0)
    await sess._handle_text_message(json.dumps({"type": "turn.send", "payload": {}}))
    await asyncio.sleep(0.4)
    await sess._handle_text_message(json.dumps({"type": "listening.stop", "payload": {}}))
    streamed_before = len(payloads(sess.ws, EventType.ANSWER_STREAMED.value))

    history_before = list(sess.question_history)
    await sess._handle_text_message(json.dumps({"type": "listening.start", "payload": {}}))
    await asyncio.sleep(0.2)

    assert sess._state is RealtimeState.LISTENING
    assert sess.question_history == history_before
    # No second answer was produced and nothing re-streamed, so the answer
    # already on screen is untouched by START.
    assert len(payloads(sess.ws, EventType.ANSWER_GENERATED.value)) == 1
    assert len(payloads(sess.ws, EventType.ANSWER_STREAMED.value)) == streamed_before


@pytest.mark.asyncio
async def test_second_question_appends_a_second_history_entry(monkeypatch):
    """Spec 9: history accumulates across the interview."""
    sess = make_session()
    monkeypatch.setattr(session_mod, "retrieve_context_sync",
                        lambda *a, **k: ("", {"embed_ms": 1.0, "search_ms": 1.0}))

    async def _complete(messages, stream=False, **kwargs):
        if stream:
            async def _gen():
                yield "answer "
            return _gen()
        return "answer"

    monkeypatch.setattr(sess.provider, "complete", _complete)

    await sess._start_listening()
    for question in ("Explain my RAG pipeline", "What are the advantages?"):
        monkeypatch.setattr(session_mod, "transcribe_with_lock",
                            lambda *a, _t=question, **k: stt_result(_t))
        await sess._handle_utterance(make_utterance(), 0.0)
        await sess._handle_text_message(json.dumps({"type": "turn.send", "payload": {}}))
        await asyncio.sleep(0.4)

    assert [h["sequence"] for h in sess.question_history] == [1, 2]
    assert sess.question_history[0]["turn_id"] != sess.question_history[1]["turn_id"]


@pytest.mark.asyncio
async def test_legacy_auto_detect_never_auto_rearms(monkeypatch):
    """Phase 5.4's legacy auto-detect path is untouched by the new loop."""
    sess = make_session(listening=False)
    monkeypatch.setattr(session_mod, "retrieve_context_sync",
                        lambda *a, **k: ("", {"embed_ms": 1.0, "search_ms": 1.0}))

    async def _complete(messages, stream=False, **kwargs):
        if stream:
            async def _gen():
                yield "legacy "
            return _gen()
        return "legacy"

    monkeypatch.setattr(sess.provider, "complete", _complete)
    monkeypatch.setattr(session_mod, "transcribe_with_lock",
                        lambda *a, **k: stt_result("What is RAG?"))

    await sess._handle_utterance(make_utterance(), 0.0)
    await asyncio.sleep(0.4)

    assert payloads(sess.ws, EventType.ANSWER_GENERATED.value), "legacy path still answers"
    assert payloads(sess.ws, EventType.LISTENING_STARTED.value) == [], \
        "no interview loop outside Interview Listening Mode"