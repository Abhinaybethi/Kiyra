"""Phase-5.2b tests: the overlay auto-starts and connects to the backend.

`python stealth_overlay.py` must work as a single command, so main() now
ensures the backend is serving before the overlay opens.

TEST 1  api_url parsing        — host/port extraction + defaults
TEST 2  interpreter resolution — env > backend/.venv > sys.executable
TEST 3  uvicorn argv           — correct host/port, no --reload
TEST 4  reuse when healthy     — an existing backend is never re-launched
TEST 5  port squat guard       — busy port without /health fails fast
TEST 6  launch failure paths   — missing interpreter / backend dir
TEST 7  stop only kills ours    — never-started launcher is a no-op
TEST 8  log tail               — failure messages carry actionable output
TEST 9  _ensure_backend        — returns a launcher ONLY when we started it
TEST 10 flag parsing           — --no-backend / --api-url / timeouts
"""
from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path

import pytest

DESKTOP_DIR = Path(__file__).resolve().parents[2] / "desktop"
sys.path.insert(0, str(DESKTOP_DIR))

import backend_server as bs  # noqa: E402

LAUNCHER = bs.BackendLauncher


def _drain(sock):
    """Accept-and-drop connections until the socket is closed."""
    try:
        while True:
            sock.accept()
    except OSError:
        pass          # socket closed by the test teardown


# ── TEST 1: api_url parsing ──────────────────────────────────────────────────

def test_parse_api_url_variants():
    assert bs.parse_api_url("http://localhost:8000") == ("localhost", 8000)
    assert bs.parse_api_url("http://localhost:8000/") == ("localhost", 8000)
    assert bs.parse_api_url("https://api.example.com") == ("api.example.com", 8000)
    assert bs.parse_api_url("127.0.0.1:9999") == ("127.0.0.1", 9999)


def test_launcher_defaults_point_at_local_backend():
    l = LAUNCHER()
    assert l.host == "localhost"
    assert l.port == 8000
    assert l.health_url() == "http://localhost:8000/health"
    assert l.log_path.endswith("kiyra-backend.log")
    # Default: keep the backend warm between runs.
    assert l.started_here is False


# ── TEST 2: interpreter resolution ───────────────────────────────────────────

def test_resolve_python_prefers_env(monkeypatch):
    monkeypatch.setenv("KIYRA_BACKEND_PYTHON", "D:/custom/python.exe")
    assert LAUNCHER().resolve_python() == "D:/custom/python.exe"


def test_resolve_python_prefers_backend_venv(monkeypatch):
    monkeypatch.delenv("KIYRA_BACKEND_PYTHON", raising=False)
    l = LAUNCHER()
    if not (Path(l.backend_dir) / ".venv").is_dir():
        pytest.skip("backend/.venv not present on this machine")
    assert l.resolve_python().endswith("python.exe")


def test_resolve_python_explicit_argument_wins(monkeypatch):
    monkeypatch.setenv("KIYRA_BACKEND_PYTHON", "D:/env/python.exe")
    assert LAUNCHER(python_exe="E:/explicit/python.exe").resolve_python() \
        == "E:/explicit/python.exe"


# ── TEST 3: uvicorn argv ─────────────────────────────────────────────────────

def test_build_command_uses_uvicorn_without_reload():
    l = LAUNCHER(python_exe="py")
    cmd = l.build_command()
    assert cmd[:3] == ["py", "-m", "uvicorn"]
    assert "main:app" in cmd
    assert "--host" in cmd and "--port" in cmd
    assert str(l.port) in cmd
    # --reload would spawn a grandchild that outlives our shutdown.
    assert "--reload" not in cmd
    assert l.command == cmd


# ── TEST 4: reuse an already-running backend ─────────────────────────────────

def test_ensure_reuses_running_backend(monkeypatch):
    l = LAUNCHER()
    monkeypatch.setattr(l, "is_running", lambda timeout=2.0: True)
    monkeypatch.setattr(
        LAUNCHER, "start",
        lambda self: pytest.fail("must not launch a second backend"))
    ready, msg = l.ensure()
    assert ready is True
    assert "already running" in msg
    # A reused backend is NOT ours to stop.
    assert l.started_here is False


# ── TEST 5: port squat guard ─────────────────────────────────────────────────

def test_busy_port_without_health_is_rejected():
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.listen(8)
    threading.Thread(
        target=lambda: _drain(srv), daemon=True).start()
    try:
        time.sleep(0.2)
        l = LAUNCHER(api_url=f"http://127.0.0.1:{port}", poll_interval=0.1)
        assert l.port_in_use() is True
        assert l.is_running(timeout=0.5) is False
        ready, msg = l.ensure()
        assert ready is False
        assert "busy" in msg
        assert l.started_here is False      # refused before spawning anything
    finally:
        srv.close()


# ── TEST 6: launch failure paths ─────────────────────────────────────────────

def test_start_fails_clearly_when_interpreter_missing():
    l = LAUNCHER(python_exe="C:/definitely/not/python.exe")
    ready, msg = l.start()
    assert ready is False
    assert "not found" in msg
    assert l.proc is None


def test_start_fails_clearly_when_backend_dir_missing():
    l = LAUNCHER(backend_dir="C:/no/such/backend")
    ready, msg = l.start()
    assert ready is False
    assert "backend directory not found" in msg


# ── TEST 7: stop semantics ───────────────────────────────────────────────────

def test_stop_is_noop_for_backend_we_did_not_start():
    LAUNCHER().stop()          # must not raise, must not touch anything


def test_stop_terminates_only_started_process(monkeypatch):
    l = LAUNCHER()
    killed = []

    class _FakeProc:
        pid = 4242
        returncode = None

        def poll(self):
            return None

        def terminate(self):
            killed.append("terminate")

        def kill(self):
            killed.append("kill")

        def wait(self, timeout=None):
            return 0

    l.proc = _FakeProc()
    l.started_here = True
    monkeypatch.setattr(l, "is_running", lambda timeout=1.0: False)
    l.stop()
    assert killed == ["terminate"]
    assert l.started_here is True


def test_stop_kills_whole_process_tree(monkeypatch):
    """A venv python.exe stub re-execs the real interpreter as a CHILD, so
    terminate() alone can orphan the live server holding the port. stop() must
    run `taskkill /T /F` on Windows."""
    l = LAUNCHER()
    calls = {}

    class _FakeProc:
        pid = 777
        returncode = None

        def poll(self):
            return None

        def terminate(self):
            pass

        def kill(self):
            pass

        def wait(self, timeout=None):
            return 0

    def _fake_run(cmd, **kw):
        calls["cmd"] = cmd
        return None

    l.proc = _FakeProc()
    l.started_here = True
    monkeypatch.setattr(l, "is_running", lambda timeout=1.0: False)
    monkeypatch.setattr(bs.subprocess, "run", _fake_run)
    monkeypatch.setattr(bs.os, "name", "nt")
    l.stop()
    assert calls["cmd"][:2] == ["taskkill", "/PID"]
    assert str(l.proc.pid) in calls["cmd"]
    assert "/T" in calls["cmd"] and "/F" in calls["cmd"]   # tree + force


def test_stop_warns_when_backend_still_answering(monkeypatch, capsys):
    l = LAUNCHER()
    l.proc = type("P", (), {"pid": 5, "returncode": None, "poll": lambda s: None,
                           "terminate": lambda s: None, "kill": lambda s: None,
                           "wait": lambda s, timeout=None: 0})()
    l.started_here = True
    monkeypatch.setattr(l, "is_running", lambda timeout=1.0: True)
    monkeypatch.setattr(bs.os, "name", "nt")
    l.stop(timeout=0.2)
    assert "still serving" in capsys.readouterr().out


# ── TEST 8: log tail ─────────────────────────────────────────────────────────

def test_log_tail_returns_last_lines(tmp_path):
    log = tmp_path / "backend.log"
    log.write_text("\n".join(f"line {i}" for i in range(200)))
    l = LAUNCHER(log_path=str(log))
    tail = l.log_tail(limit=200)
    assert "line 199" in tail
    assert "line 0\n" not in tail      # truncated, not the whole file


def test_log_tail_survives_missing_log(tmp_path):
    assert LAUNCHER(log_path=str(tmp_path / "nope.log")).log_tail() \
        == "(no log output)"


# ── TEST 9: _ensure_backend contract ─────────────────────────────────────────

def test_ensure_backend_returns_launcher_only_when_started(monkeypatch):
    import stealth_overlay as so

    monkeypatch.setattr(so.BackendLauncher, "is_running",
                        lambda self, timeout=2.0: True)
    launcher, ready, msg = so._ensure_backend("http://localhost:8000")
    assert (ready, launcher) == (True, None)
    assert "already running" in msg


def test_ensure_backend_returns_launcher_when_it_started(monkeypatch):
    import stealth_overlay as so

    monkeypatch.setattr(so.BackendLauncher, "is_running",
                        lambda self, timeout=2.0: False)
    monkeypatch.setattr(so.BackendLauncher, "port_in_use",
                        lambda self, timeout=0.5: False)
    monkeypatch.setattr(
        so.BackendLauncher, "start",
        lambda self: (setattr(self, "started_here", True)
                      or (True, "backend ready on http://localhost:8000")))
    launcher, ready, _ = so._ensure_backend("http://localhost:8000")
    assert ready is True
    assert launcher is not None
    assert launcher.started_here is True


def test_ensure_backend_disabled_does_not_start(monkeypatch):
    import stealth_overlay as so

    monkeypatch.setattr(so.BackendLauncher, "is_running",
                        lambda self, timeout=2.0: False)
    monkeypatch.setattr(
        so.BackendLauncher, "start",
        lambda self: pytest.fail("--no-backend must never launch"))
    launcher, ready, msg = so._ensure_backend(
        "http://localhost:8000", autostart=False)
    assert (launcher, ready) == (None, False)
    assert "nothing answers" in msg


# ── TEST 10: CLI flag parsing ────────────────────────────────────────────────

def test_parse_args_defaults():
    import stealth_overlay as so
    opts = so._parse_args(["stealth_overlay.py"])
    assert opts["autostart"] is True
    assert opts["api_url"] == "http://localhost:8000"
    assert opts["startup_timeout"] is None


def test_parse_args_flags():
    import stealth_overlay as so
    opts = so._parse_args([
        r"C:\kiyra\desktop\stealth_overlay.py",
        "--no-backend", "--api-url", "http://h:9000", "-t", "30", "-k",
    ])
    assert opts["autostart"] is False
    assert opts["api_url"] == "http://h:9000"
    assert opts["startup_timeout"] == 30.0
    assert opts["stop_on_exit"] is False


def test_parse_args_stop_on_exit_switch(monkeypatch):
    import stealth_overlay as so
    monkeypatch.delenv("KIYRA_STOP_BACKEND_ON_EXIT", raising=False)
    assert so._parse_args(["stealth_overlay.py", "--stop-backend-on-exit"]) \
        ["stop_on_exit"] is True
    assert so._stop_backend_on_exit() is False
    monkeypatch.setenv("KIYRA_STOP_BACKEND_ON_EXIT", "1")
    assert so._stop_backend_on_exit() is True


def test_parse_args_env_api_url(monkeypatch):
    import stealth_overlay as so
    monkeypatch.setenv("KIYRA_API_URL", "http://envhost:7000")
    assert so._parse_args(["stealth_overlay.py"])["api_url"] \
        == "http://envhost:7000"


def test_parse_args_ignores_unknown_without_crashing(capsys):
    import stealth_overlay as so
    opts = so._parse_args(["stealth_overlay.py", "--nope", "--no-backend"])
    assert opts["autostart"] is False
    assert "--nope" in capsys.readouterr().out
