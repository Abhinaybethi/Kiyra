"""Phase-4 tests: GPU detection, CUDA llama-server start/health, CPU fallback.

TEST 1  detect_gpu            — nvidia-smi parse (skipped when no NVIDIA)
TEST 2  GPU server starts      — real CUDA launch, probe + promote
TEST 3  GPU /health works      — /v1/models returns the Bonsai model
TEST 4  CPU fallback           — GPU unavailable -> known CPU command

Integration tests (2/3) are skipped when the CUDA build + cudart are not
present on disk so the suite stays meaningful on any machine.
"""
from __future__ import annotations

import sys
import time
import urllib.request
from pathlib import Path

import pytest

DESKTOP_DIR = Path(__file__).resolve().parents[2] / "desktop"
sys.path.insert(0, str(DESKTOP_DIR))

import llama_server as ls  # noqa: E402

CPU_BASELINE = [
    ls.CPU_BIN, "-m", ls.DEFAULT_MODEL, "--port", "8080", "--host", "127.0.0.1",
]

MGR = ls.LlamaCppServerManager


def _skip_no_nvidia():
    if MGR.detect_gpu() is None:
        pytest.skip("nvidia-smi / NVIDIA GPU unavailable on this machine")


def _skip_no_cuda():
    if not MGR.gpu_binary_ready():
        pytest.skip("CUDA llama-server build + cudart not installed on disk")


# ─────────────────────────────────────────────────────────────────────────────
# TEST 1 — GPU detection (Part 2)
# ─────────────────────────────────────────────────────────────────────────────
def test_detect_gpu_returns_nvidia_fields():
    gpu = MGR.detect_gpu()
    assert gpu is not None, "nvidia-smi must be reachable (skip elsewhere)"
    assert gpu["name"]
    assert int(gpu["vram_mib"]) > 0
    assert gpu["driver"]


def test_detect_gpu_does_not_assume_when_nvidia_smi_missing(monkeypatch):
    monkeypatch.setattr(ls.subprocess, "run",
                        lambda *a, **k: _FailingNVIDIA())
    assert MGR.detect_gpu() is None


class _FailingNVIDIA:
    returncode = 1
    stdout = ""


# ─────────────────────────────────────────────────────────────────────────────
# command construction (Part 1 preserves the exact Phase-3 command)
# ─────────────────────────────────────────────────────────────────────────────
def _mk_bins(tmp_path, cpu: bool, gpu: bool):
    """Controlled llama-server.exe stubs + a manager pointing at them.

    The binaries are made explicit so these tests are deterministic regardless
    of which builds happen to exist on the machine (the dedicated CPU build
    lives in a temp dir that may be cleaned).
    """
    cpu_p = tmp_path / "cpu" / "llama-server.exe"
    gpu_p = tmp_path / "gpu" / "llama-server.exe"
    cpu_p.parent.mkdir(parents=True, exist_ok=True)
    gpu_p.parent.mkdir(parents=True, exist_ok=True)
    if cpu:
        cpu_p.write_bytes(b"x")
    if gpu:
        gpu_p.write_bytes(b"x")
    m = MGR(cpu_bin=str(cpu_p), gpu_bin=str(gpu_p),
            log_path=str(tmp_path / "llama.log"))
    return m, cpu_p, gpu_p


def _cpu_cmd(bin_path):
    return [str(bin_path), "-m", ls.DEFAULT_MODEL, "--port", "8080",
            "--host", "127.0.0.1"]


def test_cpu_command_preserves_phase3_baseline(tmp_path):
    m, cpu_p, _ = _mk_bins(tmp_path, cpu=True, gpu=True)
    assert m.build_command("cpu") == _cpu_cmd(cpu_p)


def test_cpu_command_reuses_cuda_bin_with_zero_offload_when_cpu_missing(tmp_path):
    # Dedicated CPU build gone -> CPU-only inference runs on the CUDA build with
    # explicit zero GPU offload (never a missing-binary WinError 2).
    m, _, gpu_p = _mk_bins(tmp_path, cpu=False, gpu=True)
    assert m.build_command("cpu") == _cpu_cmd(gpu_p) + ["-n-gpu-layers", "0"]


def test_gpu_command_only_adds_offload_flag():
    m = MGR()
    cmd = m.build_command("gpu", gpu_layers=99)
    # identical Phase-3 tail; only the binary path (CUDA build) differs
    assert cmd[1:6] == CPU_BASELINE[1:6]
    assert Path(cmd[0]).name == "llama-server.exe"
    assert cmd[6] == "127.0.0.1"
    assert cmd[7:] == ["--n-gpu-layers", "99", "-lv", "5"]


# ─────────────────────────────────────────────────────────────────────────────
# TEST 4 — CPU fallback (Part 5), unit: decision logic without real processes
# ─────────────────────────────────────────────────────────────────────────────
def test_cpu_fallback_when_gpu_unavailable(monkeypatch, tmp_path):
    m, cpu_p, _ = _mk_bins(tmp_path, cpu=True, gpu=True)
    launcher = {}
    captured_cmd = []

    class _DummyProc:
        pid = 12345

        def poll(self):
            return None

    monkeypatch.setattr(ls.LlamaCppServerManager, "detect_gpu",
                        staticmethod(lambda: None))
    monkeypatch.setattr(ls.LlamaCppServerManager, "gpu_binary_ready",
                        staticmethod(lambda: True))

    def fake_poll(port=None, timeout_s=90.0):
        return True

    def fake_launch(cmd, log_path=None):
        captured_cmd.append(list(cmd))
        m.proc = _DummyProc()
        launcher["ran"] = True
        return m.proc

    monkeypatch.setattr(m, "_launch", fake_launch)
    monkeypatch.setattr(m, "_poll_health", fake_poll)

    report = m.start(prefer_gpu=True)
    assert launcher.get("ran"), "fallback did not launch a server"
    assert m.mode == "cpu"
    assert report["mode"] == "cpu"
    assert captured_cmd and captured_cmd[0] == _cpu_cmd(cpu_p)
    assert "reason" in m.evidence  # GPU failure is never hidden


def test_prefer_gpu_false_goes_straight_to_cpu(monkeypatch, tmp_path):
    m, cpu_p, _ = _mk_bins(tmp_path, cpu=True, gpu=True)
    captured_cmd = []

    class _DummyProc:
        pid = 12345

        def poll(self):
            return None

    monkeypatch.setattr(m, "_launch",
                        lambda cmd, log_path=None: (captured_cmd.append(list(cmd)), _DummyProc())[1])
    monkeypatch.setattr(m, "_poll_health", lambda port=None, timeout_s=90.0: True)
    r = m.start(prefer_gpu=False)
    assert r["mode"] == "cpu"
    assert captured_cmd[0] == _cpu_cmd(cpu_p)


# ─────────────────────────────────────────────────────────────────────────────
# TEST 2 / TEST 3 — real CUDA start + health (integration, skip-gated)
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.integration
def test_gpu_server_starts_and_health_works(tmp_path):
    _skip_no_nvidia()
    _skip_no_cuda()

    port = 19081
    m = MGR(port=port, log_path=str(tmp_path / "llama-gpu.log"),
            gpu_layers=99)
    try:
        result = m.start(prefer_gpu=True)
        # TEST 3 — health endpoint: OpenAI-compatible API on the real port.
        url = f"http://127.0.0.1:{port}/v1/models"
        with urllib.request.urlopen(url, timeout=5) as resp:
            body = resp.read().decode("utf-8", "replace")
            assert resp.status == 200
            assert '"model"' in body or '"id"' in body
    finally:
        if m.proc is not None:
            m._stop(m.proc)

    assert result is not None
    ev = result["evidence"]
    assert result["mode"] == "gpu", \
        f"expected GPU mode (fallback reason: {ev.get('reason')})"
    # GPU offload must be PROVEN from the llama.cpp log, not assumed.
    assert ev.get("offloaded_count", 0) > 0, (
        "no layers reported offloaded to GPU: " + repr(ev))
    assert any(ev.get("device", "")), \
        "no CUDA/NVIDIA device line in startup log"
    assert "requested_layers" in ev


def test_no_gpu_binary_ready_without_cudart(monkeypatch, tmp_path):
    # A CUDA build without cudart64_*.dll must NOT be treated as ready.
    monkeypatch.setattr(ls, "GPU_BIN", str(tmp_path / "llama-server.exe"))
    (tmp_path / "llama-server.exe").write_bytes(b"x")
    assert ls.LlamaCppServerManager.gpu_binary_ready() is False