"""Desktop runtime ownership: the launcher must never run the backend venv.

WASAPI loopback depends on pyaudiowpatch, which lives in the system Python that
runs the overlay — not in backend/.venv.  These tests verify the explicit
selection and fail-closed behaviour without touching real interpreters.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "desktop"))

import run_overlay  # noqa: E402


def test_selects_first_audio_capable_interpreter(monkeypatch):
    monkeypatch.setattr(run_overlay, "_candidate_interpreters",
                        lambda: ["backend/.venv/python.exe", "Python311/python.exe"])
    probes = []

    def _probe(path):
        probes.append(path)
        return ("Python311" in path, "Loopback [Loopback]" if "Python311" in path
                else "pyaudiowpatch missing")

    monkeypatch.setattr(run_overlay, "_probe", _probe)
    assert run_overlay.select_interpreter() == "Python311/python.exe"
    assert probes == ["backend/.venv/python.exe", "Python311/python.exe"]


def test_refuses_to_launch_without_a_loopback(monkeypatch, capsys):
    monkeypatch.setattr(run_overlay, "_candidate_interpreters",
                        lambda: ["backend/.venv/python.exe"])
    monkeypatch.setattr(run_overlay, "_probe",
                        lambda _path: (False, "no WASAPI loopback endpoint"))

    assert run_overlay.main() == 2
    out = capsys.readouterr().err
    assert "startup refused" in out
    assert "no WASAPI loopback endpoint" in out
