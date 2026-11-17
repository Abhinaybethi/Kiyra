"""Tests for the GPU power-state probe (backend/ai/gpu_status.py)."""
from __future__ import annotations

import sys

import pytest

sys.path.insert(0, __file__.rsplit("\\", 1)[0].rsplit("/", 1)[0])

from ai import gpu_status  # noqa: E402


@pytest.fixture(autouse=True)
def _no_nvidia_probe(monkeypatch):
    """Neutralise the real nvidia-smi probe so the tests are deterministic."""
    monkeypatch.setattr(gpu_status, "_nvidia_gpu_state", lambda: {})


def test_probe_never_raises_and_reports_supported_flag(monkeypatch):
    """The probe must degrade gracefully — it runs on every /health call."""
    monkeypatch.setattr(gpu_status, "_ac_line_status", lambda: None)
    monkeypatch.setattr(gpu_status, "_power_scheme", lambda: "")
    state = gpu_status.gpu_power_state()
    assert "on_battery" in state
    assert "note" in state
    assert isinstance(state["note"], str)


def test_battery_state_produces_an_explanation(monkeypatch):
    """On battery the probe MUST explain the slowdown — that is its whole job."""
    monkeypatch.setattr(gpu_status, "_ac_line_status", lambda: 0)
    monkeypatch.setattr(gpu_status, "_power_scheme", lambda: "Balanced")
    state = gpu_status.gpu_power_state()
    assert state["on_battery"] is True
    assert "charger" in state["note"].lower()


def test_ac_state_has_no_warning(monkeypatch):
    monkeypatch.setattr(gpu_status, "_ac_line_status", lambda: 1)
    monkeypatch.setattr(gpu_status, "_power_scheme", lambda: "Best performance")
    state = gpu_status.gpu_power_state()
    assert state["on_battery"] is False
    assert state["note"] == ""


# ── hybrid graphics: the dGPU is capped even while on AC ───────────────────

def test_capped_dgpu_is_reported_even_on_ac(monkeypatch):
    """The real trap: on AC, battery advice is useless.

    This laptop (Acer Aspire A715-79G) holds the RTX 3050 at 30 W of its
    60 W default because the Intel iGPU drives the display. The probe must
    say so, and must NOT tell the user to plug in the charger.
    """
    monkeypatch.setattr(gpu_status, "_ac_line_status", lambda: 1)
    monkeypatch.setattr(gpu_status, "_power_scheme", lambda: "Balanced")
    monkeypatch.setattr(gpu_status, "_nvidia_gpu_state", lambda: {
        "power_limit_w": 30,
        "default_power_limit_w": 60,
        "max_power_limit_w": 75,
        "clock_mhz": 1200,
        "max_clock_mhz": 2100,
        "display_active": False,
    })
    state = gpu_status.gpu_power_state()
    assert state["on_battery"] is False
    assert "30" in state["note"]
    assert "60" in state["note"]
    assert "charger" not in state["note"].lower(), (
        "the machine is already on AC — 'plug in the charger' is wrong advice")
    assert "prefer maximum performance" in state["note"].lower()
    # The raw numbers travel with the state so the UI can show them.
    assert state["power_limit_w"] == 30
    assert state["display_active"] is False


def test_uncapped_dgpu_on_ac_has_no_note(monkeypatch):
    """A GPU running at its default TGP is fine — no warning."""
    monkeypatch.setattr(gpu_status, "_ac_line_status", lambda: 1)
    monkeypatch.setattr(gpu_status, "_power_scheme", lambda: "Balanced")
    monkeypatch.setattr(gpu_status, "_nvidia_gpu_state", lambda: {
        "power_limit_w": 60,
        "default_power_limit_w": 60,
        "max_power_limit_w": 75,
    })
    state = gpu_status.gpu_power_state()
    assert state["note"] == ""


def test_battery_note_wins_over_the_tgp_note(monkeypatch):
    """On battery the battery explanation is the actionable one."""
    monkeypatch.setattr(gpu_status, "_ac_line_status", lambda: 0)
    monkeypatch.setattr(gpu_status, "_power_scheme", lambda: "Balanced")
    monkeypatch.setattr(gpu_status, "_nvidia_gpu_state", lambda: {
        "power_limit_w": 30,
        "default_power_limit_w": 60,
    })
    state = gpu_status.gpu_power_state()
    assert "charger" in state["note"].lower()


def test_nvidia_probe_returns_numbers_on_this_machine():
    """The probe must not crash when nvidia-smi is present (or absent)."""
    gpu = gpu_status._nvidia_gpu_state()
    assert isinstance(gpu, dict)
    for key in ("power_limit_w", "default_power_limit_w", "clock_mhz"):
        assert key in gpu or not gpu, "either no GPU at all, or real numbers"
