"""LlamaCppServerManager — GPU-first llama.cpp llama-server launching.

Phase 4: enable CUDA offload on the RTX 3050 while preserving the
OpenAI-compatible /v1 API, port 8080, /health and the Bonsai model.

Flow (Part 5 of the Phase-4 spec):

    GPU available AND CUDA build present?
        try CUDA llama-server (probe on a scratch port)
        healthy AND the log proves layers offloaded to GPU?
            promote the GPU config to the real port
        else record the failure reason and fall back to the CPU config

GPU initialization failures are never hidden: the probe reads the actual
startup log and reports the reason (mode=CPU carries the evidence).

No external dependencies are introduced — stdlib only.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

# ─────────────────────────────────────────────────────────────────────────────
# Defaults (all overridable via environment for a different machine)
# ─────────────────────────────────────────────────────────────────────────────
TEMP = os.path.join(os.environ.get("TEMP", os.path.expandvars(r"%TEMP%")),
                    "opencode")

DEFAULT_MODEL = os.environ.get(
    "KIYRA_LLAMA_MODEL", r"C:\AI\models\Bonsai-8B-Q1_0.gguf")
DEFAULT_PORT = int(os.environ.get("KIYRA_LLAMA_PORT", "8080"))
DEFAULT_HOST = os.environ.get("KIYRA_LLAMA_HOST", "127.0.0.1")

# CPU-only build (proven in Phase 3) and the CUDA-12.4 build both live under
# Temp\opencode. The CPU binary is our known-good fallback; the CUDA binary
# is the b11227 win-cuda-12.4 release with cudart dropped beside it.
_DEFAULT_CPU_DIR = os.environ.get(
    "KIYRA_LLAMA_CPU_DIR",
    os.path.join(TEMP, "llama-bin-win-x64"))
_DEFAULT_GPU_DIR = os.environ.get(
    "KIYRA_LLAMA_GPU_DIR",
    os.path.join(TEMP, "llama-bin-win-cuda-x64"))

CPU_BIN = os.environ.get(
    "KIYRA_LLAMA_CPU_BIN", os.path.join(_DEFAULT_CPU_DIR, "llama-server.exe"))
GPU_BIN = os.environ.get(
    "KIYRA_LLAMA_GPU_BIN", os.path.join(_DEFAULT_GPU_DIR, "llama-server.exe"))

LLAMA_LOG = os.environ.get(
    "KIYRA_LLAMA_LOG", os.path.join(TEMP, "llama-server.log"))

# Console-free launch flags (Phase-3 fix: DETACHED_PROCESS *creates* a
# hidden console whose close event kills the process via libiomp5md.dll's
# forrtl handler — these flags were verified to yield GetConsoleWindow()==0).
_FLAGS = (subprocess.CREATE_NEW_PROCESS_GROUP
          | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
          | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))

_HEALTH_RE = re.compile(r"\"model\"|\"id\"")
_OFFLOAD_RE = re.compile(
    r"offloaded\s+(\d+)\s*/\s*(\d+)\s+layers\s+to\s+GPU", re.IGNORECASE)
_ASSIGN_RE = re.compile(
    r"layer\s+(\d+)\s+assigned\s+to\s+device\s+(CUDA\d+)", re.IGNORECASE)
_CUDA_DEV_RE = re.compile(
    r"using device\s+(CUDA\d+)[^0-9]*\(([^)]*(?:NVIDIA|AMD)[^)]*)\)|"
    r"CUDA\d+\s*:\s*([^\n]+)",
    re.IGNORECASE)
_CUDA_ENV_RE = re.compile(r"system_info:.*CUDA\s*:\s*ARCHS", re.IGNORECASE)
_KV_CPU_RE = re.compile(
    r"llama_kv_cache:\s*[^\n]*CPU KV buffer size\s*=\s*([\d.]+)\s*MiB")
_OOM_RE = re.compile(
    r"failed to allocate|out of memory|CUDA error|no space left",
    re.IGNORECASE)


class LlamaCppServerManager:
    """Launch and health-check the llama-server with GPU-first fallback."""

    def __init__(self, model: str = DEFAULT_MODEL, port: int = DEFAULT_PORT,
                 host: str = DEFAULT_HOST, cpu_bin: str = CPU_BIN,
                 gpu_bin: str = GPU_BIN, log_path: str = LLAMA_LOG,
                 gpu_layers: int = 99):
        self.model = model
        self.port = port
        self.host = host
        self.cpu_bin = cpu_bin
        self.gpu_bin = gpu_bin
        self.log_path = log_path
        self.gpu_layers = gpu_layers
        self.proc = None
        self.mode = "cpu"          # 'gpu' | 'cpu'
        self.command: list[str] = []
        self.evidence: dict = {}   # human-visible GPU proof / fallback reason
        self.startup_health_s = None
        self._probes: list = []    # scratch-port probes, reaped on promote

    # ────────────────────────────────────────────────────────────────────────
    # Detection (Part 2 / Part 3)
    # ────────────────────────────────────────────────────────────────────────
    @staticmethod
    def detect_gpu() -> dict | None:
        """Query nvidia-smi. Returns None when NVIDIA is not usable so callers
        never *assume* a GPU merely because one is usually installed."""
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.total,driver_version",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=20)
        except Exception:
            return None
        if out.returncode != 0 or not out.stdout.strip():
            return None
        try:
            name, vram_mib, driver = [c.strip() for c in out.stdout.split(",")[:3]]
            return {"name": name, "vram_mib": int(float(vram_mib)),
                    "driver": driver}
        except Exception:
            return None

    @staticmethod
    def gpu_binary_ready() -> bool:
        """A CUDA build needs llama-server.exe + a cudart64_1xx.dll beside it.
        Returning False means "do not even attempt GPU mode"."""
        if not (os.path.isfile(GPU_BIN) and os.path.isfile(CPU_BIN)):
            return False
        dlls = os.listdir(os.path.dirname(GPU_BIN))
        return any(re.fullmatch(r"cudart64_\d+\.dll", d) for d in dlls)

    def build_command(self, kind: str = "cpu", gpu_layers: int | None = None) -> list[str]:
        """Exact argv for llama-server. CPU preserves the proven Phase-3
        command byte-for-byte; GPU only adds the offload flag + -lv 5
        (b11227 prints offload proof lines only at verbosity >= 5)."""
        if kind == "gpu":
            cmd = self._gpu_command(self.port, gpu_layers or self.gpu_layers)
            self.command = list(cmd)
            return cmd
        cmd = [self.cpu_bin, "-m", self.model, "--port", str(self.port),
               "--host", self.host]
        self.command = list(cmd)
        return cmd

    def _gpu_command(self, port: int, layers: int) -> list[str]:
        return [self.gpu_bin, "-m", self.model, "--port", str(port),
                "--host", self.host,
                "--n-gpu-layers", str(layers), "-lv", "5"]

    # ────────────────────────────────────────────────────────────────────────
    # Launch / health
    # ────────────────────────────────────────────────────────────────────────
    def _launch(self, cmd: list[str], log_path: str | None = None) -> subprocess.Popen:
        path = log_path or self.log_path
        with open(path, "ab") as logf:
            logf.write(b"\n===== llama-server start =====\n")
            logf.flush()
            proc = subprocess.Popen(
                cmd, stdout=logf, stderr=subprocess.STDOUT,
                creationflags=_FLAGS)
        self.proc = proc
        return proc

    def _poll_health(self, port: int | None = None, timeout_s: float = 90.0) -> bool:
        url = f"http://{self.host}:{port or self.port}/v1/models"
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.proc and self.proc.poll() is not None:
                return False
            try:
                with urllib.request.urlopen(url, timeout=3) as resp:
                    body = resp.read().decode("utf-8", "replace")
                    return resp.status == 200 and bool(_HEALTH_RE.search(body))
            except Exception:
                time.sleep(1)
        return False

    @staticmethod
    def _read_log(path: str) -> str:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                return f.read()
        except Exception:
            return ""

    def _offload_evidence(self, log_path: str | None = None,
                          requested_layers: int | None = None) -> dict:
        """Parse the startup log for GPU proof: layers offloaded + device.
        Returns {} when the log proves NO GPU acceleration was used."""
        text = self._read_log(log_path or self.log_path)
        ev = {}
        if requested_layers:
            ev["requested_layers"] = requested_layers
        m = _OFFLOAD_RE.search(text)
        if m:
            ev["offloaded"] = f"{m.group(1)}/{m.group(2)} layers"
            ev["offloaded_count"] = int(m.group(1))
        if not ev.get("offloaded_count"):
            # b11227 at -lv 5 prints one line per layer:
            #   load_tensors: layer 0 assigned to device CUDA0, is_swa = 0
            assigns = _ASSIGN_RE.findall(text)
            if assigns:
                ev["offloaded_count"] = len(assigns)
                ev["assignments"] = len(assigns)
        d = _CUDA_DEV_RE.search(text)
        if d:
            ev["device"] = (d.group(1) or d.group(3) or
                            d.group(2) or "CUDA device").strip()
        if _CUDA_ENV_RE.search(text):
            ev["cuda_env"] = True
        kv = _KV_CPU_RE.search(text)
        if kv:
            ev["kv_mib_cpu"] = round(float(kv.group(1)), 1)
        if _OOM_RE.search(text):
            ev["oom"] = True
        return ev

    # ────────────────────────────────────────────────────────────────────────
    # Probe then promote / fall back
    # ────────────────────────────────────────────────────────────────────────
    def start(self, prefer_gpu: bool = True) -> dict:
        """Start llama-server, GPU-first with CPU fallback.

        Returns a report dict: {proc, mode, command, evidence, health_s}.
        Raises RuntimeError only when even the CPU fallback could not start
        (a real outage — not something we silently swallow)."""
        gpu = detect_gpu = self.detect_gpu()

        if prefer_gpu and gpu and self.gpu_binary_ready():
            result = self._try_gpu(gpu)
            if result is not None:
                self.evidence = result["evidence"]
                report = self._report()
                return report

        # CPU fallback (Part 5): log the GPU failure reason, never crash.
        self.mode = "cpu"
        reason = None
        if not gpu:
            reason = "no NVIDIA GPU detected (nvidia-smi unavailable)"
        elif not self.gpu_binary_ready():
            reason = ("CUDA llama-server binary/cudart missing under "
                      + os.path.dirname(self.gpu_bin))
        else:
            reason = (self.evidence.get("reason")
                      or "GPU probe failed/could not prove offload")
        print(f"[llama] mode=CPU (fallback reason: {reason})")
        self.evidence = {"reason": reason,
                         "cmd": self.build_command("cpu")}
        cmd = self.build_command("cpu")
        self._launch(cmd)
        ok = self._poll_health(timeout_s=120.0)
        self.startup_health_s = time.monotonic()  # placeholder, refined below
        if not ok:
            tail = self._read_log(self.log_path).strip().splitlines()[-15:]
            raise RuntimeError("CPU llama-server failed to become healthy: "
                               + "; ".join(tail))
        return self._report()

    def _try_gpu(self, gpu: dict) -> dict | None:
        """Probe CUDA offload on a SCRATCH port; promote only with proof."""
        scratch = self._scratch_port()
        attempts = [self.gpu_layers, 32]
        for i, layers in enumerate(attempts):
            cmd = self._gpu_command(scratch, layers)
            probe_log = self.log_path + f".probe{i}"
            probe = self._launch(cmd, log_path=probe_log)
            self._probes.append(probe)
            ok = self._poll_health(port=scratch, timeout_s=120.0)
            evidence = self._offload_evidence(probe_log,
                                              requested_layers=layers)
            # Always reap the probe BEFORE promoting. The probe holds a full
            # copy of the model in VRAM, and promoting while it lives both
            # starves the GPU (verified: repeated starts left 12 probe servers
            # holding 5.5/6 GB and Bonsai could no longer load) and loses the
            # handle entirely, because the real launch reassigns self.proc.
            self._stop(probe)
            if ok and evidence.get("offloaded_count", 0) > 0:
                # Promote: same args on the real port, using the PROVEN layer
                # count so the actual server offloads exactly what we measured.
                layers_ok = layers
                self.mode = "gpu"
                self.command = self._gpu_command(self.port, layers_ok)
                self._launch(self.command)
                ok2 = self._poll_health(timeout_s=120.0)
                if ok2:
                    final_ev = self._offload_evidence(self.log_path)
                    final_ev["gpu"] = gpu["name"]
                    final_ev["vram_mib"] = gpu["vram_mib"]
                    final_ev["driver"] = gpu["driver"]
                    final_ev["requested_layers"] = layers_ok
                    final_ev["cmd"] = list(self.command)
                    final_ev["probe_log"] = probe_log
                    self.evidence = final_ev
                    print(f"[llama] mode=GPU offload {final_ev.get('offloaded')} "
                          f"(requested {layers_ok})")
                    return {"evidence": final_ev}
            # Record why this attempt did not yield GPU mode.
            whole_gpu = evidence or {"offloaded_count": 0}
            try:
                tail = self._read_log(probe_log).strip().splitlines()[-8:]
            except Exception:
                tail = []
            print(f"[llama] GPU probe #{i} healthy={ok} offloaded={whole_gpu.get('offloaded')} "
                  f"(reason: {'; '.join(tail[-3:])})")
            if self.proc and self.proc.poll() is None:
                self._stop(self.proc)
        reason = "GPU probes started but produced no offload evidence"
        self.evidence = {"reason": reason}
        return None

    @staticmethod
    def _stop(proc: subprocess.Popen) -> None:
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def _scratch_port(self) -> int:
        """Pick a free high port for the probe (never collides with 8080)."""
        base = 18080 + (time.monotonic_ns() % 2000)
        import socket
        for port in range(int(base), int(base) + 40):
            try:
                with socket.socket() as s:
                    s.bind((self.host, port))
                return port
            except OSError:
                continue
        return 19081

    def stop(self, timeout: float = 10.0) -> None:
        """Stop every server this manager started (probes + the real one).

        Never raises. Waits for the port to actually stop listening, because
        llama-server releases its CUDA buffers asynchronously after exit and a
        restart that races that release fails to load the model.
        """
        for proc in list(self._probes) + ([self.proc] if self.proc else []):
            if proc is None:
                continue
            try:
                if proc.poll() is None:
                    self._stop(proc)
            except Exception:
                pass
        self._probes.clear()

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self._port_open():
                break
            time.sleep(0.25)

    def _port_open(self, port: int | None = None) -> bool:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.5)
        try:
            return s.connect_ex((self.host, port or self.port)) == 0
        except OSError:
            return False
        finally:
            s.close()

    def _report(self) -> dict:
        start_s = getattr(self, "startup_health_s", None)
        return {
            "proc": self.proc,
            "pid": self.proc.pid if self.proc else None,
            "mode": self.mode,
            "command": self.command,
            "evidence": self.evidence,
            "health_s": start_s,
        }


class ModelServerLauncher:
    """Ensure Bonsai's llama-server is up; start it only if it is not running.

    The reuse contract mirrors ``backend_server.BackendLauncher`` exactly:

    * Reuses an already-serving model server (never double-starts it).
    * Never kills a server it did not start.
    * Never raises — ``ensure()`` returns ``(ready, message)`` so a missing
      model degrades with an actionable message instead of a traceback.

    ``ensure_provider_ready()`` in the backend turns a missing model server
    into an opaque 503 on session start, which is why the overlay now brings
    the model up itself.
    """

    def __init__(self,
                 port: int = DEFAULT_PORT,
                 host: str = DEFAULT_HOST,
                 model: str = DEFAULT_MODEL,
                 log_path: str = LLAMA_LOG,
                 startup_timeout: float = 180.0,
                 manager_factory=None):
        self.port = int(port)
        self.host = host
        self.model = model
        self.log_path = log_path
        self.startup_timeout = float(startup_timeout)
        self._factory = manager_factory or LlamaCppServerManager
        self.manager = None
        self.proc: subprocess.Popen | None = None
        self.started_here = False

    # ── Health probe ─────────────────────────────────────────────────────

    def models_url(self) -> str:
        return f"http://{self.host}:{self.port}/v1/models"

    def is_running(self, timeout: float = 3.0) -> bool:
        """True when /v1/models answers 200 with a model descriptor."""
        try:
            with urllib.request.urlopen(self.models_url(), timeout=timeout) as r:
                if not (200 <= r.status < 300):
                    return False
                body = r.read().decode("utf-8", "replace")
                return bool(_HEALTH_RE.search(body))
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def port_in_use(self, timeout: float = 0.5) -> bool:
        """Something owns the port but /v1/models never answers."""
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            return s.connect_ex((self.host, self.port)) == 0
        except OSError:
            return False
        finally:
            s.close()

    def log_tail(self, limit: int = 900) -> str:
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

    # ── Start / stop ─────────────────────────────────────────────────────

    def ensure(self, prefer_gpu: bool = True) -> tuple[bool, str]:
        """Reuse a serving model server, otherwise start one. Never raises."""
        if self.is_running():
            return True, (f"model server already running on "
                          f"http://{self.host}:{self.port}")
        if self.port_in_use():
            return False, (f"port {self.port} is busy but /v1/models does not "
                           f"answer — stop that process or set KIYRA_LLAMA_PORT")

        self.manager = self._factory(model=self.model, port=self.port,
                                     host=self.host, log_path=self.log_path)
        try:
            report = self.manager.start(prefer_gpu=prefer_gpu)
        except RuntimeError as e:
            return False, (f"model server failed to start: {e}\n"
                           f"          Log: {self.log_path}")
        except Exception as e:
            return False, f"model server failed to start: {e}"

        self.proc = report.get("proc")
        self.started_here = True
        mode = (report.get("mode") or "?").upper()
        pid = report.get("pid")
        return True, f"model server ready ({mode}, pid {pid}) on port {self.port}"

    def stop(self, timeout: float = 10.0) -> None:
        """Terminate ONLY a model server this process started. Never raises."""
        if not self.started_here:
            return
        proc = self.proc or (self.manager.proc if self.manager else None)
        if proc is None:
            return
        if os.name == "nt":
            try:
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                               capture_output=True, timeout=timeout + 5,
                               creationflags=_FLAGS)
            except Exception:
                pass
        if self.manager is not None:
            try:
                self.manager.stop(timeout=timeout)
            except Exception:
                pass
        else:
            LlamaCppServerManager._stop(proc)

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and self.is_running(timeout=1.0):
            time.sleep(0.25)
        if self.is_running(timeout=1.0):
            print(f"[Model] warning: something is still serving "
                  f"{self.models_url()} after stop()")
        else:
            print(f"[Model] stopped (pid {proc.pid})")


def main(argv: list[str] | None = None) -> int:
    args = argv or sys.argv[1:]
    prefer_gpu = "--cpu" not in args
    model = DEFAULT_MODEL
    port = DEFAULT_PORT
    if "--model" in args:
        model = args[args.index("--model") + 1]
    if "--port" in args:
        port = int(args[args.index("--port") + 1])
    mgr = LlamaCppServerManager(model=model, port=port)
    try:
        report = mgr.start(prefer_gpu=prefer_gpu)
    except RuntimeError as e:
        print(f"[llama] FATAL: {e}")
        return 1
    print(f"[llama] {report['mode'].upper()} server PID {report['pid']}")
    print(f"[llama] command: {' '.join(report['command'])}")
    if report["evidence"]:
        print(f"[llama] evidence: {json.dumps(report['evidence'], indent=2)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())