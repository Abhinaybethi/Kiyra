"""WebSocket session manager for live interview assistance.

Implements the explicit real-time state machine:

    IDLE → LISTENING → SPEECH_DETECTED → TRANSCRIBING → QUESTION_CANDIDATE
    → RETRIEVING_CONTEXT → GENERATING → STREAMING_ANSWER → LISTENING

Error states: STT_ERROR, PROVIDER_UNAVAILABLE, WEBSOCKET_DISCONNECTED, AUDIO_ERROR.

Latency architecture (overlap, not sequential waiting):

    Audio frames (250ms) → VAD → speech buffer
        ├─ partial STT when speech >= partial_trigger_speech_s  ──────────┐
        │      → `transcript.partial` event                              │
        │      → question-prefix detection (cheap, no LLM)               │
        │             └─ high confidence ⇒ RAG pretfetch + SPECULATIVE   │
        │                LLM generation starts while the speaker is      │
        │                still talking; tokens are BUFFERED, never shown │
        ▼                                                                 │
    utterance finalized (short trailing silence)                          │
        → final STT → final question                                     │
             ├─ similar to speculative question ⇒ RELEASE buffered       │
             │   answer immediately (≈ speech-end), continue streaming   │
             └─ different ⇒ cancel speculation, generate fresh           │
                                                                          ▼
                              first useful answer ≈ TTFT early / speech-end

Key properties:
  * Audio callbacks never block; queues are bounded and drop-oldest, and
    every queue/violation is logged with budgets (no silent backlog growth).
  * Partial transcripts feed the UI early (incremental STT) and drive the
    speculative pipeline.
  * Question detection is deterministic and layered (no LLM for detection).
  * Every generation has a unique request_id; a new question cancels stale
    RAG/generation/streams so no orphan tasks and no stale visible tokens.
  * HTTP streaming reuses the provider's pooled client (connection reuse).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
import traceback
from datetime import datetime
from enum import Enum
from typing import Optional

from fastapi import WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

from config import settings
from db.models import (
    InterviewSession,
    TranscriptSegment,
    Project,
    Skill,
    SessionTurn,
    SessionMemoryState,
    SessionRetrieval,
)
from ai.provider import get_provider, ProviderUnavailableError, AIProviderError
from knowledge.retrieval import retrieve_context_sync
from realtime.audio import (
    EnergyVAD,
    UtteranceReceiver,
    decode_wav_frame,
    encode_wav_float32,
)
from realtime.memory import ContextBuilder, Resolution, RetrievalCache, SessionMemory, Turn
from realtime.question_analyzer import analyze_question
from realtime.perf import PerformanceTracker
from realtime.prompts import realtime_system
from realtime.transcription import transcribe_with_lock
from realtime.transcript import (
    InterviewTranscriptBuffer,
    TranscriptBuffer,
    normalize as normalize_whitespace,
)


_QUESTION_LEAD = frozenset({
    "what", "why", "how", "when", "where", "who", "whom", "which",
    "explain", "describe", "tell", "briefly", "walk", "give", "show",
    "can", "could", "would", "should", "do", "does", "did", "is", "are",
    "was", "were", "have", "has", "had", "you", "me", "us", "your", "my",
    "our", "their", "the", "a", "an", "about", "please",
})

# Words that only ever connect content words inside a topic label. They are
# skipped anywhere in the question so a label never ends on a dangling
# conjunction ("project and", "rag and").
_TOPIC_CONNECTORS = frozenset({
    "and", "or", "but", "of", "to", "for", "with", "in", "on", "at",
    "from", "as", "by", "into", "via", "than", "then",
})


def _topic_fallback(question: str, limit: int = 6) -> str:
    """Last-resort topic label so the panel is never blank (Phase 5.4.1 §16).

    Strips the whole interrogative lead-in and keeps the first content words,
    e.g. "Can you explain how the retry path works?" -> "retry path works".
    Only leading words are dropped, so "the" inside the topic survives.
    Connectors are skipped wherever they appear: the old behaviour kept them
    and produced labels like "project and" / "rag and", which then read as a
    broken referent when reused as an active topic.
    """
    words = re.findall(r"[A-Za-z0-9_+#.\-]+", question or "")
    kept: list[str] = []
    for word in words:
        low = word.lower()
        if low in _TOPIC_CONNECTORS:
            continue
        if low in _QUESTION_LEAD:
            if kept:
                break  # only a leading run of filler is removed
            continue
        kept.append(word)
        if len(kept) >= limit:
            break
    return " ".join(kept)


def _memory_points(memory, limit: int = 6) -> list[str]:
    """Concise, non-sensitive memory lines for the overlay Memory panel.

    Deliberately shallow: never dumps raw memory blobs (spec §9).
    """
    if memory is None:
        return []
    points: list[str] = []

    established = getattr(memory, "established_facts", None) or []
    for fact in established[-limit:]:
        text = getattr(fact, "text", None) or str(fact)
        if text:
            points.append(" ".join(str(text).split())[:160])

    if not points:
        for turn in list(getattr(memory, "turns", []) or [])[-3:]:
            q = getattr(turn, "question", "") or ""
            if q:
                points.append(" ".join(str(q).split())[:160])

    return points[:limit]
from realtime.question_detector import (
    QuestionDetector,
    similarity,
    detect_partial,
    partial_validation_score,
    partial_spec_eligible,
)
from github_repo import (
    GitHubConfigError,
    GitHubRetriever,
    configured_projects,
    create_if_configured,
    evidence_block,
    evidence_note,
    project_display_names,
    resolve_project,
)

class RealtimeState(str, Enum):
    IDLE = "IDLE"
    LISTENING = "LISTENING"
    SPEECH_DETECTED = "SPEECH_DETECTED"
    TRANSCRIBING = "TRANSCRIBING"
    QUESTION_CANDIDATE = "QUESTION_CANDIDATE"
    RETRIEVING_CONTEXT = "RETRIEVING_CONTEXT"
    GENERATING = "GENERATING"
    STREAMING_ANSWER = "STREAMING_ANSWER"
    # ── Interview Listening Mode (Phase 5.4) ──────────────────────────────
    # The user owns the question boundary: speech accumulates into a turn and
    # ONLY an explicit SEND leaves READY_TO_SEND.
    READY_TO_SEND = "READY_TO_SEND"
    ANSWER_READY = "ANSWER_READY"
    CANCELLED = "CANCELLED"
    ERROR = "ERROR"
    # Error states
    STT_ERROR = "STT_ERROR"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    WEBSOCKET_DISCONNECTED = "WEBSOCKET_DISCONNECTED"
    AUDIO_ERROR = "AUDIO_ERROR"


class EventType(str, Enum):
    """WebSocket event types (kept backward-compatible where possible)."""

    SESSION_CONNECTED = "session.connected"
    SESSION_ERROR = "session.error"
    SESSION_END = "session.end"

    AUDIO_RECEIVED = "audio.received"
    TRANSCRIPT_FINAL = "transcript.final"
    TRANSCRIPT_PARTIAL = "transcript.partial"

    QUESTION_DETECTED = "question.detected"
    QUESTION_POTENTIAL = "question.potential"  # additive: early high-confidence prefix
    STATE_CHANGED = "state.changed"

    ANSWER_GENERATING = "answer.generating"
    ANSWER_GENERATED = "answer.generated"
    ANSWER_STREAMED = "answer.streamed"
    ANSWER_CANCELLED = "answer.cancelled"

    # ── Interview Listening Mode (Phase 5.4) ──────────────────────────────
    MODE_CHANGED = "mode.changed"
    LISTENING_STARTED = "listening.started"
    LISTENING_STOPPED = "listening.stopped"
    TRANSCRIPT_SEGMENT = "transcript.segment"  # one finalized STT segment
    TURN_SUBMITTED = "turn.submitted"          # the frozen, authoritative text
    MEMORY_UPDATED = "memory.updated"          # topic / previous / memory for UI
    PERF = "kiyra.perf"                         # [KIYRA-PERF] metrics

    PING = "ping"
    PONG = "pong"


_SPEC_END = object()  # sentinel marking the end of a speculative provider stream


# ── GitHub-aware project retrieval (Phase 5.2) ─────────────────────────────
# The retriever is the ONLY GitHub-touching object the session uses; all
# repo logic lives in github_repo/. Bonsai never calls GitHub directly.

_github_retriever: Optional[GitHubRetriever] = None
_github_error_logged = False


def _get_retriever() -> Optional[GitHubRetriever]:
    """Lazily-built GitHub retriever; None when disabled / unconfigured.

    Never raises: a missing token, bad config, or any construction failure
    simply disables project retrieval instead of failing the answer.
    """
    global _github_retriever, _github_error_logged
    if _github_retriever is not None:
        return _github_retriever
    if not settings.kiyra_github_enabled:
        return None
    try:
        provider = create_if_configured()
    except GitHubConfigError as e:
        if not _github_error_logged:
            print(f"[GITHUB] project retrieval unavailable: {e}")
            _github_error_logged = True
        return None
    except Exception as e:
        print(f"[GITHUB] retriever init failed (project retrieval off): {e}")
        return None
    if provider is None:
        return None
    _github_retriever = GitHubRetriever(provider)
    return _github_retriever


def _event(event_type: str, payload: dict) -> str:
    """Serialize WebSocket event."""
    return json.dumps({
        "type": event_type,
        "payload": payload,
        "ts": time.time(),
    })


_ANSWER_END = object()  # stream-end sentinel for the authoritative answer path


class LiveSession:
    """Single live WebSocket session with VAD, STT, detection, RAG and streaming."""

    # Diagnostics counters — class-level defaults so lightweight test doubles
    # (LiveSession.__new__ without __init__) also get them.
    _rx_count = 0
    _vad_log_n = 0
    _vad_log_state: Optional[bool] = None
    _vad_speech_started: Optional[float] = None

    # Phase-3 memory/profiling state — class defaults (same __new__ reason).
    _memory: Optional["SessionMemory"] = None
    _perf: Optional[PerformanceTracker] = None
    _spec_perf: Optional[PerformanceTracker] = None
    _vad_end_mono: Optional[float] = None

    # ── Interview Listening Mode class defaults (same __new__ reason) ─────
    # The turn buffer and question history are lazily built per instance so a
    # test double created with __new__ never shares mutable state.
    _listening_mode = False
    _listening_active = False
    _turn = None
    _turn_seq = 0
    _turn_submitting = False
    _turn_submitted_text: Optional[str] = None
    _turn_start_ts: Optional[float] = None
    _turn_listen_ms = 0.0
    _turn_id: Optional[str] = None
    _turn_no = 0
    _submitted_turn_id: Optional[str] = None
    _last_resolved_topic: Optional[str] = None
    _question_history: Optional[list] = None
    _recent_questions: Optional[list] = None

    @property
    def turn(self) -> InterviewTranscriptBuffer:
        """The open turn's transcript buffer (lazily created per instance)."""
        buf = self.__dict__.get("_turn")
        if buf is None:
            buf = InterviewTranscriptBuffer(
                min_useful_chars=settings.turn_min_useful_chars)
            self.__dict__["_turn"] = buf
        return buf

    @property
    def recent_questions(self) -> list:
        """Submitted questions for the Previous Questions panel."""
        history = self.__dict__.get("_recent_questions")
        if history is None:
            history = []
            self.__dict__["_recent_questions"] = history
        return history

    def __init__(self, websocket: WebSocket, session: InterviewSession, db: Session):
        self.ws = websocket
        self.session = session
        self.db = db
        self.provider = get_provider(db=db)

        self._running = True
        self._state = RealtimeState.IDLE
        self._pending_question: Optional[str] = None

        # Audio pipeline (bounded ingestion → VAD → speech buffer)
        self._frame_queue: asyncio.Queue[tuple[float, bytes]] = asyncio.Queue(
            maxsize=settings.audio_queue_max
        )
        self._receiver = UtteranceReceiver(
            sample_rate=settings.audio_sample_rate,
            speech_end_silence_ms=settings.speech_end_silence_ms,
            minimum_speech_duration=settings.minimum_speech_duration,
            maximum_utterance_duration=settings.maximum_utterance_duration,
            speech_start_threshold=settings.speech_start_threshold,
        )

        self._transcripts = TranscriptBuffer()
        self._detector = QuestionDetector(
            cooldown_seconds=settings.question_cooldown_seconds,
            dedup_window=settings.question_dedup_window,
        )

        # ── Interview Listening Mode (Phase 5.4) ────────────────────────────
        # Opt-in per connection. When on, VAD only SEGMENTS audio; every
        # finalized STT segment is appended to the turn buffer and nothing is
        # submitted until the client sends `turn.send`. Legacy auto-detect
        # behaviour is untouched when the mode is off (the web client).
        self._listening_mode = False
        self._listening_active = False
        # `turn` / `recent_questions` are lazy properties (see the class body)
        # so test doubles built with __new__ never share mutable state.
        self._turn = None
        self._turn_seq = 0
        self._seg_seq = 0
        self._turn_submitting = False   # single-flight guard: one SEND = one gen
        self._turn_submitted_text: Optional[str] = None
        self._turn_start_ts: Optional[float] = None
        self._turn_listen_ms = 0.0
        self._turn_id: Optional[str] = None
        self._turn_no = 0
        self._submitted_turn_id: Optional[str] = None
        self._last_resolved_topic: Optional[str] = None
        self._question_history: Optional[list] = None
        self._recent_questions = None

        # Answer generation management
        self._request_seq = 0
        self._current_request_id: Optional[str] = None
        self._answer_task: Optional[asyncio.Task] = None
        self._prefetch_task: Optional[asyncio.Task] = None
        self._prefetch_question: Optional[str] = None
        self._prefetch_context: Optional[tuple[str, dict]] = None

        # Deterministic question analysis carried between turns so follow-ups
        # ("now add authentication") inherit coding intent / language.
        self._last_analysis = None

        # Project (GitHub) grounding: the project resolved from the last
        # project question so follow-ups keep targeting the same repository.
        self._active_project_id: Optional[str] = None

        # Partial / speculative pipeline
        self._partial_task: Optional[asyncio.Task] = None
        self._last_partial_speech = 0.0
        self._partial_count = 0
        self._spec_task: Optional[asyncio.Task] = None
        self._spec_question: Optional[str] = None
        self._spec_final_question: Optional[str] = None
        self._spec_request_id: Optional[str] = None
        self._spec_released = False
        self._spec_validated = False
        self._spec_similarity = 0.0
        self._spec_tokens: list[str] = []
        self._spec_sent = 0  # how many speculative tokens were flushed to the UI
        self._spec_early_released = False  # released at speech-end (before final STT)

        # Metrics
        self._questions_detected = 0
        self._answers_generated = 0
        self._stt_count = 0
        self._dropped_frames = 0
        self._start_time = time.time()
        self._audio_worker_task: Optional[asyncio.Task] = None
        self._last_queue_log = 0.0
        self._last_queue_delay_ms = 0.0
        self._budget_warns: dict[str, float] = {}

    def _trace_prompt_boundary(self, question, source, analysis, system,
                               user_prompt):
        """[CODE-TRACE] final generation boundary: does the exact prompt about
        to reach the provider still carry the coding instruction? Logs ONLY
        marker booleans + the analysis fields — never resume/context/prompt."""
        q_safe = " ".join(str(question or "").split())[:100]
        had = analysis is not None
        req = bool(had and analysis.requires_code)
        low = user_prompt.lower()
        inst_exe = "must provide actual executable code" in low
        inst_fence = "must contain a fenced code block" in low
        sys_override = "CODING QUESTION OVERRIDE" in (system or "")
        lang = analysis.language or "" if had else ""
        fw = analysis.framework or "" if had else ""
        print(
            "[CODE-TRACE] question=%s source=%s intent=%s requires_code=%s "
            "language=%s framework=%s"
            % (q_safe, source or "-", (analysis.intent if had else "-")
               or "-", req, lang or "-", fw or "-")
        )
        print(
            "[CODE-TRACE] coding_instruction_present=%s code_block_required=%s "
            "requested_language=%s requested_framework=%s system_override=%s"
            % (inst_exe, inst_fence, lang or "-", fw or "-", sys_override)
        )

    # ────────────────────────────────────────────────────────────────────────
    # WebSocket plumbing
    # ────────────────────────────────────────────────────────────────────────

    async def send(self, event_type: str, payload: dict):
        try:
            await self.ws.send_text(_event(event_type, payload))
        except Exception as e:
            print(f"[WS] Failed to send {event_type}: {e}")
            self._running = False

    async def run(self):
        """Main WebSocket loop."""
        await self.send(EventType.SESSION_CONNECTED, {
            "session_id": self.session.id,
            "mode": self.session.mode,
            "profile": {
                "name": self.session.profile.name,
                "target_role": self.session.profile.target_role,
            },
        })
        self._set_state(RealtimeState.LISTENING)
        self._audio_worker_task = asyncio.create_task(self._audio_worker())

        try:
            while self._running:
                try:
                    message = await asyncio.wait_for(
                        self.ws.receive(),
                        timeout=settings.ws_heartbeat_interval,
                    )
                except asyncio.TimeoutError:
                    await self.send(EventType.PING, {})
                    continue

                if message["type"] == "websocket.disconnect":
                    break

                if message["type"] == "websocket.receive":
                    if "bytes" in message and message["bytes"]:
                        await self._ingest_frame(message["bytes"])
                    elif "text" in message and message["text"]:
                        await self._handle_text_message(message["text"])

        except WebSocketDisconnect:
            pass
        except Exception as e:
            print(f"[Session {self.session.id}] Unexpected error: {e}")
            try:
                await self.send(EventType.SESSION_ERROR, {
                    "code": "internal",
                    "message": str(e),
                    "recoverable": True,
                })
            except Exception:
                pass
        finally:
            self._set_state(RealtimeState.WEBSOCKET_DISCONNECTED)
            # Stop producers first, then flush the pending speech WHILE the
            # request-scoped DB session is still open (get_db closes it as
            # soon as this handler returns). Never leave fire-and-forget
            # tasks touching the session after teardown.
            self._cancel_inflight_tasks()
            await self._flush_pending_speech()
            self._cleanup()

    # ────────────────────────────────────────────────────────────────────────
    # Audio ingestion → VAD → speech buffer (bounded, non-blocking)
    # ────────────────────────────────────────────────────────────────────────

    async def _ingest_frame(self, frame_bytes: bytes):
        """Enqueue an audio frame. Drops oldest on overflow instead of blocking.

        FRESH AUDIO > OLD AUDIO: when overloaded the newest frame is kept and
        the oldest stale one dropped, so latency never accumulates silently.
        """
        self._rx_count += 1
        if self._rx_count == 1 or self._rx_count % 48 == 0:
            print(f"[WS_RX] frame={self._rx_count} bytes={len(frame_bytes)}")
        try:
            self._frame_queue.put_nowait((time.monotonic(), frame_bytes))
        except asyncio.QueueFull:
            try:
                self._frame_queue.get_nowait()
                self._frame_queue.put_nowait((time.monotonic(), frame_bytes))
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass
            self._dropped_frames += 1
            if self._dropped_frames % 100 == 1:
                print(f"[REALTIME] Dropped {self._dropped_frames} audio frames (rate-limited)")

    async def _audio_worker(self):
        """Single consumer draining the bounded frame queue through VAD.

        Never blocks on heavy work: STT runs in `to_thread`/separate tasks so
        the drain loop keeps up with wall-clock audio.
        """
        try:
            while self._running:
                try:
                    recv_ts, frame_bytes = await asyncio.wait_for(
                        self._frame_queue.get(), timeout=0.2
                    )
                except asyncio.TimeoutError:
                    continue

                delay_ms = (time.monotonic() - recv_ts) * 1000
                self._last_queue_delay_ms = delay_ms
                self._log_budget(
                    "audio_queue", delay_ms, settings.realtime_queue_budget_ms
                )
                self._log_queue_metrics()
                await self._process_frame(recv_ts, frame_bytes)
        except asyncio.CancelledError:
            pass

    def _log_queue_metrics(self):
        """Rate-limited [AUDIO_QUEUE] metrics (size / delay / dropped)."""
        now = time.monotonic()
        if now - self._last_queue_log < 1.0:
            return
        self._last_queue_log = now
        print(
            "[AUDIO_QUEUE] size=%d delay_ms=%.0f dropped=%d"
            % (self._frame_queue.qsize(), self._last_queue_delay_ms, self._dropped_frames)
        )

    def _log_budget(self, stage: str, actual_ms: float, budget_ms: float):
        """Rate-limited [LATENCY_BUDGET_EXCEEDED] warnings."""
        if budget_ms <= 0 or actual_ms <= budget_ms:
            return
        now = time.monotonic()
        if now - self._budget_warns.get(stage, 0.0) < 5.0:
            return
        self._budget_warns[stage] = now
        print(
            "[LATENCY_BUDGET_EXCEEDED] stage=%s actual_ms=%d budget_ms=%d"
            % (stage, int(actual_ms), int(budget_ms))
        )

    async def _process_frame(self, recv_ts: float, frame_bytes: bytes):
        frame = decode_wav_frame(frame_bytes)
        if frame.size == 0:
            if self._rx_count % 48 == 1:
                print(f"[WS_RX] decode_failed bytes={len(frame_bytes)}")
            self._set_state(RealtimeState.AUDIO_ERROR)
            return

        rms = EnergyVAD.rms(frame)
        was_voiced = self._receiver.is_voiced
        utterance = self._receiver.push_frame(frame, recv_ts)
        is_voiced = self._receiver.is_voiced

        # Per-frame VAD diagnostics: state changes always, otherwise every ~6s
        self._vad_log_n += 1
        if is_voiced != self._vad_log_state or self._vad_log_n % 24 == 1:
            self._vad_log_state = is_voiced
            print(
                "[VAD] rms=%.4f state=%s samples=%d"
                % (rms, "speech" if is_voiced else "silence", frame.size)
            )

        if not was_voiced and is_voiced:
            self._vad_speech_started = time.monotonic()
            self._vad_end_mono = None  # speech is ongoing — no audio-end mark yet
            print(f"[VAD] SPEECH_START rms={rms:.4f}")
            self._set_state(RealtimeState.SPEECH_DETECTED)
        if was_voiced and not is_voiced:
            self._vad_end_mono = time.monotonic()  # monotonic anchor for [PERF]
            if utterance is not None:
                dur_ms = utterance.duration * 1000.0
            else:
                dur_ms = (time.monotonic() -
                          (self._vad_speech_started or time.monotonic())) * 1000.0
            print(f"[VAD] SPEECH_END duration={dur_ms:.0f}ms rms={rms:.4f}")

        if utterance is not None:
            # Turn ownership: stamp the utterance with the turn that was OPEN
            # when the VAD finalized it. STT happens inline below and can be
            # slower than a WS `listening.start`, so the turn may rotate while
            # this utterance is in flight. The stamp is what stops a late turn-N
            # transcript from being appended into turn N+1.
            utterance_turn_id = self._turn_id
            await self._handle_utterance(utterance, recv_ts,
                                         turn_id=utterance_turn_id)

        # Partial / speculative trigger — fire-and-forget (drain loop never blocks)
        if self._receiver.is_voiced:
            self._maybe_trigger_partial()

    def _start_utterance_perf(self) -> PerformanceTracker:
        """Anchor a fresh monotonic profiler at audio-end for this utterance."""
        t0 = self._vad_end_mono if self._vad_end_mono is not None else time.monotonic()
        self._vad_end_mono = None
        perf = PerformanceTracker(t0=t0)
        perf.marks["audio_end"] = 0.0  # t0 IS audio end — offset 0 by definition
        vad_ms = perf.mark("vad_finalization")
        perf.set_duration("vad_ms", vad_ms)
        self._perf = perf
        return perf

    def _emit_turn_perf(self, tracker: Optional[PerformanceTracker],
                        request_id: str = "", prompt_tokens=None,
                        completion_tokens=None, chunks: Optional[int] = None):
        """Structured per-turn latency line ([KIYRA-PERF], Phase 5.4 §10).

        Contains timings and counts only — never transcript or answer content.
        Emitted to the log and, in listening mode, to the client as a `perf`
        event so the overlay can measure UI render latency.
        """
        if tracker is None:
            return
        d = tracker.durations

        def _ms(key):
            val = d.get(key)
            return -1 if val is None else int(round(val))

        listen_ms = int(round(getattr(self, "_turn_listen_ms", 0.0) or 0.0))
        record = {
            "turn_id": getattr(self, "_turn_id", None) or request_id or "",
            "request_id": request_id or (tracker.request_id or ""),
            "mode": "interview_listening" if self._listening_mode else "auto_detect",
            "listen_ms": listen_ms,
            "vad_ms": _ms("vad_ms"),
            "stt_ms": _ms("stt_ms"),
            "detection_ms": _ms("question_detection_ms"),
            "retrieval_ms": _ms("rag_ms"),
            "context_ms": _ms("context_ms"),
            "prompt_ms": _ms("prompt_ms"),
            "llm_ttft_ms": _ms("llm_ttft_ms"),
            "llm_total_ms": _ms("generation_ms"),
            "first_token_ms": _ms("first_token_ms"),
            "total_ms": _ms("total_ms"),
            "segments": int(getattr(self, "_turn_seq", 0) or 0),
            "chunks": int(chunks) if chunks is not None else -1,
            "prompt_tokens": int(prompt_tokens) if prompt_tokens else -1,
            "completion_tokens": (int(completion_tokens)
                                  if completion_tokens else -1),
        }
        print("[KIYRA-PERF] " + " ".join(
            f"{k}={v}" for k, v in record.items()))
        if self._listening_mode:
            try:
                asyncio.get_running_loop().create_task(
                    self.send(EventType.PERF, record))
            except Exception:
                pass

    # ────────────────────────────────────────────────────────────────────────
    # Session memory (Phase 3): lazy state, profile entities, persistence
    # ────────────────────────────────────────────────────────────────────────

    def _get_memory(self) -> SessionMemory:
        """Lazy, failure-safe session memory (never raises)."""
        memory = self._memory
        if memory is None:
            try:
                memory = SessionMemory(
                    session_id=getattr(self.session, "id", None),
                    max_turns=settings.realtime_memory_turns,
                    summary_chars=settings.realtime_memory_summary_chars,
                    recent_turns=settings.realtime_memory_recent_turns,
                    entities=self._load_profile_entities(),
                    cache=RetrievalCache(
                        ttl_s=settings.realtime_memory_cache_ttl_s,
                        max_entries=settings.realtime_memory_cache_entries,
                        hit_sim=settings.realtime_memory_hit_sim,
                    ),
                )
                self._restore_memory(memory)
            except Exception as e:
                print(f"[MEMORY] init failed: {e}")
                if memory is None:
                    memory = SessionMemory()
            self._memory = memory
        return memory

    def _load_profile_entities(self) -> list[str]:
        """Canonical entity names from the profile's projects + skills."""
        try:
            profile_id = self.session.profile.id
            names: list[str] = []
            for row in self.db.query(Project).filter_by(profile_id=profile_id).all():
                if getattr(row, "name", None):
                    names.append(row.name)
            for row in self.db.query(Skill).filter_by(profile_id=profile_id).all():
                if getattr(row, "name", None):
                    names.append(row.name)
            return names
        except Exception:
            return []  # test doubles / detached sessions → regex entities only

    def _restore_memory(self, memory: SessionMemory) -> None:
        """Reload persisted memory state so a reconnect keeps continuity."""
        try:
            sid = getattr(self.session, "id", None)
            if sid is None:
                return
            probe = self.db.query(SessionMemoryState)
            if not hasattr(probe, "filter_by"):
                return  # lightweight test double
            state = probe.filter_by(session_id=sid).first()
            rows = (
                self.db.query(SessionTurn)
                .filter_by(session_id=sid)
                .order_by(SessionTurn.turn_index.desc())
                .limit(memory.max_turns)
                .all()
            )
        except Exception as e:
            print(f"[MEMORY] restore failed: {e}")
            try:
                self.db.rollback()
            except Exception:
                pass
            return
        if state is not None:
            memory.active_topic = state.active_topic or ""
            memory.important_entities = list(state.important_entities or [])
            memory.compact_summary = state.compact_summary or ""
        for row in reversed(rows):
            memory.turns.append(Turn(
                question=row.question or "",
                answer=row.answer or "",
                resolved_question=row.resolved_question or "",
                topic=row.topic or "",
                entities=list(row.entities or []),
                followup=bool(row.follow_up),
            ))
        if state is None and memory.turns:
            memory.rebuild_summary()

    def _remember_retrieval(self, memory: Optional[SessionMemory], topic: str,
                            question: str, context: str,
                            metrics: Optional[dict]) -> None:
        """Cache + persist a non-empty retrieval (both failure-safe)."""
        if not context:
            return
        if memory is not None:
            try:
                memory.cache.put(topic, question, context, metrics)
            except Exception as e:
                print(f"[MEMORY] cache put failed: {e}")
        try:
            probe = self.db.query(SessionRetrieval)
            if not hasattr(probe, "filter_by"):
                return  # lightweight test double
            self.db.add(SessionRetrieval(
                session_id=self.session.id,
                topic=topic or "",
                query=question,
                context=context,
                metrics=dict(metrics or {}),
            ))
            self.db.commit()
        except Exception as e:
            print(f"[MEMORY] retrieval persist failed: {e}")
            try:
                self.db.rollback()
            except Exception:
                pass

    def _record_turn(self, question: str, answer: str,
                     resolution: Optional[Resolution]) -> str:
        """Append the answered turn to memory + DB (never raises).

        Returns the resolved topic (never blank when a question exists) so the
        history entry can reuse it instead of re-resolving.
        """
        try:
            memory = self._get_memory()
            resolution = resolution or Resolution.identity(question)
            # Inherit the previous active topic only for real follow-ups.
            # Otherwise a self-contained question ("What is RAG…?") would be
            # labelled with the previous project topic.
            inherited = memory.active_topic if resolution.followup else ""
            topic = (resolution.referent or resolution.topic
                     or inherited
                     or _topic_fallback(question))
            turn = Turn(
                question=question,
                answer=answer,
                resolved_question=resolution.resolved_question or question,
                topic=topic,
                entities=list(resolution.referenced_entities),
                followup=resolution.followup,
                ts=time.time(),  # storage only — profiling stays monotonic
            )
            memory.record_turn(turn)
        except Exception as e:
            print(f"[MEMORY] record failed: {e}")
            return _topic_fallback(question)
        print(
            "[MEMORY] turn=%d topic=%r followup=%s entities=%d"
            % (len(memory.turns), topic, resolution.followup, len(turn.entities))
        )
        try:
            probe = self.db.query(SessionTurn)
            if not hasattr(probe, "filter_by"):
                return topic  # lightweight test double
            sid = getattr(self.session, "id", None)
            if sid is None:
                return topic
            self.db.add(SessionTurn(
                session_id=sid,
                turn_index=len(memory.turns),
                question=question,
                answer=answer,
                resolved_question=turn.resolved_question,
                topic=topic,
                entities=turn.entities,
                follow_up=turn.followup,
            ))
            state = self.db.query(SessionMemoryState).filter_by(
                session_id=sid).first()
            if state is None:
                state = SessionMemoryState(session_id=sid)
                self.db.add(state)
            state.active_topic = memory.active_topic
            state.compact_summary = memory.compact_summary
            state.important_entities = list(memory.important_entities)
            self.db.commit()
            print("[MEMORY] persisted session=%s turn=%d"
                  % (sid, len(memory.turns)))
        except Exception as e:
            print(f"[MEMORY] persist failed: {e}")
            try:
                self.db.rollback()
            except Exception:
                pass
        return topic

    def _report_perf(self, tracker: Optional[PerformanceTracker]) -> None:
        """Print a speculative [PERF] once, inheriting utterance timing."""
        if tracker is None:
            return
        utt = self._perf
        if utt is not None and utt.marks.get("question_detection") is not None:
            tracker.inherit(utt)
        tracker.log()


    async def _handle_utterance(self, utterance, recv_ts: float, *,
                            finalize_only: bool = False,
                            turn_id: str | None = None):
        """Full utterance ready → transcribe → aggregate → detect → answer.

        finalize_only=True is used on disconnect: transcribe and persist the
        trailing speech, but skip detection/answer generation (no client is
        listening and the answer task would outlive the DB session).

        turn_id is the turn that owned this utterance at the VAD boundary. If
        the turn has since rotated, the STT result is stale and is dropped
        instead of corrupting the new question (Phase 5.4.1 §7-§8).
        """
        # Utterance boundary: TranscriptBuffer.current_utterance is a
        # fragment roll-up for ONE utterance, and finalize_utterance() is
        # never called in the live path — without this reset every detected
        # question would concatenate the previous ones
        # ("Q1Q2Q3..."). History is already preserved in entries by
        # add_fragment; only the scratch roll-up is cleared.
        self._transcripts.current_utterance = ""

        # Allocate the segment identity BEFORE the async STT hop. A stale
        # result can then be logged with the exact segment it belonged to,
        # and a late/failed transcript never has to invent an identity
        # (Phase 5.4.1 §8).
        seg_seq = getattr(self, "_seg_seq", 0) + 1
        self._seg_seq = seg_seq
        segment_id = f"u{seg_seq}"

        # Aggressive first answer: speech just ended and a speculative answer is
        # already buffered (generated from a *complete-looking* partial). Release
        # it NOW — don't wait ~1.7s for the full final STT. The authoritative
        # transcript that lands later confirms it or supersedes it.
        # Interview Listening Mode never speculates: the user owns the boundary.
        if (not self._listening_mode
                and self._spec_task and not self._spec_task.done()
                and self._spec_question and not self._spec_released):
            self._spec_released = True
            self._spec_validated = True
            self._spec_similarity = 1.0
            self._spec_final_question = self._spec_question
            self._spec_early_released = True
            print("[SPEC] early-release at speech-end q=%r" % self._spec_question[:70])

        perf = self._start_utterance_perf()
        self._set_state(RealtimeState.TRANSCRIBING)
        audio_wav = encode_wav_float32(utterance.audio)
        print(
            "[STT] starting samples=%d audio=%.2fs"
            % (utterance.audio.size, utterance.duration)
        )

        stt_start = time.monotonic()
        perf.mark("stt_start")

        async def _stt_once(timeout_s: float):
            return await asyncio.wait_for(
                asyncio.to_thread(transcribe_with_lock, audio_wav, "wav"),
                timeout=timeout_s,
            )

        try:
            try:
                result = await _stt_once(settings.stt_timeout)
            except asyncio.TimeoutError:
                # Dropping the audio here loses the candidate's words forever,
                # and the retry is what absorbs a cold Whisper load. One
                # bounded retry, then surface it (never silently discard).
                print(f"[STT] timed out at {settings.stt_timeout}s; retrying "
                      f"within {settings.stt_retry_timeout}s")
                result = await _stt_once(settings.stt_retry_timeout)
        except asyncio.TimeoutError:
            self._set_state(RealtimeState.STT_ERROR)
            await self.send(EventType.SESSION_ERROR, {
                "code": "stt_timeout",
                "message": f"Transcription exceeded {settings.stt_timeout}s",
                "recoverable": True,
            })
            self._set_state(RealtimeState.LISTENING)
            return
        except Exception as e:
            self._set_state(RealtimeState.STT_ERROR)
            await self.send(EventType.SESSION_ERROR, {
                "code": "transcription_failed",
                "message": f"Transcription error: {e}",
                "recoverable": True,
            })
            self._set_state(RealtimeState.LISTENING)
            return

        self._stt_count += 1
        stt_ms = (time.monotonic() - stt_start) * 1000
        perf.mark("stt_end")
        perf.set_duration("stt_ms", stt_ms)
        print(
            "[STT] audio=%.2fs queue_delay=%.0fms transcription=%.0fms total=%.0fms text=%r"
            % (utterance.duration, max(0.0, (stt_start - utterance.start_ts) * 1000),
               result.latency_ms, stt_ms, (result.text or "")[:100])
        )
        if result.text and result.text.strip():
            print(f'[STT] transcript="{result.text}"')

        # ── Turn ownership gate (Phase 5.4.1 §8) ───────────────────────────
        # The turn rotated while STT was running (a faster `listening.start`, a
        # STOP, or a SEND that finalised this utterance). Appending now would
        # splice turn N's words into turn N+1's question. Drop it.
        if (turn_id is not None and turn_id != self._turn_id
                and self._listening_mode):
            print("[TURN] stale_stt_ignored session=%s turn=%s active_turn=%s "
                  "segment=%s chars=%d"
                  % (self.session.id, turn_id, self._turn_id, segment_id,
                     len(result.text or "")))
            return

        if not result.text or len(result.text.strip()) < 3:
            self._cancel_spec()
            return

        # ── Interview Listening Mode ──────────────────────────────────────
        # Speech ended, but that is NOT "the interviewer finished". Append the
        # finalized segment to the open turn and go back to waiting for more
        # speech. Only an explicit SEND submits.
        if self._listening_mode:
            await self._append_turn_segment(result, segment_id=segment_id)
            return

        self._on_partial_transcript(result)
        if finalize_only:
            return
        await self._analyze_transcript(result)

    async def _append_turn_segment(self, result, *, segment_id: str):
        """Append one finalized STT segment to the open turn (listening mode)."""
        self._turn_seq += 1
        stored = self.turn.append(result.text, segment_id=segment_id)

        entry = self._transcripts.add_fragment(
            result.text, speaker="interviewer")
        db_segment_id = None
        if entry is not None:
            seg = TranscriptSegment(
                session_id=self.session.id,
                speaker=entry.speaker,
                content=entry.text,
                timestamp_ms=int(time.time() * 1000),
            )
            self.db.add(seg)
            self.db.commit()
            db_segment_id = seg.id
            self._last_transcript_segment_id = seg.id

        current = self.turn.get_current()
        # `duplicate` keeps the UI honest: nothing was added to the turn.
        await self.send(EventType.TRANSCRIPT_SEGMENT, {
            "segment_id": segment_id,
            "turn_id": self._turn_id or "",
            "text": result.text,
            "current": current,
            "duplicate": stored is None,
            "char_count": len(current),
            "can_send": self.turn.can_send,
            "segment_count": self.turn.segment_count,
        })
        print(
            '[TURN] segment appended turn=%s segment=%s segments=%d chars=%d'
            % (self._turn_id, segment_id, self.turn.segment_count, len(current))
        )

        if self.turn.can_send:
            self._set_state(RealtimeState.READY_TO_SEND)
        else:
            self._set_state(RealtimeState.LISTENING)

    # ────────────────────────────────────────────────────────────────────────
    # Partial STT + question-prefix detection + speculative preparation (Phases 3–5)
    # ────────────────────────────────────────────────────────────────────────

    def _maybe_trigger_partial(self):
        """Schedule a partial transcription when enough NEW speech has gathered."""
        if not settings.realtime_speculative_enabled:
            return
        if self._spec_task and not self._spec_task.done():
            return  # already speculating — leave CPU for the LLM
        if self._partial_task and not self._partial_task.done():
            return  # one partial in flight is enough
        speech = self._receiver.speech_seconds
        if speech < settings.realtime_partial_speech_s:
            return
        if (speech - self._last_partial_speech) < settings.realtime_partial_interval_s:
            return
        self._last_partial_speech = speech
        self._partial_task = asyncio.create_task(self._run_partial())

    async def _run_partial(self):
        """Transcribe the audio buffered *so far* and possibly start speculation."""
        try:
            snapshot = self._receiver.snapshot()
            if snapshot.size < settings.audio_sample_rate * 0.4:
                return
            wav = encode_wav_float32(snapshot)
            stt_start = time.monotonic()
            try:
                result = await asyncio.wait_for(
                    asyncio.to_thread(transcribe_with_lock, wav, "wav", fast=True),
                    timeout=settings.stt_timeout,
                )
            except (asyncio.TimeoutError, Exception):
                return
            stt_ms = (time.monotonic() - stt_start) * 1000
            self._partial_count += 1
            self._log_budget("partial_stt", stt_ms, settings.realtime_partial_budget_ms)
            print(
                "[PARTIAL_STT] speech=%.1fs stt=%.0fms text=%r"
                % (self._receiver.speech_seconds, stt_ms, (result.text or "")[:80])
            )

            if not result.text or len(result.text.strip()) < 3:
                return

            # Incremental transcript reaches the UI immediately (Metric C/Phase 3).
            await self.send(EventType.TRANSCRIPT_PARTIAL, {
                "text": result.text,
                "partial": True,
                "stt_ms": int(stt_ms),
            })

            det = detect_partial(result.text)
            if not det["is_question"] or not det["stable_prefix"]:
                if det["confidence"] >= settings.realtime_prefetch_confidence:
                    self._maybe_prefetch(result.text)
                return

            # ── Interview Listening Mode ──────────────────────────────────
            # Partial text updates the live transcript and warms the RAG cache,
            # but never starts a generation: the SEND button owns submission.
            if self._listening_mode:
                if det["confidence"] >= settings.realtime_prefetch_confidence:
                    self._maybe_prefetch(result.text)
                return

            if det["confidence"] < settings.realtime_speculative_min_confidence:
                self._maybe_prefetch(result.text)
                return

            # Never gamble a generation (or an early release) on a transcript
            # that visibly cut off mid-phrase — we would answer a truncated
            # question ("...a pro" instead of "...a process and a thread?").
            if not partial_spec_eligible(result.text):
                if det["confidence"] >= settings.realtime_prefetch_confidence:
                    self._maybe_prefetch(result.text)
                return

            # A new question takes priority over any buffered/streaming work.
            if self._answer_task and not self._answer_task.done():
                return  # authoritative answer already streaming — don't clobber
            if self._spec_task and not self._spec_task.done():
                return
            if self._spec_question and similarity(self._spec_question, result.text) >= 0.8:
                return  # near-duplicate of the current speculation

            request_id = self._next_request_id()

            # Kill superseded work FIRST, so its cleanup cannot wipe the fresh
            # speculative state we set up below.
            self._cancel_inflight()
            if self._answer_task and not self._answer_task.done():
                self._answer_task.cancel()

            self._spec_question = result.text
            self._spec_final_question = None
            self._spec_validated = False
            self._spec_released = False
            self._spec_similarity = 0.0
            self._spec_tokens = []
            self._spec_sent = 0
            self._spec_request_id = request_id
            self._current_request_id = request_id
            # Question boundary: the final utterance of THIS question gets a
            # fresh [PERF] tracker when it lands; a stale one must never be
            # inherited into the speculative report.
            self._perf = None

            await self.send(EventType.QUESTION_POTENTIAL, {
                "question": result.text,
                "confidence": det["confidence"],
                "speculative": True,
            })

            self._spec_task = asyncio.create_task(
                self._speculate_answer(result.text, request_id, det["confidence"])
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[Session {self.session.id}] Partial pipeline error: {e}")
        finally:
            self._partial_task = None

    # ────────────────────────────────────────────────────────────────────────
    # Transcript aggregation + question detection
    # ────────────────────────────────────────────────────────────────────────

    def _on_partial_transcript(self, result):
        entry = self._transcripts.add_fragment(result.text, speaker="interviewer")
        if entry is None:
            return  # duplicate fragment — already contained in the utterance

        segment = TranscriptSegment(
            session_id=self.session.id,
            speaker="interviewer",
            content=entry.text,
            timestamp_ms=int(time.time() * 1000),
        )
        self.db.add(segment)
        self.db.commit()
        self._last_transcript_segment_id = segment.id

        asyncio.create_task(self.send(EventType.TRANSCRIPT_FINAL, {
            "text": result.text,
            "duration": result.duration,
            "timestamp": time.time(),
        }))

    async def _analyze_transcript(self, result):
        """Question detection on the *final* utterance, then answer.

        Validates any buffered speculation first: a similar final question
        releases the pre-generated answer immediately (≈ speech-end), a
        divergent one cancels it and starts a fresh authoritative generation.
        """
        merged = self._transcripts.current_utterance or result.text
        detect_start = time.monotonic()
        is_q, confidence = self._detector.detect(merged)
        self._log_budget(
            "question_detect", (time.monotonic() - detect_start) * 1000,
            settings.realtime_detect_budget_ms,
        )
        if self._perf is not None:
            self._perf.mark("question_detection")
            self._perf.set_duration(
                "question_detection_ms",
                (time.monotonic() - detect_start) * 1000,
            )

        if not is_q or confidence < settings.question_min_confidence:
            self._cancel_spec()
            return
        print(
            '[QUESTION] detected="%s" confidence=%.2f source=live'
            % (merged[:160], confidence)
        )

        # Prefetch RAG context as soon as we have a strong candidate, so that
        # retrieval overlaps with everything else the pipeline has to do.
        if not is_q and confidence >= 0.6:
            self._maybe_prefetch(merged)

        self._set_state(RealtimeState.QUESTION_CANDIDATE)
        self._questions_detected += 1
        self._pending_question = merged

        segment_id = getattr(self, "_last_transcript_segment_id", None)
        if segment_id:
            seg = self.db.query(TranscriptSegment).filter_by(id=segment_id).first()
            if seg:
                seg.is_question = True
                seg.confidence = confidence
                self.db.commit()

        await self.send(EventType.QUESTION_DETECTED, {
            "question": merged,
            "confidence": confidence,
            "segment_id": segment_id,
        })

        # 1) Validation path: speculative question ≈ final question ⇒ release /
        # confirm the buffered answer (whether released early or still pending).
        spec_active = self._spec_task and not self._spec_task.done()
        if self._spec_question and (spec_active or self._spec_released):
            sim = partial_validation_score(self._spec_question, merged)
            if sim >= settings.realtime_spec_validation_threshold:
                if not self._spec_released:
                    self._spec_released = True
                    self._spec_validated = True
                self._spec_similarity = max(sim, self._spec_similarity)
                self._spec_final_question = merged
                self._spec_question = merged
                # Hand the utterance's STT/detect timing to the speculative
                # tracker so its [PERF] report covers the full pipeline.
                if self._spec_perf is not None:
                    self._spec_perf.inherit(self._perf)
                print(
                    "[SPEC] validated sim=%.2f question=%r" % (sim, merged[:80])
                )
                if not spec_active:
                    # Answer was already flushed early; the final transcript
                    # simply confirms it — nothing else to do.
                    self._spec_validated = True
                    return
                # spec task still streaming — it flushes the buffer and streams live
                return
            if self._spec_released:
                # The early-released answer no longer matches the final question
                # (whisper heard the tail differently) — supersede it now.
                print("[SPEC] early answer superseded sim=%.2f question=%r"
                      % (sim, merged[:80]))

        # 2) Fresh authoritative generation.
        self._cancel_spec()
        request_id = self._next_request_id()
        self._cancel_inflight()
        if self._answer_task and not self._answer_task.done():
            self._answer_task.cancel()

        # Claim the request id BEFORE the new task starts, so a superseded
        # task's `finally` runs on a stale id and never clobbers this ref.
        self._current_request_id = request_id
        self._answer_task = asyncio.create_task(
            self._generate_answer(
                merged, request_id, confidence, perf=self._perf, source="voice")
        )

    # ────────────────────────────────────────────────────────────────────────
    # Early RAG prefetch
    # ────────────────────────────────────────────────────────────────────────

    def _maybe_prefetch(self, question: str):
        if self._prefetch_task and not self._prefetch_task.done():
            return  # one outstanding prefetch is enough
        self._prefetch_question = question
        self._prefetch_task = asyncio.create_task(self._prefetch_worker(question))

    async def _prefetch_worker(self, question: str):
        try:
            self._prefetch_context = await asyncio.wait_for(
                asyncio.to_thread(
                    retrieve_context_sync,
                    self.session.profile.id,
                    question,
                    top_k=settings.realtime_top_k,
                    max_context_chars=settings.realtime_max_context_chars,
                ),
                timeout=settings.realtime_rag_timeout,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            self._prefetch_context = ("", {"error": "prefetch failed"})

    async def _bounded_rag(self, question: str,
                           resolution: Optional[Resolution] = None,
                           perf: Optional[PerformanceTracker] = None
                           ) -> tuple[str, str]:
        """Cache → prefetch → hard-bounded retrieval. Never raises.

        Returns (context, rag_note). Slow RAG can never stall the answer:
        on timeout we answer without context. Cache reuse requires a topic
        match + query similarity — otherwise a fresh retrieval runs.
        """
        try:
            memory = self._get_memory()
        except Exception:
            memory = None
        topic = ""
        if resolution is not None:
            topic = resolution.referent or resolution.topic or ""
        if not topic and memory is not None:
            topic = memory.active_topic

        # 1) Topic-scoped retrieval cache (correctness: topic must match).
        if memory is not None:
            try:
                hit = memory.cache.get(topic, question)
            except Exception:
                hit = None
            if hit is not None:
                if perf is not None:
                    perf.mark("retrieval_start")
                    perf.mark("retrieval_end")
                    perf.set_duration("rag_ms", 0.0)
                print("[RAG] cache_hit topic=%r query=%r" % (topic, question[:60]))
                return hit[0], "cache"

        # 2) Reuse a recent prefetch (it was fresh when it ran) — remember it.
        if self._prefetch_context and self._prefetch_question and \
                similarity(self._prefetch_question, question) >= 0.6:
            context, metrics = self._prefetch_context
            self._prefetch_context = None
            self._prefetch_question = None
            if perf is not None:
                perf.mark("retrieval_start")
                perf.mark("retrieval_end")
                perf.set_duration("rag_ms", 0.0)
            self._remember_retrieval(memory, topic, question, context, metrics)
            return context, "prefetch"

        # 3) Fresh retrieval.
        try:
            rag_start = time.monotonic()
            if perf is not None:
                perf.mark("retrieval_start")
            context, metrics = await asyncio.wait_for(
                asyncio.to_thread(
                    retrieve_context_sync,
                    self.session.profile.id,
                    question,
                    top_k=settings.realtime_top_k,
                    max_context_chars=settings.realtime_max_context_chars,
                ),
                timeout=settings.realtime_rag_timeout,
            )
            rag_ms = (time.monotonic() - rag_start) * 1000
            if perf is not None:
                perf.mark("retrieval_end")
                perf.set_duration("rag_ms", rag_ms)
            self._log_budget("rag", rag_ms, settings.realtime_rag_budget_ms)
            self._remember_retrieval(memory, topic, question, context, metrics)
            return context, ("query" if context else "no-rag")
        except asyncio.TimeoutError:
            if perf is not None:
                perf.mark("retrieval_end")
                perf.set_duration(
                    "rag_ms", settings.realtime_rag_timeout * 1000.0)
            return "", "no-rag-timeout"
        except Exception as e:
            # Retrieval failure is non-fatal: answer without context.
            print(f"[RAG] retrieval failed: {e}")
            if perf is not None:
                perf.mark("retrieval_end")
            return "", "no-rag-error"

    # ────────────────────────────────────────────────────────────────────────
    # GitHub-aware project retrieval (Phase 5.2)
    # ────────────────────────────────────────────────────────────────────────

    def _project_scope(self) -> tuple[list[str], bool, Optional[GitHubRetriever]]:
        """(project_names, project_active, retriever) for the question analyzer.

        Never raises. ``project_active`` is true when a project is firmly in
        scope (active-project memory OR exactly one configured project) so
        project-area questions become Mode B without guessing between repos.
        """
        gh = _get_retriever()
        if gh is None:
            return [], False, None
        try:
            projects = configured_projects()
        except Exception:
            return [], False, gh
        if not projects:
            return [], False, gh
        try:
            names = project_display_names()
        except Exception:
            names = []
        active = bool(getattr(self, "_active_project_id", None)) or len(projects) == 1
        if not active:
            try:
                memory = self._get_memory()
                cands = set(memory.entity_candidates())
                if any(n in cands for n in names):
                    active = True
            except Exception:
                pass
        return names, active, gh

    def _resolve_project_match(self, question: str, resolution: Resolution,
                               include_entities: bool = False):
        """Select the target project (no network). None = ambiguous/generic."""
        try:
            memory = self._get_memory()
        except Exception:
            memory = None
        entities = []
        if include_entities and memory is not None:
            entities = memory.entity_candidates()
        try:
            return resolve_project(
                question,
                active_project_id=getattr(self, "_active_project_id", None),
                active_topic=(resolution.topic if resolution else "") or
                             (memory.active_topic if memory is not None else ""),
                conversation_entities=entities,
            )
        except Exception:
            return None

    async def _github_evidence(
        self, question: str, analysis, resolution: Resolution,
        match=None,
    ) -> tuple[str, str, dict]:
        """PROJECT EVIDENCE for a project-related question + rag_note tail.

        Returns (evidence_text, evidence_note, project_info). Hard-bounded by
        ``kiyra_github_real_timeout`` and NEVER raises — an unavailable or
        ambiguous project degrades to (empty, note, None).
        """
        gh = _get_retriever()
        if gh is None or not analysis.project_related:
            return "", "", None
        if match is None:
            match = self._resolve_project_match(
                question, resolution, include_entities=True)
        if match is None:
            return "", "github-ambiguous", None
        with_repo = {
            "id": match.project.project_id,
            "name": match.project.project_name,
            "repo": match.project.full_name,
        }
        # Remember the resolved project for follow-ups ("now add rate limiting").
        self._active_project_id = match.project.project_id
        try:
            memory = self._get_memory()
        except Exception:
            memory = None
        if memory is not None:
            memory.add_entities([
                match.project.project_name, match.project.full_name])
        try:
            hits = await asyncio.wait_for(
                asyncio.to_thread(
                    self._retrieve_project_chunks,
                    gh, match.project, question, analysis),
                timeout=settings.kiyra_github_real_timeout,
            )
        except asyncio.TimeoutError:
            return "", "github-timeout", with_repo
        except Exception as e:
            print(f"[GITHUB] retrieval failed (safe note only): {e}")
            return "", "github-error", with_repo
        if not hits:
            return "", "github-noevidence", with_repo
        try:
            text = evidence_block(
                match.project.project_name, hits, analysis.project_intent or "")
        except Exception:
            text = ""
        if not text:
            return "", "github-noevidence", with_repo
        return text, evidence_note(hits), with_repo

    def _retrieve_project_chunks(self, gh: GitHubRetriever, project,
                                 question: str, analysis):
        """Sync retrieval (runs in a worker thread, bounded by the caller)."""
        gh.ensure_indexed(project)
        return gh.retrieve(project, question, intent=analysis.project_intent or "")

    # ────────────────────────────────────────────────────────────────────────
    # Answer generation (streaming + cancellation)
    # ────────────────────────────────────────────────────────────────────────

    def _next_request_id(self) -> str:
        self._request_seq += 1
        return f"{self.session.id}:{self._request_seq}:{int(time.time() * 1000)}"

    def _cancel_prefetch(self):
        if self._prefetch_task and not self._prefetch_task.done():
            self._prefetch_task.cancel()
        self._prefetch_task = None
        self._prefetch_context = None
        self._prefetch_question = None

    def _cancel_spec(self):
        """Cancel buffered speculation and drop any unsent tokens (never shown)."""
        if self._spec_task and not self._spec_task.done():
            self._spec_task.cancel()
        self._spec_task = None
        self._spec_perf = None
        self._spec_question = None
        self._spec_final_question = None
        self._spec_request_id = None
        self._spec_validated = False
        self._spec_released = False
        self._spec_similarity = 0.0
        self._spec_tokens = []
        self._spec_sent = 0
        self._spec_early_released = False

    def _cancel_inflight(self):
        """Cancel stale RAG prefetch + buffered speculation (not the answer task)."""
        self._cancel_prefetch()
        self._cancel_spec()

    async def _generate_answer(self, question: str, request_id: str,
                               confidence: float,
                               perf: Optional[PerformanceTracker] = None,
                               note: str = "", source: str = ""):
        """Authoritative answer path — used when no speculation was valid.

        ``note`` is an optional prompt suffix supplied by callers that have
        extra context the model should honor (e.g. OCR screenshot format hints).
        ``source`` (manual / screenshot / voice) is only attached to the
        question-analysis metadata so the answer format follows the question
        content, never the input modality.
        """
        started = time.monotonic()
        drain: Optional[asyncio.Task] = None
        first_token_t: Optional[float] = None
        if perf is None:
            # Manual/text path (no VAD/STT stages) — anchor a fresh tracker.
            perf = PerformanceTracker(request_id=request_id)
        else:
            perf.request_id = request_id
        try:
            resolution = self._get_memory().resolve(question)
        except Exception:
            resolution = Resolution.identity(question)
        # Deterministic intent analysis (resolved question: follow-ups resolve
        # against previous context so "now add authentication" keeps coding).
        project_names, project_active, _gh = self._project_scope()
        analysis = analyze_question(
            resolution.resolved_question or question,
            source=source,
            previous=getattr(self, "_last_analysis", None),
            followup=bool(resolution.followup),
            project_names=project_names,
            project_active=project_active,
        )
        self._last_analysis = analysis
        latency = {
            "stt_ms": 0.0, "detect_ms": 0.0, "rag_ms": 0.0,
            "llm_ttft_ms": 0.0, "total_ms": 0.0,
        }
        try:
            self._current_request_id = request_id
            print(
                '[LLM] generation started request_id=%s question="%s"'
                % (request_id, question[:80])
            )
            print(
                '[ANALYSIS] intent=%s requires_code=%s language=%s framework=%s'
                ' source=%s project_related=%s project_intent=%s'
                % (analysis.intent, analysis.requires_code,
                   analysis.language or "-", analysis.framework or "-",
                   source or "-", analysis.project_related,
                   analysis.project_intent or "-")
            )

            # Signal "generating" immediately so the client never stares at a
            # silent spinner while retrieval (bounded below) runs.
            project_info = None
            project_match = None
            if analysis.project_related:
                project_match = self._resolve_project_match(
                    resolution.resolved_question or question, resolution)
                if project_match is not None:
                    project_info = {
                        "id": project_match.project.project_id,
                        "name": project_match.project.project_name,
                        "repo": project_match.project.full_name,
                    }
            await self.send(EventType.ANSWER_GENERATING, {
                "agent": "realtime_answer",
                "question": question,
                "request_id": request_id,
                "analysis": analysis.to_dict(),
                "project": project_info,
            })
            self._set_state(RealtimeState.RETRIEVING_CONTEXT)

            context, rag_note = await self._bounded_rag(
                question, resolution=resolution, perf=perf)
            latency["rag_ms"] = perf.durations.get("rag_ms") or 0.0
            self._prefetch_context = None
            self._prefetch_question = None

            # Project evidence (GitHub) — bounded, only when project-related.
            project_evidence = ""
            if analysis.project_related:
                ev, gh_note, project_info = await self._github_evidence(
                    resolution.resolved_question or question, analysis,
                    resolution, match=project_match)
                project_evidence = ev
                if gh_note:
                    rag_note = f"{rag_note}|{gh_note}"

            self._set_state(RealtimeState.GENERATING)

            memory = self._get_memory()
            ctx_start = time.monotonic()
            sections = ContextBuilder.memory_sections(memory, resolution)
            perf.mark("context_construction")
            perf.span("context_ms", ctx_start)

            prompt_start = time.monotonic()
            system = realtime_system(
                settings.answer_mode, requires_code=bool(analysis.requires_code))
            user_prompt = ContextBuilder.assemble(
                question=question,
                candidate_name=self.session.profile.name or "the candidate",
                context=context,
                memory=memory,
                resolution=resolution,
                sections=sections,
                analysis=analysis,
                project_evidence=project_evidence or None,
            )
            if note:
                user_prompt = f"{user_prompt}\n\n{note}"
            perf.mark("prompt_construction")
            perf.span("prompt_ms", prompt_start)

            self._trace_prompt_boundary(question, source, analysis, system,
                                        user_prompt)

            perf.mark("llm_request")
            llm_call_t = time.monotonic()
            stream = await self.provider.complete(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_prompt},
                ],
                stream=True,
                temperature=0.5,
                max_tokens=settings.realtime_max_output_tokens,
                request_id=request_id,
            )
            perf.span("llm_request_ms", llm_call_t)

            self._set_state(RealtimeState.STREAMING_ANSWER)
            tokens: list[str] = []
            partial = ""
            last_flush = time.monotonic()
            q: asyncio.Queue = asyncio.Queue()
            drain = asyncio.create_task(self._drain(stream, q, _ANSWER_END))
            token_deadline: Optional[float] = None
            _trace = {"first_chunk": None, "fenced_so_far": False,
                      "chunk_count": 0}

            while True:
                if self._current_request_id != request_id:
                    break  # superseded by a newer question
                if token_deadline is None:
                    # First token: CPU-only llama may need ~75s for a cold
                    # prompt eval. After that: 15s per-token dead-stream cap.
                    budget = (settings.realtime_first_token_timeout
                              if latency["llm_ttft_ms"] == 0.0
                              else settings.answer_generation_timeout)
                    token_deadline = time.monotonic() + budget
                remaining = token_deadline - time.monotonic()
                if remaining <= 0:
                    break  # dead stream — no token within the deadline
                try:
                    tok = await asyncio.wait_for(
                        q.get(), timeout=min(remaining, 1.0)
                    )
                except asyncio.TimeoutError:
                    continue  # deadline not reached — re-check supersede each second
                token_deadline = None  # token arrived — fresh deadline for the next one
                if tok is _ANSWER_END:
                    break
                if latency["llm_ttft_ms"] == 0.0:
                    latency["llm_ttft_ms"] = (time.monotonic() - started) * 1000
                    first_token_t = time.monotonic()
                    perf.mark("llm_ttft")
                    perf.span("llm_ttft_ms", llm_call_t)
                    self._log_budget(
                        "llm_ttft", latency["llm_ttft_ms"],
                        settings.realtime_ttft_budget_ms,
                    )
                tokens.append(tok)
                partial += tok
                if _trace is not None and _trace.get("first_chunk") is None:
                    _trace["first_chunk"] = tok.strip()[:80]
                if _trace is not None and ("```" in tok or "~~~" in tok):
                    _trace["fenced_so_far"] = True
                if len(partial) >= 40 or (time.monotonic() - last_flush) > 0.1:
                    await self.send(EventType.ANSWER_STREAMED, {
                        "delta": partial,
                        "request_id": request_id,
                    })
                    perf.first_visible(llm_call_t)
                    partial = ""
                    last_flush = time.monotonic()

            if partial and self._current_request_id == request_id:
                await self.send(EventType.ANSWER_STREAMED, {
                    "delta": partial,
                    "request_id": request_id,
                })
                perf.first_visible(llm_call_t)

            if _trace is not None:
                _trace["chunk_count"] = len(tokens)

            if self._current_request_id != request_id:
                return

            answer = "".join(tokens).strip()
            if _trace is not None:
                print(
                    "[CODE-TRACE] first_chunk=%r fenced_so_far=%s chunk_count=%s "
                    "final_contains_fence=%s"
                    % (str(_trace.get("first_chunk") or "")[:80],
                       _trace.get("fenced_so_far", False),
                       _trace.get("chunk_count", 0),
                       "```" in answer or "~~~" in answer))
            if _trace is not None:
                print(
                    "[CODE-TRACE] generated_chars=%d generated_contains_code_fence=%s"
                    % (len(answer), "```" in answer or "~~~" in answer)
                )
            if not tokens:
                print(
                    "[LLM] empty stream request_id=%s question=%r "
                    "(provider returned no tokens — check LLM endpoint)"
                    % (request_id, question[:80])
                )
            perf.mark("generation_complete")
            perf.span("generation_ms", first_token_t or llm_call_t)
            self._answers_generated += 1
            latency["total_ms"] = (time.monotonic() - started) * 1000
            latency["stt_ms"] = perf.durations.get("stt_ms") or 0.0
            latency["detect_ms"] = perf.durations.get("question_detection_ms") or 0.0
            perf.finish()
            # Structured per-turn latency line (Phase 5.4 §10).
            self._emit_turn_perf(
                perf, request_id,
                chunks=len(tokens),
                prompt_tokens=_trace.get("prompt_tokens") if _trace else None,
                completion_tokens=_trace.get("completion_tokens") if _trace else None,
            )
            print(
                "[REALTIME_LATENCY] stt=%.0fms detect=%.0fms rag=%.0fms "
                "llm_ttft=%.0fms total=%.0fms"
                % (latency["stt_ms"], latency["detect_ms"], latency["rag_ms"],
                   latency["llm_ttft_ms"], latency["total_ms"])
            )
            self._last_resolved_topic = self._record_turn(question, answer, resolution)
            perf.log()

            await self.send(EventType.ANSWER_GENERATED, {
                "answer": answer,
                "key_points": [],
                "star": None,
                "follow_up_questions": [],
                "confidence": confidence,
                "mode": settings.answer_mode,
                "request_id": request_id,
                "latency_ms": int(latency["total_ms"]),
                "ttft_ms": int(latency["llm_ttft_ms"]),
                "rag": rag_note,
            })

            # ── Continuous interview loop (Phase 5.4.1, spec 1/5/24) ──────
            # ANSWER_READY is NOT terminal: the answer is already on screen and
            # persisted, so the turn rotates and the mic goes live again for
            # the next question without another START click. Only per-turn
            # state is reset — Whisper/VAD/audio device are never reloaded.
            if self._listening_mode:
                self._set_state(RealtimeState.ANSWER_READY)
                # _record_turn already resolved and persisted the topic; reuse
                # it so history and the panel can never disagree (spec 16).
                resolved_topic = (getattr(self, "_last_resolved_topic", "")
                                  or _topic_fallback(question))
                self._record_question_history(
                    question=question, answer=answer,
                    turn_id=getattr(self, "_submitted_turn_id", None) or
                            self._turn_id or "",
                    topic=resolved_topic or "",
                )
                try:
                    await self._send_memory_snapshot()
                except Exception:
                    pass
                next_turn = self._rearm_turn(listening=True)
                self._set_state(RealtimeState.LISTENING)
                await self.send(EventType.LISTENING_STARTED, {
                    "listening": True,
                    "state": RealtimeState.LISTENING.value,
                    "turn_id": next_turn,
                    "can_send": False,
                    "segment_count": 0,
                    "auto_rearm": True,
                    "previous_answer": True,
                })
                print("[TURN] answer ready -> auto re-armed turn=%s "
                      "(mic/VAD/STT untouched)" % next_turn)

        except asyncio.CancelledError:
            try:
                await self.send(EventType.ANSWER_CANCELLED, {
                    "reason": "superseded",
                    "request_id": request_id,
                })
            except Exception:
                pass
            # A cancelled turn stays usable and the mic keeps running
            # (Phase 5.4.1 §1: CANCELLED -> LISTENING).
            if self._listening_mode:
                self._recover_after_failed_generation()
                await self.send(EventType.LISTENING_STARTED, {
                    "listening": self._listening_active,
                    "state": self._state.value,
                    "turn_id": self._turn_id,
                    "can_send": self.turn.can_send,
                    "previous_answer": True,
                })
            raise
        except ProviderUnavailableError as e:
            self._set_state(RealtimeState.PROVIDER_UNAVAILABLE)
            await self.send(EventType.SESSION_ERROR, {
                "code": "provider_unavailable",
                "message": str(e),
                "recoverable": True,
            })
            if self._listening_mode:
                self._recover_after_failed_generation()
            else:
                self._set_state(RealtimeState.LISTENING)
        except AIProviderError as e:
            await self.send(EventType.SESSION_ERROR, {
                "code": "answer_generation_failed",
                "message": str(e),
                "recoverable": True,
            })
            if self._listening_mode:
                self._recover_after_failed_generation()
            else:
                self._set_state(RealtimeState.LISTENING)
        except Exception as e:
            print(f"[Session {self.session.id}] Generation error: {e}\n{traceback.format_exc()}")
            try:
                await self.send(EventType.SESSION_ERROR, {
                    "code": "answer_generation_failed",
                    "message": str(e),
                    "recoverable": True,
                })
            except Exception:
                pass
            if self._listening_mode:
                self._recover_after_failed_generation()
            else:
                self._set_state(RealtimeState.LISTENING)
        finally:
            if drain is not None and not drain.done():
                drain.cancel()
            if self._current_request_id == request_id:
                self._answer_task = None
                self._current_request_id = None
                # In listening mode the turn owns its terminal state
                # (ANSWER_READY / CANCELLED / ERROR) so the user can see why
                # it ended and SEND again. Only auto-detect mode resumes.
                if self._running and not self._listening_mode:
                    self._set_state(RealtimeState.LISTENING)

    # ────────────────────────────────────────────────────────────────────────
    # Speculative generation (buffered until validated; never shown early)
    # ────────────────────────────────────────────────────────────────────────

    async def _drain(self, stream, q: asyncio.Queue, end_sentinel):
        """Pump provider tokens into a queue; signal end; never blocks callers.
        Draining into a queue (instead of awaiting the generator directly) keeps
        Task.cancel() reliable: cancellation delivered at q.get() is NOT absorbed
        by async-generator machinery the way it is with async-for in the consumer."""
        try:
            async for tok in stream:
                await q.put(tok)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            await q.put(end_sentinel)

    async def _flush_spec(self, request_id: str,
                          perf: Optional[PerformanceTracker] = None,
                          llm_t: Optional[float] = None):
        """Send the buffered speculative answer to the UI in small burst deltas."""
        chunk = ""
        while self._spec_sent < len(self._spec_tokens):
            chunk += self._spec_tokens[self._spec_sent]
            self._spec_sent += 1
            if len(chunk) >= 40:
                await self.send(EventType.ANSWER_STREAMED, {
                    "delta": chunk,
                    "request_id": request_id,
                })
                if perf is not None and llm_t is not None:
                    perf.first_visible(llm_t)
                chunk = ""
        if chunk:
            await self.send(EventType.ANSWER_STREAMED, {
                "delta": chunk,
                "request_id": request_id,
            })
            if perf is not None and llm_t is not None:
                perf.first_visible(llm_t)

    async def _speculate_answer(self, question: str, request_id: str, confidence: float):
        """Start generation on the *partial* question; buffer, never reveal,
        until the final transcript validates it. Release ⇒ burst-flush + live."""
        started = time.monotonic()
        drain: Optional[asyncio.Task] = None
        first_token_at: Optional[float] = None
        spec_perf = PerformanceTracker(request_id=request_id)
        self._spec_perf = spec_perf
        try:
            resolution = self._get_memory().resolve(question)
        except Exception:
            resolution = Resolution.identity(question)
        project_names, project_active, _gh = self._project_scope()
        analysis = analyze_question(
            resolution.resolved_question or question,
            source="voice",
            previous=getattr(self, "_last_analysis", None),
            followup=bool(resolution.followup),
            project_names=project_names,
            project_active=project_active,
        )
        if analysis.intent or analysis.requires_code:
            self._last_analysis = analysis
        try:
            self._current_request_id = request_id
            print(
                '[LLM] generation started request_id=%s speculative=1 question="%s"'
                % (request_id, question[:80])
            )
            project_info = None
            project_match = None
            if analysis.project_related:
                project_match = self._resolve_project_match(
                    resolution.resolved_question or question, resolution)
                if project_match is not None:
                    project_info = {
                        "id": project_match.project.project_id,
                        "name": project_match.project.project_name,
                        "repo": project_match.project.full_name,
                    }
            await self.send(EventType.ANSWER_GENERATING, {
                "agent": "realtime_answer",
                "question": question,
                "request_id": request_id,
                "speculative": True,
                "analysis": analysis.to_dict(),
                "project": project_info,
            })
            self._set_state(RealtimeState.RETRIEVING_CONTEXT)

            context, rag_note = await self._bounded_rag(
                question, resolution=resolution, perf=spec_perf)
            project_evidence = ""
            if analysis.project_related:
                ev, gh_note, _pi = await self._github_evidence(
                    resolution.resolved_question or question, analysis,
                    resolution, match=project_match)
                project_evidence = ev
                if gh_note:
                    rag_note = f"{rag_note}|{gh_note}"
            self._set_state(RealtimeState.GENERATING)

            memory = self._get_memory()
            ctx_start = time.monotonic()
            sections = ContextBuilder.memory_sections(memory, resolution)
            spec_perf.mark("context_construction")
            spec_perf.span("context_ms", ctx_start)

            prompt_start = time.monotonic()
            system = realtime_system(
                settings.answer_mode, requires_code=bool(analysis.requires_code))
            user_prompt = ContextBuilder.assemble(
                question=question,
                candidate_name=self.session.profile.name or "the candidate",
                context=context,
                memory=memory,
                resolution=resolution,
                sections=sections,
                analysis=analysis,
                project_evidence=project_evidence or None,
            )
            spec_perf.mark("prompt_construction")
            spec_perf.span("prompt_ms", prompt_start)

            spec_perf.mark("llm_request")
            llm_call_t = time.monotonic()
            stream = await self.provider.complete(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_prompt},
                ],
                stream=True,
                temperature=0.5,
                max_tokens=settings.realtime_max_output_tokens,
                request_id=request_id,
            )
            spec_perf.span("llm_request_ms", llm_call_t)
            self._set_state(RealtimeState.STREAMING_ANSWER)

            q: asyncio.Queue = asyncio.Queue()
            drain = asyncio.create_task(self._drain(stream, q, _SPEC_END))

            abandoned = False
            while True:
                if self._current_request_id != request_id:
                    return
                try:
                    tok = await asyncio.wait_for(q.get(), timeout=0.05)
                except asyncio.TimeoutError:
                    tok = None  # timer tick — lets release events land promptly

                # The final transcript validated this question ⇒ flush now.
                if self._spec_released:
                    await self._flush_spec(request_id, spec_perf, llm_call_t)

                if tok is None:
                    continue
                if tok is _SPEC_END:
                    # Stream finished. DO NOT discard the buffer: hold it and
                    # wait for the final transcript below.
                    break

                if first_token_at is None:
                    first_token_at = time.monotonic()
                    ttft_ms = (first_token_at - started) * 1000
                    spec_perf.mark("llm_ttft")
                    spec_perf.span("llm_ttft_ms", llm_call_t)
                    self._log_budget("llm_ttft", ttft_ms, settings.realtime_ttft_budget_ms)
                    print("[SPEC] first token after %.0fms" % ttft_ms)

                self._spec_tokens.append(tok)

                if self._spec_released:
                    continue  # _flush_spec above already covers released tokens

                # Cap buffered speculation — abandon so CPU frees for the
                # authoritative final generation if validation never lands.
                buffered_chars = sum(len(t) for t in self._spec_tokens)
                if (time.monotonic() - started) * 1000 > settings.realtime_spec_max_ms \
                        or buffered_chars > settings.realtime_spec_max_chars:
                    print(
                        "[SPEC] abandoned q=%r buffered=%d chars" % (question[:50], buffered_chars)
                    )
                    abandoned = True
                    break

            if self._current_request_id != request_id:
                return

            # Stream consumed but final transcript hasn't validated yet — hold
            # the completed buffer (zero CPU) until release or the deadline.
            if not self._spec_released and not abandoned:
                deadline = started + settings.realtime_spec_max_ms / 1000.0
                while not self._spec_released and time.monotonic() < deadline:
                    await asyncio.sleep(0.02)
                if not self._spec_released:
                    abandoned = True

            if self._current_request_id != request_id:
                return

            if not abandoned and self._spec_released:
                await self._flush_spec(request_id, spec_perf, llm_call_t)
                answer = "".join(self._spec_tokens).strip()
                self._answers_generated += 1
                spec_perf.mark("generation_complete")
                spec_perf.span("generation_ms", first_token_at or llm_call_t)
                spec_perf.finish()
                print(
                    "[SPEC] completed q=%r sim=%.2f buffered_sent=%d total_ms=%.0f"
                    % (question[:60], self._spec_similarity, len(self._spec_tokens),
                       (time.monotonic() - started) * 1000)
                )
                await self.send(EventType.ANSWER_GENERATED, {
                    "answer": answer,
                    "key_points": [],
                    "star": None,
                    "follow_up_questions": [],
                    "confidence": confidence,
                    "mode": settings.answer_mode,
                    "request_id": request_id,
                    "latency_ms": int((time.monotonic() - started) * 1000),
                    "ttft_ms": int((first_token_at - started) * 1000) if first_token_at else 0,
                    "rag": rag_note,
                    "speculative": True,
                    "spec_similarity": self._spec_similarity,
                })
                self._record_turn(
                    self._spec_final_question or question, answer, resolution)
                self._report_perf(spec_perf)
            else:
                print("[SPEC] abandoned-silently (never shown) q=%r" % question[:60])

        except asyncio.CancelledError:
            try:
                if not self._spec_released:
                    await self.send(EventType.ANSWER_CANCELLED, {
                        "reason": "superseded",
                        "request_id": request_id,
                    })
            except Exception:
                pass
            raise
        except ProviderUnavailableError as e:
            if not self._spec_released:
                self._set_state(RealtimeState.PROVIDER_UNAVAILABLE)
                await self.send(EventType.SESSION_ERROR, {
                    "code": "provider_unavailable",
                    "message": str(e),
                    "recoverable": True,
                })
                self._set_state(RealtimeState.LISTENING)
        except AIProviderError as e:
            if not self._spec_released:
                await self.send(EventType.SESSION_ERROR, {
                    "code": "answer_generation_failed",
                    "message": str(e),
                    "recoverable": True,
                })
                self._set_state(RealtimeState.LISTENING)
        except Exception as e:
            print(f"[Session {self.session.id}] Speculative error: {e}\n{traceback.format_exc()}")
            if not self._spec_released:
                try:
                    await self.send(EventType.SESSION_ERROR, {
                        "code": "answer_generation_failed",
                        "message": str(e),
                        "recoverable": True,
                    })
                except Exception:
                    pass
                self._set_state(RealtimeState.LISTENING)
        finally:
            if drain and not drain.done():
                drain.cancel()
            if self._current_request_id == request_id:
                self._spec_task = None
                self._spec_request_id = None
                # Keep _spec_question/_spec_released so the later authoritative
                # transcript can CONFIRM the already-released answer (and only a
                # supersession via _cancel_spec resets them).
                if self._running:
                    self._set_state(RealtimeState.LISTENING)

    # ────────────────────────────────────────────────────────────────────────
    # Text control messages
    # ────────────────────────────────────────────────────────────────────────

    async def _handle_text_message(self, text: str):
        try:
            msg = json.loads(text)
        except json.JSONDecodeError:
            return

        msg_type = msg.get("type")
        payload = msg.get("payload", {})

        # ── Interview Listening Mode (Phase 5.4) ───────────────────────────
        if msg_type == "mode.set":
            await self._set_listening_mode(
                payload.get("mode") == "interview_listening"
                or bool(payload.get("enabled")))
            return

        if msg_type == "listening.start":
            await self._start_listening()
            return

        if msg_type == "listening.stop":
            await self._stop_listening()
            return

        if msg_type == "turn.send":
            await self._submit_turn(
                text=payload.get("text") or "",
                turn_id=payload.get("turn_id"),
            )
            return

        if msg_type == "answer.cancel":
            self._cancel_inflight()
            if self._answer_task and not self._answer_task.done():
                self._answer_task.cancel()
            self._turn_submitting = False
            self._set_state(RealtimeState.CANCELLED)
            await self.send(EventType.ANSWER_CANCELLED,
                            {"reason": payload.get("reason", "user")})
            return

        if msg_type == "generate_answer":
            question = payload.get("question", "")
            if question:
                await self._trigger_question(
                    question, 1.0, -1,
                    note=payload.get("note", ""),
                    source=payload.get("source", ""),
                )

        elif msg_type == "transcript.text":
            content = payload.get("text", "")
            if not content:
                return
            # Listening mode: typed/pushed text is part of the open turn and is
            # never auto-submitted, exactly like spoken segments.
            if self._listening_mode:
                await self._append_typed_segment(content)
                return
            entry = self._transcripts.add_fragment(
                content, speaker=payload.get("speaker", "unknown")
            )
            segment_id = None
            if entry:
                segment = TranscriptSegment(
                    session_id=self.session.id,
                    speaker=entry.speaker,
                    content=entry.text,
                    timestamp_ms=int(time.time() * 1000),
                )
                self.db.add(segment)
                self.db.commit()
                segment_id = segment.id

            is_q, confidence = self._detector.detect(
                self._transcripts.current_utterance or content
            )
            if is_q or payload.get("force_answer"):
                await self._trigger_question(
                    self._transcripts.current_utterance or content,
                    confidence, segment_id,
                )

        elif msg_type == "pong":
            pass  # heartbeat response

        elif msg_type == "session.end":
            self._running = False

    # ────────────────────────────────────────────────────────────────────────
    # Interview Listening Mode (Phase 5.4)
    # ────────────────────────────────────────────────────────────────────────

    async def _set_listening_mode(self, enabled: bool):
        """Enable/disable Interview Listening Mode for this connection."""
        self._listening_mode = bool(enabled)
        if not self._listening_mode:
            self.turn.clear()
        self._set_state(RealtimeState.LISTENING if enabled
                        else RealtimeState.IDLE)
        await self.send(EventType.MODE_CHANGED, {
            "mode": "interview_listening" if enabled else "auto_detect",
            "listening_mode": self._listening_mode,
        })
        print(f"[TURN] listening_mode={self._listening_mode}")

    def _next_turn_id(self) -> str:
        """Advance to a new turn id (turn ownership, Phase 5.4.1 §8)."""
        self._turn_no = getattr(self, "_turn_no", 0) + 1
        self._turn_id = f"t{self._turn_no}"
        self._turn_seq = 0
        self._seg_seq = 0
        return self._turn_id

    def _rearm_turn(self, *, listening: bool = True) -> str:
        """Rotate to a fresh turn WITHOUT touching the mic, VAD or STT model.

        Called after an answer completes so the interviewer can speak again
        immediately (Phase 5.4.1 §5-§6). Resets only per-turn state:
        transcript buffer, VAD utterance segmentation, segment counter, SEND
        availability. The audio device, VAD and Whisper model are untouched —
        a reload here would cost seconds and drop the start of the question.
        """
        self._next_turn_id()
        self.turn.clear()          # fresh, open buffer (clear() un-pauses)
        self._turn_submitting = False
        self._turn_submitted_text = None
        self._turn_listen_ms = 0.0
        self._listening_active = bool(listening)
        if listening:
            # Segmentation state only — UtteranceReceiver keeps its config.
            try:
                self._receiver.reset()
            except Exception:
                pass
            self._turn_start_ts = time.monotonic()
        return self._turn_id

    async def _start_listening(self):
        """START LISTENING: fresh turn, listening state, nothing generated.

        Keeps the last answer, question history and memory on screen — only
        the live transcript and the current turn's audio state are reset
        (Phase 5.4.1 §21).
        """
        turn_id = self._rearm_turn(listening=True)
        self._set_state(RealtimeState.LISTENING)
        await self.send(EventType.LISTENING_STARTED, {
            "listening": True,
            "state": RealtimeState.LISTENING.value,
            "turn_id": turn_id,
            "can_send": False,
            "segment_count": 0,
        })
        print("[TURN] listening started turn=%s" % turn_id)

    async def _stop_listening(self):
        """STOP: end capture, KEEP the transcript, never generate."""
        self._listening_active = False
        self.turn.pause()
        # Rotate ownership immediately so an in-flight STT result can never
        # append into this stopped turn (Phase 5.4.1 §8/§20).
        self._next_turn_id()
        current = self.turn.get_current()
        if self._turn_start_ts is not None:
            self._turn_listen_ms = (time.monotonic() - self._turn_start_ts) * 1000
        await self.send(EventType.LISTENING_STOPPED, {
            "listening": False,
            "current": current,
            "char_count": len(current),
            "can_send": self.turn.can_send,
            "listen_ms": int(self._turn_listen_ms),
            # STOP must not generate anything.
            "generated": False,
        })
        # STOP is explicit and terminal: IDLE (Phase 5.4.1 §20). The answer,
        # history and memory all stay on screen; the transcript is kept so a
        # START can still send what was captured before STOP.
        self._set_state(RealtimeState.IDLE)
        print(f"[TURN] listening stopped chars={len(current)} "
              f"can_send={self.turn.can_send} state=IDLE")

    async def _append_typed_segment(self, content: str):
        """Append externally supplied text (manual entry) to the open turn."""
        self._turn_seq += 1
        stored = self.turn.append(content, segment_id=f"t{self._turn_seq}")
        current = self.turn.get_current()
        await self.send(EventType.TRANSCRIPT_SEGMENT, {
            "segment_id": f"t{self._turn_seq}",
            "text": content,
            "current": current,
            "duplicate": stored is None,
            "char_count": len(current),
            "can_send": self.turn.can_send,
            "segment_count": self.turn.segment_count,
            "source": "typed",
        })
        if self.turn.can_send:
            self._set_state(RealtimeState.READY_TO_SEND)

    async def _submit_turn(self, text: str = "", turn_id: str | None = None):
        """SEND — the authoritative, single-flight question submission.

        Order matters (spec §6/§7):
          1. refuse a second concurrent submission
          2. stop accepting new text
          3. bounded final flush of the trailing STT segment
          4. snapshot (never clear before acceptance)
          5. normalize + minimum-useful-length check
          6. exactly one generation
        """
        # 1. Single-flight: a repeated click while GENERATING is ignored.
        if self._turn_submitting:
            print("[TURN] send ignored: generation already in flight")
            await self.send(EventType.SESSION_ERROR, {
                "code": "already_generating",
                "message": "Already generating an answer.",
                "recoverable": True,
            })
            return

        # 2. Freeze input for this turn, and bank how long the user actually spoke.
        if self._turn_start_ts is not None:
            self._turn_listen_ms = (time.monotonic() - self._turn_start_ts) * 1000
        self.turn.pause()

        # 3. Bounded final flush so trailing speech is not silently discarded.
        await self._flush_turn_tail()

        # 4. Snapshot — the buffer is intentionally NOT cleared here, so a
        #    failed generation can be retried without losing the question.
        question = self.turn.snapshot()
        if text:
            question = normalize_whitespace(f"{question} {text}")

        # 5. Usability gate.
        if len(question) < settings.turn_min_useful_chars:
            self.turn.resume()
            self._turn_submitting = False
            self._set_state(RealtimeState.LISTENING)
            await self.send(EventType.SESSION_ERROR, {
                "code": "empty_turn",
                "message": "Nothing to send yet — keep listening.",
                "recoverable": True,
            })
            print("[TURN] send rejected: transcript too short")
            return

        self._turn_submitting = True
        self._turn_submitted_text = question
        # Ownership of the turn being submitted — recorded before the id
        # rotates at re-arm so history keeps the right turn_id.
        self._submitted_turn_id = self._turn_id or ""
        self._set_state(RealtimeState.GENERATING)
        await self.send(EventType.TURN_SUBMITTED, {
            "turn_id": self._submitted_turn_id,
            "question": question,
            "char_count": len(question),
            "segment_count": self.turn.segment_count,
            "listen_ms": int(self._turn_listen_ms),
        })
        print(f'[TURN] submitted chars={len(question)} q="{question[:140]}"')

        # Remember it for the Previous Questions panel.
        self.recent_questions.append({
            "question": question,
            "ts": time.time(),
        })
        del self.recent_questions[:-8]

        is_q, confidence = self._detector.detect(question)
        if not is_q:
            # SEND is authoritative: an explicit submission is never second-guessed
            # by the auto-detector.
            confidence = 1.0
        await self._trigger_question(question, confidence, -1, source="send")

    async def _flush_turn_tail(self):
        """Transcribe + append any speech buffered at SEND time (bounded).

        Uses the SAME `finalize_only` path as disconnect, and is a no-op when the
        last utterance was already finalized, so nothing is duplicated.
        """
        receiver = getattr(self, "_receiver", None)
        if receiver is None:
            return
        try:
            # `flush()` is a no-op when nothing is buffered (it just resets),
            # so it doubles as the "is anything pending?" check.
            utterance = receiver.flush()
        except Exception:
            return
        if utterance is None:
            return
        if utterance.audio.size == 0 or utterance.speech_seconds <= 0:
            return
        # The buffer is frozen for this turn, but this tail belongs to the very
        # question being submitted: briefly allow the append, then re-freeze.
        was_paused = self.turn.is_paused
        if was_paused:
            self.turn.resume()
        try:
            await asyncio.wait_for(
                self._handle_utterance(utterance, time.monotonic(),
                                       finalize_only=True,
                                       turn_id=self._turn_id),
                timeout=settings.turn_final_flush_timeout,
            )
        except asyncio.TimeoutError:
            print("[TURN] final flush timed out; using buffered transcript")
        except Exception as e:
            print(f"[TURN] final flush failed: {e}")
        finally:
            if was_paused:
                self.turn.pause()

    async def _send_memory_snapshot(self):
        """Push concise topic/previous-question context to the overlay.

        ``topic`` is preserved across answers, never blanked (§16), and
        ``previous`` carries the navigable history with a stable index.
        """
        memory = self._memory or self._get_memory()
        history = list(self.question_history)
        last_turn = getattr(self, "_submitted_turn_id", None)
        previous = [
            {"index": i + 1,
             "turn_id": h.get("turn_id", ""),
             "sequence": h.get("sequence"),
             "question": (h.get("question") or "")[:220],
             "answer_available": bool(h.get("answer")),
             "answer": (h.get("answer") or "")[:400],
             "topic": h.get("topic", "") or "",
             "ts": h.get("ts"),
             "current": h.get("turn_id") == last_turn}
            for i, h in enumerate(history[-8:])
        ]
        payload = {
            "topic": (getattr(memory, "active_topic", "") or "") or
                     (history[-1].get("topic", "") if history else ""),
            "project": getattr(memory, "active_project", "") or "",
            "previous": previous,
            "history_index": len(previous) - 1 if previous else -1,
            "history_count": len(previous),
            "memory_points": _memory_points(memory),
        }
        await self.send(EventType.MEMORY_UPDATED, payload)

    @property
    def question_history(self) -> list:
        """Ordered list of completed SENDs (Phase 5.4.1 §9).

        Session-scoped in-memory navigation index over the turns already
        persisted by ``_record_turn`` — no second source of truth.
        """
        hist = self.__dict__.get("_question_history")
        if hist is None:
            hist = []
            self.__dict__["_question_history"] = hist
        return hist

    def _record_question_history(self, *, question: str, answer: str,
                                 turn_id: str, topic: str = "") -> dict:
        """Append one completed turn to the navigable history."""
        hist = self.question_history
        entry = {
            "turn_id": turn_id or "",
            "sequence": len(hist) + 1,
            "question": question or "",
            "answer": answer or "",
            "answer_available": bool(answer),
            "topic": topic or "",
            "ts": time.time(),
        }
        hist.append(entry)
        return entry

    def _recover_after_failed_generation(self):
        """§19: keep the previous answer, release the turn, stay usable.

        The failed question's transcript is preserved so SEND can retry, and
        the mic stays live so the candidate can keep talking.
        """
        self._turn_submitting = False
        self.turn.resume() if self.turn.is_paused else None
        self._set_state(RealtimeState.READY_TO_SEND if self.turn.can_send
                        else RealtimeState.LISTENING)

    async def _trigger_question(self, question: str, confidence: float, segment_id: int,
                                note: str = "", source: str = ""):
        """Shared path for detected, manual and screenshot (OCR) questions.

        ``note`` is an optional per-question prompt suffix (e.g. a code-answer
        format hint for OCR'd screenshots); voice/manual paths pass nothing.
        ``source`` is recorded on the question-analysis metadata only.
        """
        print(
            '[QUESTION] detected="%s" confidence=%.2f source=%s'
            % (question[:160], confidence, source or ("manual" if segment_id == -1 else "text"))
        )
        self._set_state(RealtimeState.QUESTION_CANDIDATE)
        self._questions_detected += 1
        self._pending_question = question

        if segment_id and segment_id > 0:
            seg = self.db.query(TranscriptSegment).filter_by(id=segment_id).first()
            if seg:
                seg.is_question = True
                seg.confidence = confidence
                self.db.commit()

        await self.send(EventType.QUESTION_DETECTED, {
            "question": question,
            "confidence": confidence,
            "segment_id": segment_id,
        })

        request_id = self._next_request_id()
        self._cancel_inflight()
        if self._answer_task and not self._answer_task.done():
            self._answer_task.cancel()

        # Claim the request id before the new task starts (see _analyze_transcript).
        self._current_request_id = request_id
        self._answer_task = asyncio.create_task(
            self._generate_answer(
                question, request_id, confidence, note=note, source=source)
        )

    # ────────────────────────────────────────────────────────────────────────
    # State machine + cleanup
    # ────────────────────────────────────────────────────────────────────────

    def _set_state(self, state: RealtimeState):
        if state == self._state:
            return
        print(f"[REALTIME_STATE] {state.value}")
        self._state = state

    def _cancel_inflight_tasks(self):
        for task in (self._answer_task, self._prefetch_task, self._partial_task,
                     self._spec_task, self._audio_worker_task):
            if task and not task.done():
                task.cancel()

    def _cleanup(self):
        """Cancel all in-flight work and reset the speech receiver."""
        self._cancel_inflight_tasks()

        try:
            import asyncio as _a
            _loop = _a.get_event_loop()
            if _loop.is_running():
                _loop.call_soon_threadsafe(self._finalize_cleanup)
        except Exception:
            pass

    def _finalize_cleanup(self):
        self._receiver.reset()

    async def _flush_pending_speech(self):
        """Force-finalize a partially-spoken utterance on disconnect.

        Awaited (never spawned) so the transcript is persisted while the
        request-scoped DB session is still open.
        """
        utt = self._receiver.flush()
        if not utt:
            return
        try:
            await asyncio.wait_for(
                self._handle_utterance(utt, time.monotonic(), finalize_only=True),
                timeout=10.0,
            )
        except Exception as e:
            print(f"[FLUSH] pending speech finalize failed: {e}")

    async def close(self):
        self._running = False
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass