"""The dev-auth bypass must not silently disable the live audio WebSocket.

Regression: with no OTP token, `_ws_main` used to return before connecting, so
loopback frames were queued locally but never reached the backend and SEND
fell back to the slow, non-streaming HTTP answer path.
"""
from __future__ import annotations

import asyncio
import json
import queue
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "desktop"))

import stealth_overlay as so  # noqa: E402


class _FakeWS:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def recv(self):
        await asyncio.sleep(30)


def _app(dev_bypass: bool, statuses: list):
    app = so.StealthOverlayApp.__new__(so.StealthOverlayApp)
    app.auth_token = None
    app.dev_bypass = dev_bypass
    app.api_url = "http://127.0.0.1:8000"
    app.session_id = 7
    app._stop_event = threading.Event()
    app._ui_set_status = lambda msg, ok=False: statuses.append(msg)
    app._audio_queue = queue.Queue(maxsize=2)
    return app


async def _run_ws_main(app, connect):
    original = so.websockets.connect
    so.websockets.connect = connect
    task = asyncio.create_task(app._ws_main())
    try:
        await asyncio.sleep(0.1)
    finally:
        app._stop_event.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    so.websockets.connect = original


@pytest.mark.asyncio
async def test_ws_connects_without_token_when_dev_bypass(monkeypatch):
    calls = []

    def connect(url, **kwargs):
        calls.append((url, kwargs))
        return _FakeWS()

    await _run_ws_main(_app(True, []), connect)
    assert calls, "dev bypass must still open the live audio WebSocket"
    assert calls[0][0].endswith("/api/interviews/7/ws")
    assert "additional_headers" not in calls[0][1], "no empty Bearer header"


@pytest.mark.asyncio
async def test_ws_still_requires_token_in_strict_mode():
    statuses = []
    calls = []

    def connect(url, **kwargs):
        calls.append(url)
        return _FakeWS()

    await _run_ws_main(_app(False, statuses), connect)
    assert calls == []
    assert any("Sign in required" in s for s in statuses)


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


def test_skip_setup_only_honoured_by_live_dev_bypass(monkeypatch):
    seen = []

    def fake_urlopen(url, *args, **kwargs):
        seen.append(url)
        return _Response({"dev_bypass": True})

    monkeypatch.setattr(so.urllib.request, "urlopen", fake_urlopen)
    setup = so._dev_setup_for_skip("http://127.0.0.1:8000/")
    assert seen == ["http://127.0.0.1:8000/api/auth/status"]
    assert setup["dev_bypass"] is True
    assert setup["auth_token"] is None

    monkeypatch.setattr(so.urllib.request, "urlopen",
                        lambda *a, **k: _Response({"dev_bypass": False}))
    assert so._dev_setup_for_skip("http://127.0.0.1:8000") is None


def test_skip_setup_flag_is_only_a_request():
    opts = so._parse_args(["stealth_overlay.py", "--skip-setup", "--keep-backend"])
    assert opts["skip_setup"] is True
    assert opts["stop_on_exit"] is False
