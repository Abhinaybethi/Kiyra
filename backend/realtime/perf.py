"""Per-answer latency profiling with monotonic timers only.

One :class:`PerformanceTracker` anchors a single answer attempt at ``t0``
(monotonic). Two kinds of measurements are captured:

* **stages** — ordered marks (ms since ``t0``) for the pipeline events
  (audio_end → vad_finalization → stt → question_detection → retrieval →
  context/prompt construction → llm_request/llm_ttft → first visible token →
  generation complete → total).
* **durations** — per-stage wall durations measured *where the stage runs*
  (so they stay meaningful when a later component owns the tracker, e.g. a
  speculative answer inheriting STT timing from its utterance).

Wall-clock (time.time) is never used for durations — profiling is monotonic
only. ``log()`` is idempotent so a superseded/validated speculative answer
can never print twice.
"""
from __future__ import annotations

import time
from typing import Optional

# Ordered pipeline stage marks (ms offsets from t0).
STAGES = (
    "audio_end",
    "vad_finalization",
    "stt_start",
    "stt_end",
    "question_detection",
    "retrieval_start",
    "retrieval_end",
    "context_construction",
    "prompt_construction",
    "llm_request",
    "llm_ttft",
    "first_visible_token",
    "generation_complete",
    "total",
)

# Per-stage durations (measured around each stage, independent of t0).
DURATIONS = (
    "vad_ms",
    "stt_ms",
    "question_detection_ms",
    "rag_ms",
    "context_ms",
    "prompt_ms",
    "llm_request_ms",
    "llm_ttft_ms",
    "first_token_ms",
    "generation_ms",
    "total_ms",
)

# Durations copied from the utterance tracker onto a speculative tracker
# (different t0 anchors — duration values remain valid, marks do not).
INHERIT_DURATIONS = ("vad_ms", "stt_ms", "question_detection_ms")
INHERIT_MARKS = (
    "audio_end",
    "vad_finalization",
    "stt_start",
    "stt_end",
    "question_detection",
)


class PerformanceTracker:
    """Latency profiler for one answer request."""

    def __init__(self, request_id: Optional[str] = None,
                 t0: Optional[float] = None):
        self.request_id = request_id
        self.t0: float = t0 if t0 is not None else time.monotonic()
        self.marks: dict[str, Optional[float]] = {s: None for s in STAGES}
        self.durations: dict[str, Optional[float]] = {d: None for d in DURATIONS}
        self.logged = False

    # ── Recording ────────────────────────────────────────────────────────

    def mark(self, stage: str) -> float:
        """Record an ordered stage mark; returns ms since t0."""
        ms = (time.monotonic() - self.t0) * 1000.0
        if stage in self.marks:
            self.marks[stage] = ms
        return ms

    def span(self, duration_key: str, started: float) -> float:
        """Record a duration measured from ``started`` (monotonic)."""
        ms = (time.monotonic() - started) * 1000.0
        self.set_duration(duration_key, ms)
        return ms

    def set_duration(self, duration_key: str, ms: float) -> None:
        if duration_key in self.durations:
            self.durations[duration_key] = float(ms)

    def first_visible(self, started: float) -> None:
        """Mark the first token actually flushed to the UI (idempotent)."""
        if self.marks.get("first_visible_token") is None:
            self.mark("first_visible_token")
            self.set_duration("first_token_ms",
                              (time.monotonic() - started) * 1000.0)

    def finish(self) -> None:
        """Close the tracker: total stage + total duration."""
        if self.marks.get("total") is None:
            self.mark("total")
        if self.durations.get("total_ms") is None:
            self.durations["total_ms"] = self.marks["total"]

    def inherit(self, other: Optional["PerformanceTracker"]) -> None:
        """Copy utterance-stage timings from another tracker (idempotent)."""
        if other is None or other is self:
            return
        for key in INHERIT_DURATIONS:
            if self.durations.get(key) is None and other.durations.get(key) is not None:
                self.durations[key] = other.durations[key]
        for stage in INHERIT_MARKS:
            if self.marks.get(stage) is None and other.marks.get(stage) is not None:
                self.marks[stage] = other.marks[stage]

    # ── Reporting ────────────────────────────────────────────────────────

    def report(self) -> dict:
        """Full structured report: stages + durations (None = not recorded)."""
        return {
            "request_id": self.request_id,
            "stages": {
                k: (round(v, 1) if v is not None else None)
                for k, v in self.marks.items()
            },
            "durations": {
                k: (round(v, 1) if v is not None else None)
                for k, v in self.durations.items()
            },
        }

    def lines(self) -> list[str]:
        out = ["[PERF] request=%s" % (self.request_id or "-")]
        for key in DURATIONS:
            val = self.durations.get(key)
            out.append("%s=%s" % (key[:-3] if key.endswith("_ms") else key,
                                  "-" if val is None else round(val, 1)))
        stage_bits = []
        for stage in STAGES:
            val = self.marks.get(stage)
            stage_bits.append("%s=%s" % (stage,
                                         "-" if val is None else round(val, 1)))
        out.append("stages: " + " ".join(stage_bits))
        return out

    def log(self) -> None:
        """Print the [PERF] report once."""
        if self.logged:
            return
        self.logged = True
        for line in self.lines():
            print(line)
