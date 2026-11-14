"""Tests for the GPU power-state probe (backend/ai/gpu_status.py)."""
from __future__ import annotations

import sys
import types

import pytest

sys.path.insert(0, __file__.rsplit("\\", 1)[0].rsplit("/", 1)[0])

from ai import gpu_status  # noqa: E402


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
