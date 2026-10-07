"""Phase-5.2 behavior tests: GitHub-grounded project answers end-to-end through
the live answer path (analyzer → project selection → retrieval → PROJECT
EVIDENCE section → prompt), plus the anti-leak / anti-guess guarantees.

GitHub is fully stubbed here: no network, no token, no real repo.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import realtime.session as session_mod
from realtime.question_detector import QuestionDetector
from realtime.session import EventType, LiveSession, RealtimeState
from realtime.transcript import TranscriptBuffer
from github_repo.config import ProjectSource
from github_repo.retrieval import ProjectChunkHit


PROJECT_A = ProjectSource("p1", "ShopSense", "me", "shopsense")
PROJECT_B = ProjectSource("p2", "Bus Booking", "me", "bus-booking")

HITS = [
    ProjectChunkHit(
        content="const jwt = require('jsonwebtoken');\n"
                "module.exports = function auth(req, res, next) {\n"
                "  const token = req.headers.authorization;\n"
                "  res.locals.user = jwt.verify(token, SECRET);\n"
                "  next();\n};",
        metadata={"source": "github", "project_id": "p1",
                  "repo": "me/shopsense", "language": "javascript",
                  "path": "backend/middleware/auth.js",
                  "start_line": 3, "end_line": 8},
        score=0.87),
    ProjectChunkHit(
        content="module.exports = new Sequelize('shop', { dialect: 'postgres' });",
        metadata={"source": "github", "project_id": "p1",
                  "repo": "me/shopsense", "language": "javascript",
                  "path": "backend/config/database.js",
                  "start_line": 1, "end_line": 1},
        score=0.42),
]


class FakeRetriever:
    """Stub GitHubRetriever: records calls, returns canned hits."""

    def __init__(self, hits=HITS):
        self._hits = list(hits)
        self.index_calls = 0
        self.retrieve_calls: list[tuple] = []

    def ensure_indexed(self, project, force_refresh: bool = False):
        self.index_calls += 1

    def retrieve(self, project, query, *, intent: str = "", top_k=None,
                 min_score: float = 0.0):
        self.retrieve_calls.append((project.project_id, query, intent))
        return list(self._hits)


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
    stream_tokens = ["first ", "second ", "third "]
    token_delay = 0.005

    async def _stream(self):
        for tok in self.stream_tokens:
            await asyncio.sleep(self.token_delay)
            yield tok

    async def complete(self, messages, stream=False, **kwargs):
        if stream:
            return self._stream()
        return "".join(self.stream_tokens)


def make_session(provider):
    sess = LiveSession.__new__(LiveSession)
    sess.ws = FakeWebSocket()
    sess.session = SimpleNamespace(
        id=7, mode="realtime",
        profile=SimpleNamespace(id=3, name="Test Candidate"))
    sess.db = SimpleNamespace(
        add=lambda *a, **k: None, commit=lambda *a, **k: None,
        query=lambda *a, **k: [], filter_by=lambda *a, **k: None)
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


def _stub_github(monkeypatch, projects, hits=HITS):
    """Wire a stub retriever + configured projects into the session module."""
    retriever = FakeRetriever(hits)
    monkeypatch.setattr(session_mod, "_get_retriever", lambda: retriever)
    monkeypatch.setattr(session_mod, "configured_projects", lambda: list(projects))
    monkeypatch.setattr(
        session_mod, "project_display_names",
        lambda: [n for p in projects for n in
                 (p.project_name, p.project_id, p.github_repo)])
    # resolve_project() lives in github_repo.config and resolves names itself.
    import github_repo.config as gh_cfg
    monkeypatch.setattr(gh_cfg, "configured_projects", lambda: list(projects))
    monkeypatch.setattr(
        gh_cfg, "project_display_names",
        lambda: [n for p in projects for n in
                 (p.project_name, p.project_id, p.github_repo)])
    monkeypatch.setattr(
        session_mod, "retrieve_context_sync",
        lambda *a, **k: ("", {"embed_ms": 1.0, "search_ms": 1.0}))
    return retriever


async def _ask(sess, question, monkeypatch):
    seen: dict = {}

    async def fake_complete(messages, stream=False, **kwargs):
        seen["system"] = messages[0]["content"]
        seen["user"] = messages[1]["content"]
        return sess.provider._stream()

    monkeypatch.setattr(sess.provider, "complete", fake_complete)
    await sess._handle_text_message(json.dumps({
        "type": "generate_answer",
        "payload": {"question": question},
    }))
    await asyncio.sleep(0.5)
    return seen


def _generating(sess) -> dict:
    events = [e for e in sess.ws.sent
              if e["type"] == EventType.ANSWER_GENERATING.value]
    assert events, "no answer.generating event"
    return events[-1]["payload"]


# ── Mode B: project question gets grounded in real repo code ──────────────

@pytest.mark.asyncio
async def test_project_question_injects_grounded_evidence(monkeypatch):
    sess = make_session(FakeProvider())
    retriever = _stub_github(monkeypatch, [PROJECT_A])
    seen = await _ask(
        sess, "How did I implement JWT authentication in my project?", monkeypatch)

    user = seen.get("user", "")
    assert "PROJECT EVIDENCE" in user
    assert "PROJECT GROUNDING MODE" in user
    assert "backend/middleware/auth.js" in user          # real repo path
    assert "jsonwebtoken" in user                        # real repo content
    assert "ShopSense" in user                           # project name
    assert retriever.index_calls == 1
    assert retriever.retrieve_calls and \
        retriever.retrieve_calls[0][0] == "p1"

    payload = _generating(sess)
    assert payload["analysis"]["project_related"] is True
    assert payload["analysis"]["project_intent"] == "project_implementation"
    assert payload["project"]["repo"] == "me/shopsense"

    generated = [e for e in sess.ws.sent
                 if e["type"] == EventType.ANSWER_GENERATED.value]
    assert generated and "github:" in generated[-1]["payload"]["rag"]


@pytest.mark.asyncio
async def test_project_code_question_requests_fenced_code(monkeypatch):
    sess = make_session(FakeProvider())
    _stub_github(monkeypatch, [PROJECT_A])
    seen = await _ask(
        sess, "Show me the code for my auth middleware in my project", monkeypatch)

    user = seen.get("user", "")
    assert "PROJECT EVIDENCE" in user
    assert "PROJECT CODE MODE" in user
    assert "backend/middleware/auth.js" in user
    payload = _generating(sess)
    assert payload["analysis"]["project_intent"] == "project_code"
    assert payload["analysis"]["requires_code"] is True


@pytest.mark.asyncio
async def test_explicit_project_name_selects_that_repository(monkeypatch):
    sess = make_session(FakeProvider())
    retriever = _stub_github(monkeypatch, [PROJECT_A, PROJECT_B])
    await _ask(sess, "Tell me about Bus Booking", monkeypatch)
    payload = _generating(sess)
    assert payload["project"]["id"] == "p2"
    assert retriever.retrieve_calls[-1][0] == "p2"


# ── Mode A: generic questions never reach GitHub ─────────────────────────

@pytest.mark.asyncio
async def test_generic_question_gets_no_project_evidence(monkeypatch):
    sess = make_session(FakeProvider())
    retriever = _stub_github(monkeypatch, [PROJECT_A])
    seen = await _ask(sess, "Explain the Node.js event loop", monkeypatch)

    user = seen.get("user", "")
    assert "PROJECT EVIDENCE" not in user
    assert "PROJECT GROUNDING MODE" not in user
    payload = _generating(sess)
    assert payload["analysis"]["project_related"] is False
    assert payload["project"] is None
    assert retriever.index_calls == 0
    assert retriever.retrieve_calls == []


@pytest.mark.asyncio
async def test_generic_jwt_question_with_active_project_stays_generic(monkeypatch):
    """'What is JWT?' must never be boosted into Mode B by an active project."""
    sess = make_session(FakeProvider())
    retriever = _stub_github(monkeypatch, [PROJECT_A])
    seen = await _ask(sess, "What is JWT?", monkeypatch)
    assert "PROJECT EVIDENCE" not in seen.get("user", "")
    assert retriever.retrieve_calls == []


# ── Never guess between multiple projects ────────────────────────────────

@pytest.mark.asyncio
async def test_ambiguous_multiple_projects_never_guesses(monkeypatch):
    sess = make_session(FakeProvider())
    retriever = _stub_github(monkeypatch, [PROJECT_A, PROJECT_B])
    seen = await _ask(
        sess, "How did you handle payments in my project?", monkeypatch)

    user = seen.get("user", "")
    assert "PROJECT EVIDENCE" not in user
    assert retriever.retrieve_calls == []          # no repo was chosen
    payload = _generating(sess)
    assert payload["analysis"]["project_related"] is True
    assert payload["project"] is None             # ambiguous → ask, don't guess
    generated = [e for e in sess.ws.sent
                 if e["type"] == EventType.ANSWER_GENERATED.value]
    assert "github-ambiguous" in generated[-1]["payload"]["rag"]


@pytest.mark.asyncio
async def test_followup_keeps_the_active_project(monkeypatch):
    sess = make_session(FakeProvider())
    retriever = _stub_github(monkeypatch, [PROJECT_A, PROJECT_B])
    await _ask(sess, "Tell me about Bus Booking", monkeypatch)
    assert sess._active_project_id == "p2"
    retriever.retrieve_calls.clear()
    await _ask(sess, "How did you implement its database schema?", monkeypatch)
    assert retriever.retrieve_calls
    assert retriever.retrieve_calls[-1][0] == "p2"


# ── Degradation: GitHub unavailable must never fail an answer ────────────

@pytest.mark.asyncio
async def test_unconfigured_github_degrades_gracefully(monkeypatch):
    sess = make_session(FakeProvider())
    monkeypatch.setattr(session_mod, "_get_retriever", lambda: None)
    monkeypatch.setattr(
        session_mod, "retrieve_context_sync",
        lambda *a, **k: ("[PROJECT]: resume text", {"chunks": 1}))
    seen = await _ask(
        sess, "How did I implement JWT authentication in my project?", monkeypatch)

    user = seen.get("user", "")
    assert "PROJECT EVIDENCE" not in user
    assert "[PROJECT]: resume text" in user        # normal RAG still present
    generated = [e for e in sess.ws.sent
                 if e["type"] == EventType.ANSWER_GENERATED.value]
    assert generated                               # answer still delivered


@pytest.mark.asyncio
async def test_retrieval_failure_degrades_gracefully(monkeypatch):
    sess = make_session(FakeProvider())
    retriever = _stub_github(monkeypatch, [PROJECT_A])

    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(retriever, "ensure_indexed", boom)
    seen = await _ask(
        sess, "How did I implement JWT authentication in my project?", monkeypatch)
    assert "PROJECT EVIDENCE" not in seen.get("user", "")
    generated = [e for e in sess.ws.sent
                 if e["type"] == EventType.ANSWER_GENERATED.value]
    assert generated
    assert "github-error" in generated[-1]["payload"]["rag"]
