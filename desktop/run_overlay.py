"""Launch Kiyra's Windows overlay with an explicitly chosen audio-capable Python.

The backend runs in ``backend/.venv`` (Python 3.13+) and deliberately does NOT
ship ``pyaudiowpatch``; WASAPI loopback capture needs a desktop interpreter that
does.  Selecting ``python`` from PATH is therefore a coin flip.  This launcher
probes known interpreters, verifies the WASAPI loopback endpoint, and only then
starts the overlay.  There is no microphone fallback: if loopback validation
fails, startup is refused with an actionable message.

Usage:  python desktop/run_overlay.py [stealth_overlay.py arguments]
        KIYRA_DESKTOP_PYTHON=C:\\Python311\\python.exe python desktop/run_overlay.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OVERLAY = HERE / "stealth_overlay.py"


def _candidate_interpreters() -> list[str]:
    """Ordered candidates: explicit override, py-launcher, known installs, current."""
    found: list[str] = []

    def add(path):
        if path and Path(path).exists() and path not in found:
            found.append(path)

    add(os.environ.get("KIYRA_DESKTOP_PYTHON"))
    if sys.platform == "win32":
        py = shutil.which("py")
        if py:
            for tag in ("-3.11", "-3.12", "-3.13", "-3.14"):
                try:
                    out = subprocess.run(
                        [py, tag, "-c", "import sys; print(sys.executable)"],
                        capture_output=True, text=True, timeout=10, check=True,
                    ).stdout.strip()
                    add(out)
                except Exception:
                    pass
    for version in ("311", "312", "313", "314"):
        add(Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
            / f"Python{version}" / "python.exe")
    add(sys.executable)
    return found


def _probe(interpreter: str) -> tuple[bool, str]:
    """True when this interpreter has the audio stack AND a real loopback endpoint."""
    code = (
        "import sys; "
        f"sys.path.insert(0, {str(HERE)!r}); "
        "import numpy, pyaudiowpatch, websockets; "
        "import audio_loopback as a; "
        "info = a.find_wasapi_loopback_device(verbose=False); "
        "print(info['name'] if info else ''); "
        "sys.exit(0 if info else 2)"
    )
    try:
        result = subprocess.run(
            [interpreter, "-c", code], capture_output=True, text=True,
            timeout=30, cwd=str(ROOT),
        )
    except Exception as exc:
        return False, f"probe failed: {exc}"
    if result.returncode == 0:
        return True, result.stdout.strip()
    if result.returncode == 2:
        return False, "no WASAPI loopback endpoint for the active playback device"
    return False, (result.stderr or result.stdout).strip()[:300] or \
        "missing numpy, pyaudiowpatch or websockets"


def select_interpreter() -> str:
    """Return the first audio-capable interpreter, or raise with all failures."""
    failures = []
    for interpreter in _candidate_interpreters():
        ok, detail = _probe(interpreter)
        if ok:
            print(f"[DESKTOP_RUNTIME] {interpreter} — loopback: {detail}")
            return interpreter
        failures.append(f"{interpreter}: {detail}")
    raise RuntimeError(
        "No Python interpreter with a working WASAPI loopback was found.\n"
        + "\n".join(failures)
        + "\nInstall dependencies with: "
          "python -m pip install numpy pyaudiowpatch websockets"
    )


def main() -> int:
    try:
        interpreter = select_interpreter()
    except RuntimeError as exc:
        print(f"[DESKTOP_RUNTIME] startup refused: {exc}", file=sys.stderr)
        return 2
    # -u keeps the pipeline logs live when stdout is redirected to a file.
    return subprocess.call([interpreter, "-u", str(OVERLAY), *sys.argv[1:]],
                           cwd=str(ROOT))


if __name__ == "__main__":
    sys.exit(main())
