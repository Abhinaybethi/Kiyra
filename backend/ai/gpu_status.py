"""GPU power-state probe — explains "the app got slow" from the outside.

Local inference on a laptop is GPU-bound, and a laptop GPU running on
battery is hard-capped by the OS: measured on this machine, the RTX 3050
pulled only ~30 W at 100% utilization with clocks pinned near 1.1 GHz of a
2.1 GHz maximum, which roughly halves token throughput versus AC power
(~95 W). The symptom is indistinguishable from "the model got slower", so
the backend reports the power state explicitly instead of leaving it to be
rediscovered.

Windows-only; every probe is best-effort and never raises.

Use:  from ai.gpu_status import gpu_power_state
"""
from __future__ import annotations

import ctypes
import platform
import subprocess


class _SystemPowerStatus(ctypes.Structure):
    """Win32 SYSTEM_POWER_STATUS (see kernel32.GetSystemPowerStatus)."""

    _fields_ = [
        ("ACLineStatus", ctypes.c_byte),
        ("BatteryFlag", ctypes.c_byte),
        ("BatteryLifePercent", ctypes.c_byte),
        ("SystemStatusFlag", ctypes.c_byte),
        ("BatteryLifeTime", ctypes.c_uint32),
        ("BatteryFullLifeTime", ctypes.c_uint32),
    ]


def _ac_line_status():
    """1 = on AC, 0 = on battery, 255 = unknown. None if unavailable."""
    try:
        status = _SystemPowerStatus()
        if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
            return None
        value = int(status.ACLineStatus)
        return value if value in (0, 1) else None
    except Exception:
        return None


def _power_scheme() -> str:
    """Name of the active Windows power plan (e.g. 'Balanced')."""
    try:
        exe = "powercfg"
        out = subprocess.run([exe, "/getactivescheme"], capture_output=True,
                             text=True, timeout=8)
        text = (out.stdout or "") + (out.stderr or "")
        if "(" in text and text.rstrip().endswith(")"):
            return text.split("(", 1)[1].rstrip(")").strip()
        return text.strip()[:60]
    except Exception:
        return ""


def gpu_power_state() -> dict:
    """Describe the machine's GPU power situation.

    Returns a dict with:
      supported  — False on non-Windows hosts (no probe was run)
      on_battery — True/False/None (None = unknown)
      percent    — battery charge percentage, or None
      scheme     — active Windows power plan name
      note       — human-readable explanation, empty when nothing is wrong
    """
    state = {
        "supported": platform.system() == "Windows",
        "on_battery": None,
        "percent": None,
        "scheme": "",
        "note": "",
    }
    if not state["supported"]:
        return state

    ac = _ac_line_status()
    state["on_battery"] = (ac == 0) if ac is not None else None
    state["scheme"] = _power_scheme()

    try:
        status = _SystemPowerStatus()
        ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status))
        pct = int(status.BatteryLifePercent)
        if 0 < pct <= 100:
            state["percent"] = pct
    except Exception:
        pass

    if state["on_battery"]:
        state["note"] = (
            "Running on battery: the GPU is power-capped, so answers take "
            "roughly twice as long. Plug in the charger and set the power "
            "plan to Best performance."
        )
    return state
