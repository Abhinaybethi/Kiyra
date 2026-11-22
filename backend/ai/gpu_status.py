"""GPU power-state probe — explains "the app got slow" from the outside.

Local inference on a laptop is GPU-bound, and the GPU's *power budget* is
the single biggest factor in how fast an answer appears. Two different
things cap it, and they need different fixes:

  1. BATTERY. Running unplugged halves the token throughput. Fix: plug in
     the charger and use the "Best performance" power plan.

  2. HYBRID-GRAPHICS (Optimus). On a laptop with an Intel iGPU driving the
     display, the NVIDIA dGPU runs *compute only* and the driver caps it to
     a low TGP. Measured on this machine (Acer Aspire A715-79G, RTX 3050):
     the dGPU was limited to **30 W** of its 60 W default / 75 W maximum
     *while plugged in*, ran at ~1.2 GHz of a 2.1 GHz maximum, and drew
     ~29 W at 100% utilization — so the power cap, not the battery, was
     the limit. Fix: NVIDIA Control Panel → Manage 3D settings → Power
     management mode → "Prefer maximum performance" (or an admin
     `nvidia-smi -pl 60`).

The probe reports which of these is actually in play, because "plug in the
charger" is useless advice when the machine is already on AC and the
battery is at 100%.

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


def _nvidia_gpu_state() -> dict:
    """Probe the NVIDIA GPU: power limit, clocks and display routing.

    Returns a dict with any of these keys (missing/None when nvidia-smi is
    unavailable or the query fails):
      power_limit_w        — the limit the driver is enforcing RIGHT NOW
      default_power_limit_w — the card's default TGP
      max_power_limit_w    — the card's maximum TGP
      clock_mhz            — current SM clock
      max_clock_mhz        — maximum SM clock
      display_active       — True when the dGPU drives a display
    """
    out: dict = {}
    try:
        res = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=clocks.sm,clocks.max.sm,display_active",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=8)
        row = (res.stdout or "").strip().splitlines()[0]
        parts = [p.strip() for p in row.split(",")]
        if len(parts) >= 3:
            out["clock_mhz"] = int(float(parts[0]))
            out["max_clock_mhz"] = int(float(parts[1]))
            out["display_active"] = parts[2].lower().startswith("enabled")
    except Exception:
        pass

    try:
        res = subprocess.run(["nvidia-smi", "-q", "-d", "POWER"],
                             capture_output=True, text=True, timeout=8)
        text = res.stdout or ""
        for label, key in (("Current Power Limit", "power_limit_w"),
                           ("Default Power Limit", "default_power_limit_w"),
                           ("Max Power Limit", "max_power_limit_w")):
            for line in text.splitlines():
                if line.strip().startswith(label):
                    raw = line.split(":", 1)[1].strip().split()[0]
                    try:
                        out[key] = int(float(raw))
                    except (ValueError, IndexError):
                        pass
                    break
    except Exception:
        pass
    return out


def gpu_power_state() -> dict:
    """Describe the machine's GPU power situation.

    Returns a dict with:
      supported  — False on non-Windows hosts (no probe was run)
      on_battery — True/False/None (None = unknown)
      percent    — battery charge percentage, or None
      scheme     — active Windows power plan name
      note       — human-readable explanation, empty when nothing is wrong
    plus the raw NVIDIA numbers (power_limit_w, clock_mhz, …) when a GPU
    is present.
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

    gpu = _nvidia_gpu_state()
    state.update(gpu)

    if state["on_battery"]:
        state["note"] = (
            "Running on battery: the GPU is power-capped, so answers take "
            "roughly twice as long. Plug in the charger and set the power "
            "plan to Best performance."
        )
        return state

    # On AC. The remaining cap is the hybrid-graphics one: the dGPU runs
    # compute-only (the iGPU drives the display) and the driver holds it at
    # a fraction of its TGP. "Plug in the charger" would be useless advice
    # here — the machine is already on AC.
    limit = gpu.get("power_limit_w")
    default = gpu.get("default_power_limit_w")
    if limit and default and limit < default * 0.7:
        state["note"] = (
            f"GPU power-capped to {limit} W of its {default} W default "
            f"(the iGPU drives the display, so the RTX card runs compute-only "
            f"at a reduced TGP). This roughly doubles answer latency. Fix: "
            f"NVIDIA Control Panel -> Manage 3D settings -> Power management "
            f"mode -> 'Prefer maximum performance'."
        )
    return state
