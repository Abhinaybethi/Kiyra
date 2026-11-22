"""Generation-speed regressions.

The three changes this locks down:

  1. The Ollama payload must carry the prefill batch sizes. Measured on the
     RTX 3050 / qwen3.5:4b: num_batch=1024 cut the prefill of a 699-token
     prompt from 1034 ms to 143 ms. Ollama reloads the runner when these
     change, so the startup warmup must send EXACTLY the same values as the
     live requests — otherwise the first interview question pays a ~20 s
     reload.
  2. The system prompt must stay short. Every token is re-processed on every
     turn (this model has no prefix caching), and a verbose rule also made
     the model overshoot the word band and get clipped mid-sentence.
  3. The first streamed chunk must be flushed immediately — the candidate
     should see text the moment the first token lands, not 0.3 s later.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from ai.provider import OllamaProvider
from config import settings
from realtime.prompts import realtime_system

DESKTOP_DIR = Path(__file__).resolve().parents[2] / "desktop"
sys.path.insert(0, str(DESKTOP_DIR))


# ── 1. the Ollama payload ───────────────────────────────────────────────────

class _FakeResponse:
    def __init__(self, payload: dict):
        self._lines = [
            json.dumps({"message": {"content": "hello"}, "done": False}),
            json.dumps({"message": {"content": ""}, "done": True,
                        "prompt_eval_count": 10, "eval_count": 2,
                        "done_reason": "stop"}),
        ]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        pass

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _FakeClient:
    """Captures the JSON payload the provider sends to Ollama."""

    payloads: list[dict] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, method, url, json=None):
        _FakeClient.payloads.append(json)
        return _FakeResponse(json)

    async def post(self, url, json=None):
        _FakeClient.payloads.append(json)
        return _FakeResponse(json)

    async def aclose(self):
        pass


@pytest.fixture
def captured_payloads(monkeypatch):
    _FakeClient.payloads = []
    monkeypatch.setattr("ai.provider.httpx.AsyncClient", _FakeClient)
    return _FakeClient.payloads


@pytest.mark.asyncio
async def test_ollama_payload_carries_the_prefill_batch_sizes(captured_payloads):
    provider = OllamaProvider(base_url="http://localhost:11434",
                              model="qwen3.5:4b")
    stream = await provider.complete(
        [{"role": "user", "content": "hi"}], stream=True, max_tokens=270)
    async for _ in stream:
        pass

    payload = captured_payloads[-1]
    options = payload["options"]
    assert options["num_batch"] == settings.ollama_num_batch
    assert options["num_ubatch"] == settings.ollama_num_ubatch
    assert options["num_ctx"] == settings.ollama_num_ctx
    # …and they are the values that actually make prefill fast.
    assert options["num_batch"] >= 512


@pytest.mark.asyncio
async def test_warmup_pins_the_same_options_as_a_live_request(captured_payloads):
    """A warmup that differs from the live request forces a runner reload."""
    provider = OllamaProvider(base_url="http://localhost:11434",
                              model="qwen3.5:4b")
    await provider.warmup()
    warmup_options = captured_payloads[-1]["options"]

    stream = await provider.complete(
        [{"role": "user", "content": "hi"}], stream=True, max_tokens=270)
    async for _ in stream:
        pass
    live_options = captured_payloads[-1]["options"]

    for key in ("num_ctx", "num_batch", "num_ubatch"):
        assert warmup_options.get(key) == live_options.get(key), (
            f"{key} differs between warmup and live request — Ollama would "
            f"reload the runner (~20 s) on the first question")


@pytest.mark.asyncio
async def test_ollama_payload_keeps_the_pinned_context_and_alive(captured_payloads):
    provider = OllamaProvider(base_url="http://localhost:11434",
                              model="qwen3.5:4b")
    stream = await provider.complete(
        [{"role": "user", "content": "hi"}], stream=True)
    async for _ in stream:
        pass
    payload = captured_payloads[-1]
    assert payload["keep_alive"] == settings.ollama_keep_alive
    assert payload["think"] is False  # qwen3 thinking mode would stall the answer


# ── 2. the system prompt ────────────────────────────────────────────────────

def test_system_prompt_keeps_every_rule():
    system = realtime_system("detailed")
    for phrase in (
        "150-190 words",
        "Hard minimum 130 words",
        "NEVER invent",
        "first person",
        "according to the resume",
        "FOLLOW-UPS",
        "PREVIOUS CONTEXT",
        "RECENT CONVERSATION",
        "never restate or continue an earlier answer",
        "never contradict",
        "factually consistent",
        "plain text only",
    ):
        assert phrase in system, phrase
    # The rules that invite the model to copy an earlier answer stay out.
    assert "stay consistent with the answers you already gave" not in system
    assert "stay consistent" not in system


# The system prompt length before the trim (measured on git HEAD). Every
# token is re-processed on every turn — this model has no prefix caching.
_OLD_SYSTEM_PROMPT_CHARS = 2330


def test_system_prompt_is_much_shorter_than_it_was():
    """The old prompt was 2330 chars (~580 tokens) on every single turn."""
    system = realtime_system("detailed")
    assert len(system) < _OLD_SYSTEM_PROMPT_CHARS, (
        f"system prompt grew to {len(system)} chars")
    assert len(system) <= _OLD_SYSTEM_PROMPT_CHARS * 0.90, (
        f"system prompt is {len(system)} chars — the trim lost its effect")


def test_coding_prompt_still_demands_a_runnable_code_block():
    coding = realtime_system("detailed", requires_code=True)
    assert "CODING QUESTION OVERRIDE" in coding
    assert "MUST contain a fenced code block" in coding
    assert "plain text only" in coding
    plain = realtime_system("detailed")
    assert "CODING QUESTION OVERRIDE" not in plain


# ── 3. the first chunk is flushed immediately ───────────────────────────────

@pytest.mark.asyncio
async def test_first_streamed_chunk_is_flushed_immediately(monkeypatch):
    """Text must reach the UI on the FIRST token, not after 40 characters."""
    import realtime.session as session_mod
    from realtime.session import EventType, LiveSession, RealtimeState
    from realtime.transcript import TranscriptBuffer
    from realtime.question_detector import QuestionDetector
    from types import SimpleNamespace

    class FakeWebSocket:
        def __init__(self):
            self.sent: list[dict] = []

        async def send_text(self, text: str):
            self.sent.append(json.loads(text))

        async def close(self):
            pass

    class SlowProvider:
        model_name = "test-model"
        error = None

        async def complete(self, messages, stream=False, **kwargs):
            if not stream:
                return "done"
            return self._stream()

        async def _stream(self):
            # Small tokens: the 40-char gate would swallow the first ones.
            for tok in ["A", "B", "C", "D", "E", "F", "G", "H"]:
                await asyncio.sleep(0.01)
                yield tok

    ws = FakeWebSocket()
    sess = LiveSession.__new__(LiveSession)
    sess.ws = ws
    sess.session = SimpleNamespace(id=7, mode="realtime",
                                   profile=SimpleNamespace(id=3, name="Test"))
    sess.db = SimpleNamespace(add=lambda *a, **k: None,
                               commit=lambda *a, **k: None,
                               query=lambda *a, **k: [])
    sess.provider = SlowProvider()
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

    monkeypatch.setattr(session_mod, "retrieve_context_sync",
                        lambda *a, **k: ("", {"embed_ms": 1.0, "search_ms": 1.0}))

    await sess._handle_text_message(json.dumps({
        "type": "generate_answer",
        "payload": {"question": "What is a closure?"},
    }))
    await asyncio.sleep(0.8)

    streamed = [e for e in ws.sent
                if e["type"] == EventType.ANSWER_STREAMED.value]
    assert streamed, "no streamed chunk was sent"
    # The FIRST streamed event carries the first token — not an empty delta
    # and not a batch of 40 characters.
    assert streamed[0]["payload"]["delta"].strip(), (
        "the first streamed chunk must not be empty")
    assert len(streamed[0]["payload"]["delta"]) <= 4, (
        "the first chunk must be flushed on the first token, not batched")


# ── 4. the overlay's top-right controls ─────────────────────────────────────

def test_overlay_has_no_hide_show_button():
    """The '▁' button was removed; the F9 hotkey still hides the overlay."""
    import stealth_overlay as so

    src = Path(so.__file__).read_text(encoding="utf-8")
    assert "btn_visibility" not in src, (
        "the top-right hide/show button must be gone")
    assert '_btn(ctrls, "▁"' not in src, (
        "the top-right hide/show button must not be created")
    # The hotkey and the method it calls are untouched.
    assert '<F9>' in src
    assert "def toggle_visibility" in src
    assert "toggle_visibility" in src
