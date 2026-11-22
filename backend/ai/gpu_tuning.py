"""GPU tuning — put the local model on the fast GPU and keep it there.

WHY THIS EXISTS
---------------
On a hybrid-graphics laptop (Intel iGPU drives the display, NVIDIA dGPU does
the compute), Windows treats the dGPU as a background device. Measured on
this machine (Acer Aspire A715-79G, RTX 3050 6 GB, qwen3.5:4b):

    before   dGPU limited to 30 W (of 60 W default) -> ~1200 MHz of 2100
             -> 25-35 tok/s -> live TTFT 3.4-4.6 s, total 9.5-18.5 s
    after    dGPU runs at 60 W -> ~1650 MHz -> 41 tok/s
             -> live TTFT 2.7 s, total 8.2 s

Two settings produce that, and BOTH are needed:

  1. ``GpuPreference=2`` under
     ``HKCU\\Software\\Microsoft\\DirectX\\UserGpuPreferences`` — tells
     Windows "run this app on the high-performance GPU". Without it the dGPU
     stays on its reduced compute-only TGP. This is the same value the
     Windows Settings → System → Display → Graphics → "High performance"
     toggle writes, so it shows up in the UI as such.

  2. A performance Windows power plan. With the "Balanced" plan the dGPU
     is throttled even when the 60 W limit is allowed: the same warm runner
     measured 3393 ms median TTFT on Balanced and 2681 ms on High
     performance (-21%).

This module applies (1) automatically and reports the state of (2). It is
best-effort: every operation is optional and failure is non-fatal.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

# Windows' per-app GPU preference key.
_GPU_PREF_KEY = r"Software\Microsoft\DirectX\UserGpuPreferences"
# 2 = "High performance" GPU. 1 = power saving, 0 = no preference.
_HIGH_PERFORMANCE = "GpuPreference=2;"


def _gpu_pref_key_path() -> str:
    return os.path.join("HKEY_CURRENT_USER", _GPU_PREF_KEY)


def _ollama_executable() -> str:
    """Locate ollama.exe — the process that actually holds the GPU."""
    candidates = []
    # An explicit override always wins.
    env = os.environ.get("OLLAMA_EXE")
    if env:
        candidates.append(env)
    # The default install location for the Windows desktop app.
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(os.path.join(local, "Programs", "Ollama", "ollama.exe"))
    # Whatever is on PATH.
    found = shutil.which("ollama")
    if found:
        candidates.append(found)
    for path in candidates:
        if path and os.path.exists(path):
            return os.path.abspath(path)
    return ""


def _current_python_executable() -> str:
    """The backend's own interpreter (it also uses the GPU for embeddings)."""
    return os.path.abspath(sys.executable)


def _read_preferences() -> dict:
    """The existing per-app GPU preferences (empty when the key is absent)."""
    try:
        import winreg
    except ImportError:
        return {}
    out: dict = {}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _GPU_PREF_KEY) as key:
            index = 0
            while True:
                try:
                    name, value, _typ = winreg.EnumValue(key, index)
                except OSError:
                    break
                out[name] = value
                index += 1
    except OSError:
        pass
    return out


def _write_preference(exe_path: str, value: str) -> bool:
    """Write one per-app GPU preference. True when the value is in place."""
    try:
        import winreg
    except ImportError:
        return False
    try:
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, _GPU_PREF_KEY, 0, winreg.KEY_WRITE
        ) as key:
            winreg.SetValueEx(key, exe_path, 0, winreg.REG_SZ, value)
        return True
    except OSError:
        return False


def ensure_high_performance_gpu() -> dict:
    """Make sure the local-model processes are assigned to the fast GPU.

    Returns a dict describing what happened:
      supported     — False on non-Windows hosts (nothing was changed)
      applied       — list of executables that are now on the fast GPU
      already_set   — list that already were
      failed        — list that could not be written
      plan          — active Windows power plan name
      plan_is_fast  — True when the plan is a performance plan
    """
    result = {
        "supported": os.name == "nt",
        "applied": [],
        "already_set": [],
        "failed": [],
        "plan": "",
        "plan_is_fast": False,
    }
    if not result["supported"]:
        return result

    try:
        from ai.gpu_status import _power_scheme
        plan = _power_scheme()
    except Exception:
        plan = ""
    result["plan"] = plan
    result["plan_is_fast"] = any(
        word in plan.lower() for word in ("performance", "ultimate", "high")
    )

    existing = _read_preferences()
    targets = []
    ollama = _ollama_executable()
    if ollama:
        targets.append(ollama)
    python_exe = _current_python_executable()
    if python_exe:
        targets.append(python_exe)

    for exe in targets:
        current = existing.get(exe, "")
        if current == _HIGH_PERFORMANCE:
            result["already_set"].append(exe)
            continue
        if _write_preference(exe, _HIGH_PERFORMANCE):
            result["applied"].append(exe)
        else:
            result["failed"].append(exe)
    return result


def set_high_performance_power_plan() -> dict:
    """Try to switch Windows to a performance power plan.

    Windows ships the "High performance" plan hidden on many laptops, so it
    is duplicated first when missing. Needs no admin for the duplication on
    current Windows builds, but the activation may still be refused on
    locked-down machines — hence the dict result instead of an exception.

    Returns: {"ok": bool, "plan": str, "error": str}
    """
    result = {"ok": False, "plan": "", "error": ""}
    if os.name != "nt":
        result["error"] = "not Windows"
        return result
    try:
        res = subprocess.run(["powercfg", "/list"], capture_output=True,
                             text=True, timeout=10)
        text = res.stdout or ""
        plans = {}
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("Power Scheme GUID:"):
                guid = line.split(":", 1)[1].split(")")[0].strip()
                name = line.rsplit("(", 1)[-1].rstrip(")").strip()
                plans[guid] = name
        fast = next((g for g, n in plans.items()
                     if any(w in n.lower() for w in
                            ("performance", "ultimate", "high"))), None)
        if fast is None:
            # Duplicate the hidden High performance plan.
            hidden = "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"
            dup = subprocess.run(["powercfg", "-duplicatescheme", hidden],
                                 capture_output=True, text=True, timeout=10)
            out = (dup.stdout or "") + (dup.stderr or "")
            if "Power Scheme GUID:" in out:
                fast = out.split("Power Scheme GUID:", 1)[1].split(")")[0].strip()
            else:
                result["error"] = out.strip()[:120] or "could not create plan"
                return result
        act = subprocess.run(["powercfg", "/setactive", fast],
                             capture_output=True, text=True, timeout=10)
        out = (act.stdout or "") + (act.stderr or "")
        result["ok"] = act.returncode == 0
        result["plan"] = plans.get(fast, fast)
        if not result["ok"]:
            result["error"] = out.strip()[:120] or "activation refused"
    except Exception as e:
        result["error"] = str(e)[:120]
    return result
