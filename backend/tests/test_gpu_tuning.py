"""Tests for the GPU tuning helper (backend/ai/gpu_tuning.py).

The helper is the app's half of the biggest latency win on this machine:
assigning the local model to the high-performance GPU raises the dGPU's
power limit from 30 W to 60 W, which is worth ~20-30% of the answer
latency on a hybrid-graphics laptop.
"""
from __future__ import annotations

import sys

import pytest

sys.path.insert(0, __file__.rsplit("\\", 1)[0].rsplit("/", 1)[0])

from ai import gpu_tuning  # noqa: E402


def test_ensure_never_raises_and_reports_supported(monkeypatch):
    monkeypatch.setattr(gpu_tuning, "_read_preferences", lambda: {})
    monkeypatch.setattr(gpu_tuning, "_write_preference", lambda *a: True)
    monkeypatch.setattr(gpu_tuning, "_ollama_executable", lambda: "")
    result = gpu_tuning.ensure_high_performance_gpu()
    assert "supported" in result
    assert "applied" in result
    assert "plan_is_fast" in result


def test_ensure_writes_the_preference_for_ollama(monkeypatch):
    written: list = []

    monkeypatch.setattr(gpu_tuning, "_read_preferences", lambda: {})
    monkeypatch.setattr(
        gpu_tuning, "_write_preference",
        lambda exe, value: written.append((exe, value)) or True)
    monkeypatch.setattr(gpu_tuning, "_ollama_executable",
                        lambda: "C:/Program Files/Ollama/ollama.exe")
    monkeypatch.setattr(gpu_tuning, "_current_python_executable",
                        lambda: "C:/python/python.exe")

    result = gpu_tuning.ensure_high_performance_gpu()

    assert result["applied"], "ollama.exe must be assigned to the fast GPU"
    assert result["failed"] == []
    exes = [e for e, _v in written]
    assert any("ollama.exe" in e for e in exes)
    assert any("python.exe" in e for e in exes)
    for _exe, value in written:
        assert value == "GpuPreference=2;"


def test_ensure_does_not_rewrite_an_existing_preference(monkeypatch):
    written: list = []
    monkeypatch.setattr(
        gpu_tuning, "_read_preferences",
        lambda: {"C:/Program Files/Ollama/ollama.exe": "GpuPreference=2;"})
    monkeypatch.setattr(
        gpu_tuning, "_write_preference",
        lambda exe, value: written.append(exe) or True)
    monkeypatch.setattr(gpu_tuning, "_ollama_executable",
                        lambda: "C:/Program Files/Ollama/ollama.exe")
    monkeypatch.setattr(gpu_tuning, "_current_python_executable", lambda: "")

    result = gpu_tuning.ensure_high_performance_gpu()

    assert result["already_set"], "an already-correct value must not be rewritten"
    assert not written


def test_ensure_reports_a_write_failure_instead_of_raising(monkeypatch):
    monkeypatch.setattr(gpu_tuning, "_read_preferences", lambda: {})
    monkeypatch.setattr(gpu_tuning, "_write_preference", lambda *a: False)
    monkeypatch.setattr(gpu_tuning, "_ollama_executable", lambda: "C:/x/ollama.exe")
    monkeypatch.setattr(gpu_tuning, "_current_python_executable", lambda: "")
    result = gpu_tuning.ensure_high_performance_gpu()
    assert result["failed"], "a failed write must be reported, not swallowed"
    assert result["applied"] == []


def test_power_plan_detection_marks_a_performance_plan(monkeypatch):
    monkeypatch.setattr("ai.gpu_status._power_scheme", lambda: "High performance")
    result = gpu_tuning.ensure_high_performance_gpu()
    assert result["plan"] == "High performance"
    assert result["plan_is_fast"] is True


def test_power_plan_detection_marks_balanced_as_slow(monkeypatch):
    monkeypatch.setattr("ai.gpu_status._power_scheme", lambda: "Balanced")
    result = gpu_tuning.ensure_high_performance_gpu()
    assert result["plan"] == "Balanced"
    assert result["plan_is_fast"] is False


def test_set_high_performance_plan_reports_failure_without_raising(monkeypatch):
    monkeypatch.setattr(gpu_tuning.os, "name", "posix")
    result = gpu_tuning.set_high_performance_power_plan()
    assert result["ok"] is False
    assert result["error"]
