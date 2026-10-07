"""Model-server autostart: ModelServerLauncher reuse contract + probe reaping.

The failure this prevents: ``ensure_provider_ready()`` turns a missing
llama-server into an opaque 503 on session start, which surfaced as
"listening never starts" with a healthy-looking /health.

TEST A  reuse            — an already-serving model server is never restarted
TEST B  start            — a down server is started GPU-first, marked ours
TEST C  failures         — never raises; returns (False, actionable message)
TEST D  stop contract    — stops only what this process started
TEST E  probe reaping    — the scratch-port GPU probe is always reaped, and is
                           reaped BEFORE the real server loads, so repeated
                           starts cannot exhaust VRAM (verified: 12 leaked
                           probes left 537 MiB free and Bonsai stopped loading)
"""
from __future__ import annotations

import sys
import urllib.error
from pathlib import Path

import pytest

DESKTOP_DIR = Path(__file__).resolve().parents[2] / "desktop"
sys.path.insert(0, str(DESKTOP_DIR))

import llama_server as ls  # noqa: E402

LAUNCHER = ls.ModelServerLauncher
MGR = ls.LlamaCppServerManager


# ── Fakes ────────────────────────────────────────────────────────────────────
class _FakeProc:
    """Minimal Popen stand-in that records lifecycle calls."""

    def __init__(self, tag="p"):
        self.tag = tag
        self.pid = abs(id(self)) % 100000
        self.terminated = False

    def poll(self):
        return 0 if self.terminated else None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.terminated = True


class _FakeManager:
    """Manager double; `raise_on_start` simulates a total start failure."""

    instances: list = []

    def __init__(self, raise_on_start=None, report=None, **kwargs):
        self.kwargs = kwargs
        self.raise_on_start = raise_on_start
        self.proc = _FakeProc("managed")
        self.stopped = False
        self._report = report or {"proc": self.proc, "pid": self.proc.pid,
                                  "mode": "gpu", "evidence": {}}
        _FakeManager.instances.append(self)

    def start(self, prefer_gpu=True):
        if self.raise_on_start is not None:
            raise self.raise_on_start
        return self._report

    def stop(self, timeout=10.0):
        self.stopped = True


@pytest.fixture(autouse=True)
def _reset_fakes():
    _FakeManager.instances.clear()
    yield
    _FakeManager.instances.clear()


def _launcher(**kwargs):
    kwargs.setdefault("port", 8080)
    kwargs.setdefault("host", "127.0.0.1")
    return LAUNCHER(**kwargs)


# ─────────────────────────────────────────────────────────────────────────────
# TEST A — reuse an already-serving model server
# ─────────────────────────────────────────────────────────────────────────────
def test_ensure_reuses_running_server_without_starting(monkeypatch):
    launcher = _launcher(manager_factory=_FakeManager)
    monkeypatch.setattr(launcher, "is_running", lambda timeout=3.0: True)

    ready, message = launcher.ensure()

    assert ready is True
    assert "already running" in message
    assert launcher.started_here is False
    assert launcher.proc is None
    assert _FakeManager.instances == [], "must not construct a manager"


def test_is_running_requires_a_model_descriptor(monkeypatch):
    launcher = _launcher()

    class _Resp:
        status = 200

        def __init__(self, body):
            self._body = body

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    # A 200 that is not a llama.cpp /v1/models payload is NOT a model server.
    monkeypatch.setattr(ls.urllib.request, "urlopen",
                        lambda *a, **k: _Resp(b"<html>hello</html>"))
    assert launcher.is_running() is False

    monkeypatch.setattr(ls.urllib.request, "urlopen",
                        lambda *a, **k: _Resp(b'{"data":[{"id":"bonsai"}]}'))
    assert launcher.is_running() is True


def test_is_running_false_when_unreachable(monkeypatch):
    launcher = _launcher()

    def _boom(*a, **k):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(ls.urllib.request, "urlopen", _boom)
    assert launcher.is_running() is False


# ─────────────────────────────────────────────────────────────────────────────
# TEST B — start a down model server
# ─────────────────────────────────────────────────────────────────────────────
def test_ensure_starts_server_when_down(monkeypatch):
    launcher = _launcher(manager_factory=_FakeManager)
    monkeypatch.setattr(launcher, "is_running", lambda timeout=3.0: False)
    monkeypatch.setattr(launcher, "port_in_use", lambda timeout=0.5: False)

    ready, message = launcher.ensure()

    assert ready is True
    assert launcher.started_here is True
    assert launcher.proc is not None
    assert "GPU" in message and "8080" in message
    assert len(_FakeManager.instances) == 1
    # The manager is told which model/port/host to serve.
    kwargs = _FakeManager.instances[0].kwargs
    assert kwargs["port"] == 8080
    assert kwargs["host"] == "127.0.0.1"


def test_ensure_refuses_when_port_busy_but_not_serving(monkeypatch):
    launcher = _launcher(manager_factory=_FakeManager)
    monkeypatch.setattr(launcher, "is_running", lambda timeout=3.0: False)
    monkeypatch.setattr(launcher, "port_in_use", lambda timeout=0.5: True)

    ready, message = launcher.ensure()

    assert ready is False
    assert "8080" in message
    assert _FakeManager.instances == [], "must not fight for an occupied port"
    assert launcher.started_here is False


# ─────────────────────────────────────────────────────────────────────────────
# TEST C — never raises
# ─────────────────────────────────────────────────────────────────────────────
def test_ensure_reports_runtimeerror_without_raising(monkeypatch):
    launcher = _launcher(
        manager_factory=lambda **kw: _FakeManager(
            raise_on_start=RuntimeError("CPU llama-server failed"), **kw))
    monkeypatch.setattr(launcher, "is_running", lambda timeout=3.0: False)
    monkeypatch.setattr(launcher, "port_in_use", lambda timeout=0.5: False)

    ready, message = launcher.ensure()

    assert ready is False
    assert "failed to start" in message
    assert "CPU llama-server failed" in message
    assert launcher.started_here is False


def test_ensure_reports_unexpected_exception_without_raising(monkeypatch):
    launcher = _launcher(
        manager_factory=lambda **kw: _FakeManager(
            raise_on_start=OSError("access denied"), **kw))
    monkeypatch.setattr(launcher, "is_running", lambda timeout=3.0: False)
    monkeypatch.setattr(launcher, "port_in_use", lambda timeout=0.5: False)

    ready, message = launcher.ensure()
    assert ready is False
    assert "access denied" in message


# ─────────────────────────────────────────────────────────────────────────────
# TEST D — stop contract
# ─────────────────────────────────────────────────────────────────────────────
def test_stop_is_noop_for_a_reused_server():
    launcher = _launcher(manager_factory=_FakeManager)
    # Reuse path: nothing of ours is running.
    launcher.proc = _FakeProc("foreign")
    launcher.started_here = False

    launcher.stop()  # must not raise, must not touch the foreign process

    assert launcher.proc.terminated is False


def test_stop_terminates_only_what_we_started(monkeypatch):
    launcher = _launcher(manager_factory=_FakeManager)
    monkeypatch.setattr(launcher, "is_running", lambda timeout=3.0: False)
    monkeypatch.setattr(launcher, "port_in_use", lambda timeout=0.5: False)
    launcher.ensure()
    manager = _FakeManager.instances[0]

    launcher.stop()

    assert manager.stopped is True, "the started server must be stopped"
    assert launcher.started_here is True


def test_stop_survives_a_manager_without_stop(monkeypatch):
    class _NoStopManager(_FakeManager):
        stop = None  # attribute exists but is not callable

    launcher = _launcher(manager_factory=_NoStopManager)
    monkeypatch.setattr(launcher, "is_running", lambda timeout=3.0: False)
    monkeypatch.setattr(launcher, "port_in_use", lambda timeout=0.5: False)
    launcher.ensure()

    launcher.stop()  # must not raise


# ─────────────────────────────────────────────────────────────────────────────
# TEST E — the GPU probe is always reaped (VRAM leak regression)
# ─────────────────────────────────────────────────────────────────────────────
def _drive_try_gpu(monkeypatch, *, probe_healthy, offload_count):
    """Run _try_gpu with fakes; return (events, manager).

    `events` records launch/stop ordering so we can assert the probe dies
    before the real server is launched.
    """
    m = MGR(port=8080, log_path="probe-reap.log")
    events: list = []
    counter = {"n": 0}

    def fake_launch(cmd, log_path=None):
        counter["n"] += 1
        proc = _FakeProc(f"proc{counter['n']}")
        events.append(("launch", proc.tag, cmd))
        m.proc = proc
        return proc

    def fake_poll(port=None, timeout_s=None):
        # Only the scratch probe's health decides the offload branch.
        if port is not None:
            return probe_healthy
        return True  # the real port comes up

    def fake_evidence(log_path, requested_layers=None):
        return {"offloaded_count": offload_count, "device": "CUDA0",
                "offloaded": f"{offload_count}/37"}

    def fake_stop(proc):
        events.append(("stop", proc.tag))
        proc.terminated = True

    monkeypatch.setattr(m, "_launch", fake_launch)
    monkeypatch.setattr(m, "_poll_health", fake_poll)
    monkeypatch.setattr(m, "_offload_evidence", fake_evidence)
    monkeypatch.setattr(m, "_stop", staticmethod(fake_stop))
    monkeypatch.setattr(m, "_scratch_port", lambda: 18080)
    return m, events


def test_probe_is_reaped_before_promoting_to_the_real_port(monkeypatch):
    m, events = _drive_try_gpu(monkeypatch, probe_healthy=True,
                               offload_count=37)

    result = m._try_gpu({"name": "RTX", "vram_mib": 6144, "driver": "1"})

    assert result is not None, "a proven-offload probe must promote"
    # Exactly: launch the probe, reap it, launch the real server. The real
    # server is deliberately left running.
    assert [e[0] for e in events] == ["launch", "stop", "launch"], (
        f"probe must be reaped between the probe and the real launch: {events}")
    assert events[0][1] == "proc1" and events[1][1] == "proc1", (
        f"the probe process itself must be the one reaped: {events}")
    stop_index = [i for i, e in enumerate(events) if e[0] == "stop"][0]
    real_launch = [i for i, e in enumerate(events) if e[0] == "launch"][1]
    assert stop_index < real_launch, (
        "the probe still holds VRAM while the real model loads -> OOM")


def test_probe_is_reaped_when_it_fails(monkeypatch):
    m, events = _drive_try_gpu(monkeypatch, probe_healthy=False,
                               offload_count=0)

    assert m._try_gpu({"name": "RTX", "vram_mib": 6144, "driver": "1"}) is None

    stops = [e for e in events if e[0] == "stop"]
    launches = [e for e in events if e[0] == "launch"]
    assert len(stops) == len(launches), (
        f"every probe must be stopped: launches={launches} stops={stops}")


def test_manager_stop_reaps_recorded_probes(monkeypatch):
    m = MGR(port=8080)
    probe = _FakeProc("probe")
    real = _FakeProc("real")
    m._probes.append(probe)
    m.proc = real
    monkeypatch.setattr(m, "_port_open", lambda port=None: False)

    m.stop()

    assert probe.terminated is True
    assert real.terminated is True
    assert m._probes == []