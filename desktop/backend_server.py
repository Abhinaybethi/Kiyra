"""BackendLauncher — start the Kiyra FastAPI backend automatically.

The overlay must work with a single command. ``python stealth_overlay.py``
now starts the backend itself when nothing answers on the API URL, waits for
``GET /health`` to go green, and only then creates the live session and opens
the WebSocket.

Design mirrors LlamaCppServerManager (Phase 4):

* Reuses an already-running backend (never double-starts, never kills it).
* Launches uvicorn as a HIDDEN child process (no console window), logging to
  ``%TEMP%\\opencode\\kiyra-backend.log`` exactly like llama-server does.
* Interpreter resolution: ``KIYRA_BACKEND_PYTHON`` -> ``backend/.venv`` ->
  the current interpreter (``python -m uvicorn``).
* Hard startup timeout; a dead child fails fast with the log tail instead of
  hanging the overlay.
* ``stop()`` only terminates a backend THIS process started, and only when
  asked (default: leave it running so the next start is instant).

Stdlib only — no new dependencies.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

# ── Defaults (environment-overridable) ────────────────────────────────────────
TEMP = os.path.join(os.environ.get("TEMP", os.path.expandvars(r"%TEMP%")),
                    "opencode")

DEFAULT_API_URL = os.environ.get("KIYRA_API_URL", "http://localhost:8000")
DEFAULT_BACKEND_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend")
BACKEND_LOG = os.path.join(TEMP, "kiyra-backend.log")
STARTUP_TIMEOUT = float(os.environ.get("KIYRA_BACKEND_STARTUP_TIMEOUT", "120"))
PORT_IN_USE_TIMEOUT = float(os.environ.get("KIYRA_BACKEND_PORT_TIMEOUT", "8"))
LOG_TAIL_CHARS = 900


def _hidden_flags() -> int:
    """Same console-free flags verified for llama-server in Phase 4."""
    if os.name != "nt":
        return 0
    return (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))


def parse_api_url(api_url: str) -> tuple[str, int]:
    """('http://localhost:8000/') -> ('127.0.0.1', 8000)."""
    url = (api_url or DEFAULT_API_URL).strip().rstrip("/")
    for prefix in ("http://", "https://"):
        if url.lower().startswith(prefix):
            url = url[len(prefix):]
            break
    host, _, port = url.partition(":")
    return (host or "127.0.0.1"), int(port or "8000")


class BackendLauncher:
    """Ensure the backend is up; start it only if it is not already running."""

    def __init__(self,
                 api_url: str = DEFAULT_API_URL,
                 backend_dir: str = DEFAULT_BACKEND_DIR,
                 python_exe: str | None = None,
                 log_path: str = BACKEND_LOG,
                 startup_timeout: float = STARTUP_TIMEOUT,
                 app: str = "main:app",
                 poll_interval: float = 0.5):
        self.api_url = (api_url or DEFAULT_API_URL).rstrip("/")
        self.host, self.port = parse_api_url(self.api_url)
        self.backend_dir = backend_dir
        self.python_exe = python_exe
        self.log_path = log_path
        self.startup_timeout = float(startup_timeout)
        self.app = app
        self.poll_interval = float(poll_interval)
        self.proc: subprocess.Popen | None = None
        self.started_here = False
        self.command: list[str] = []

    # ── Health probe ─────────────────────────────────────────────────────

    def health_url(self) -> str:
        return f"{self.api_url}/health"

    def is_running(self, timeout: float = 2.0) -> bool:
        """True when /health answers 2xx (a backend is already serving)."""
        try:
            with urllib.request.urlopen(self.health_url(), timeout=timeout) as r:
                return 200 <= r.status < 300
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def port_in_use(self, timeout: float = 0.5) -> bool:
        """Something already owns the port but /health never answers."""
        import socket
        for family, addr in ((socket.AF_INET, (self.host, self.port)),):
            s = socket.socket(family, socket.SOCK_STREAM)
            try:
                s.settimeout(timeout)
                if s.connect_ex(addr) == 0:
                    return True
            except OSError:
                return False
            finally:
                s.close()
        return False

    # ── Interpreter resolution ───────────────────────────────────────────

    def resolve_python(self) -> str:
        """KIYRA_BACKEND_PYTHON -> backend/.venv -> current interpreter."""
        if self.python_exe:
            return self.python_exe
        env = os.environ.get("KIYRA_BACKEND_PYTHON", "").strip()
        if env:
            return env
        venv = os.path.join(
            self.backend_dir, ".venv",
            "Scripts" if os.name == "nt" else "bin",
            "python.exe" if os.name == "nt" else "python")
        if os.path.isfile(venv):
            return venv
        return sys.executable

    def build_command(self) -> list[str]:
        """uvicorn argv. No --reload: a reloader spawns a grandchild process
        that would outlive our shutdown and defeat the health/stop logic."""
        cmd = [self.resolve_python(), "-m", "uvicorn", self.app,
               "--host", self.host, "--port", str(self.port)]
        self.command = list(cmd)
        return cmd

    # ── Log tail (for actionable failure messages) ───────────────────────

    def log_tail(self, limit: int = LOG_TAIL_CHARS) -> str:
        try:
            with open(self.log_path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - limit * 4))
                raw = fh.read().decode("utf-8", errors="replace")
            lines = [ln for ln in raw.splitlines() if ln.strip()]
            return "\n".join(lines[-12:])[-limit:]
        except Exception:
            return "(no log output)"

    # ── Start / wait / stop ──────────────────────────────────────────────

    def start(self) -> tuple[bool, str]:
        """Launch the backend and block until /health is green.

        Returns (ready, message). Never raises.
        """
        if not os.path.isdir(self.backend_dir):
            return False, (f"backend directory not found: {self.backend_dir} "
                           f"(set KIYRA_BACKEND_DIR or start it manually)")

        cmd = self.build_command()
        try:
            os.makedirs(os.path.dirname(self.log_path) or ".", exist_ok=True)
            with open(self.log_path, "ab") as logf:
                logf.write(b"\n===== backend start =====\n")
                logf.flush()
                self.proc = subprocess.Popen(
                    cmd, cwd=self.backend_dir, stdout=logf,
                    stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                    creationflags=_hidden_flags())
        except FileNotFoundError:
            self.proc = None
            return False, f"python interpreter not found: {cmd[0]}"
        except Exception as e:
            self.proc = None
            return False, f"failed to launch backend: {e}"

        self.started_here = True
        print(f"[Backend] launching: {' '.join(cmd)} (cwd={self.backend_dir})")

        deadline = time.monotonic() + self.startup_timeout
        busy_since = None
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                return False, (f"backend exited immediately (code "
                               f"{self.proc.returncode}). Log tail:\n"
                               f"{self.log_tail()}")
            if self.is_running(timeout=1.0):
                return True, (f"backend ready on {self.api_url} "
                              f"(pid {self.proc.pid})")
            # Port busy but no /health: a wrong/foreign service is squatting.
            if self.port_in_use():
                busy_since = busy_since or time.monotonic()
                if time.monotonic() - busy_since > PORT_IN_USE_TIMEOUT:
                    return False, (
                        f"port {self.port} is occupied by something that is "
                        f"not the Kiyra backend — free it or set KIYRA_API_URL")
            time.sleep(self.poll_interval)

        return False, (f"backend did not become healthy within "
                       f"{self.startup_timeout:.0f}s. Log tail:\n"
                       f"{self.log_tail()}")

    def ensure(self) -> tuple[bool, str]:
        """Reuse a running backend, otherwise start one. Never raises."""
        if self.is_running():
            return True, f"backend already running on {self.api_url}"
        if self.port_in_use():
            return False, (
                f"port {self.port} is busy but /health does not answer — "
                f"stop the other process or set KIYRA_API_URL")
        return self.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Terminate ONLY a backend this process started. Never raises.

        Kills the whole PROCESS TREE: on Windows a venv ``python.exe`` stub
        re-execs the real interpreter as a *child* (verified: stub pid -> child
        pid), so terminating the direct child alone can orphan the live server
        and leave the port held.
        """
        proc = self.proc
        if proc is None or not self.started_here:
            return
        if proc.poll() is not None:
            return

        if os.name == "nt":
            try:
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                               capture_output=True, timeout=timeout + 5,
                               creationflags=_hidden_flags())
            except Exception:
                pass

        try:
            proc.terminate()
        except Exception:
            pass
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.wait(timeout=timeout)
            except Exception:
                pass
        except Exception:
            pass

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and self.is_running(timeout=1.0):
            time.sleep(0.25)

        if self.is_running(timeout=1.0):
            print(f"[Backend] warning: something is still serving "
                  f"{self.api_url} after stop()")
        else:
            print(f"[Backend] stopped (pid {proc.pid})")


if __name__ == "__main__":  # manual control: python backend_server.py [stop]
    _l = BackendLauncher()
    if len(sys.argv) > 1 and sys.argv[1] == "stop":
        _l.stop()
    else:
        _ok, _msg = _l.ensure()
        print(f"[Backend] {_msg}")
