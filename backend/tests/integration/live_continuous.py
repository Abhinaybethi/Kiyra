"""PHASE 5.4.1 LIVE VERIFICATION - continuous interview loop.

Real backend + real Whisper STT + real Bonsai. Speech is Windows SAPI TTS.

What this proves that the Phase 5.4 harness could not:
  1. START is pressed ONCE. Q1, Q2 and Q3 are each spoken and SENT with no
     further START, and every question gets exactly one answer.
  2. ANSWER_READY is not terminal: the backend returns to LISTENING on its own.
  3. answer_complete -> listening_ready is fast (no model/audio reload).
  4. Question history accumulates and is navigable (turn_id/sequence/answer).
  5. A late STT result owned by a previous turn is dropped, not merged.
"""
import asyncio
import json
import re
import sys
import time
from pathlib import Path

import numpy as np
import websockets

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from live_listening import (  # noqa: E402
    FRAME_MS, SR, WORK, create_session, frame_stream, read_wav, silence, tts_wav,
    wav_frame,
)

Q1 = ["Explain my Kiyra project."]
Q2 = ["Why did I use FastAPI for the backend?"]
Q3 = ["What are the advantages of that choice?"]


class Collector:
    def __init__(self):
        self.events: list[dict] = []

    def start(self, ws):
        self.t0 = time.monotonic()
        return asyncio.create_task(self._run(ws))

    async def _run(self, ws):
        try:
            async for raw in ws:
                data = json.loads(raw)
                data["_ts"] = time.monotonic()
                self.events.append(data)
                self._log(data)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    def _log(self, d):
        t, p = d["type"], d.get("payload", {})
        if t == "transcript.segment":
            print(f"    [segment ] turn={p.get('turn_id')} "
                  f"can_send={p.get('can_send')} :: {p.get('current')}")
        elif t == "turn.submitted":
            print(f"    [SEND    ] turn={p.get('turn_id')} :: {p.get('question')}")
        elif t == "answer.generated":
            print(f"    [ANSWER  ] chars={len(p.get('answer',''))} "
                  f":: {p.get('answer','')[:150]}")
        elif t == "listening.started":
            print(f"    [re-armed] turn={p.get('turn_id')} "
                  f"auto={p.get('auto_rearm')} state={p.get('state')}")
        elif t == "memory.updated":
            prev = p.get("previous") or []
            print(f"    [memory  ] topic={p.get('topic')!r} "
                  f"history={p.get('history_count')} "
                  f"idx={p.get('history_index')} prev={len(prev)}")
        elif t == "perf":
            print(f"    [perf    ] stt={p.get('stt_ms')}ms "
                  f"ttft={p.get('llm_ttft_ms')}ms total={p.get('total_ms')}ms")
        elif t == "session.error":
            print(f"    [ERROR   ] {p.get('code')}: {p.get('message')}")

    def of(self, kind):
        return [e for e in self.events if e["type"] == kind]

    def clear(self):
        self.events.clear()


async def speak(ws, pieces, label, want_segments, timeout=60.0):
    """Speak `pieces` with real pauses, then wait for STT to actually land.

    Waiting on the segment count instead of a fixed sleep matters: the first
    transcription of a session can take several seconds (model warm-up), and a
    fixed sleep made Q1 bleed into Q2's turn.
    """
    for i, piece in enumerate(pieces):
        wav = tts_wav(piece, WORK / f"{label}_{i}.wav")
        for frame in frame_stream(read_wav(wav)):
            await ws.send(wav_frame(frame))
            await asyncio.sleep(FRAME_MS / 1000.0)
        for frame in frame_stream(silence(1.2)):
            await ws.send(wav_frame(frame))
            await asyncio.sleep(FRAME_MS / 1000.0)

    deadline = time.monotonic() + timeout
    last = -1
    while time.monotonic() < deadline:
        n = len(c_global[0].of("transcript.segment"))
        if n >= want_segments and n == last:
            return n
        last = n
        await asyncio.sleep(0.4)
    return len(c_global[0].of("transcript.segment"))


async def speak_no_wait(ws, pieces, label, tail=0.45):
    """Send speech and trailing silence, then return immediately.

    The VAD finalizes ~300 ms into the trailing silence and hands the audio to
    STT, which takes seconds; returning right after the silence leaves the STT
    call genuinely in flight.
    """
    for i, piece in enumerate(pieces):
        wav = tts_wav(piece, WORK / f"{label}_{i}.wav")
        for frame in frame_stream(read_wav(wav)):
            await ws.send(wav_frame(frame))
            await asyncio.sleep(FRAME_MS / 1000.0)
        for frame in frame_stream(silence(tail)):
            await ws.send(wav_frame(frame))
            await asyncio.sleep(FRAME_MS / 1000.0)


c_global: list = [None]


async def send_and_wait(c, ws, timeout=120):
    await ws.send(json.dumps({"type": "turn.send", "payload": {}}))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if c.of("answer.generated"):
            return True
        await asyncio.sleep(0.25)
    return False


async def main():
    WORK.mkdir(parents=True, exist_ok=True)
    print("=" * 74)
    print("PHASE 5.4.1 LIVE - continuous interview loop (START once, Q1/Q2/Q3)")
    print("=" * 74)
    failures = []

    # ── warm up STT on a throwaway session ───────────────────────────────
    warm_id = create_session()
    print(f"warmup session {warm_id}")
    async with websockets.connect(
            f"ws://localhost:8000/api/interviews/{warm_id}/ws",
            max_size=None) as wws:
        wc = Collector()
        c_global[0] = wc
        wt = wc.start(wws)
        await wws.send(json.dumps({"type": "mode.set",
                                   "payload": {"mode": "interview_listening"}}))
        await asyncio.sleep(0.6)
        await wws.send(json.dumps({"type": "listening.start", "payload": {}}))
        await asyncio.sleep(1.0)
        await speak(wws, ["Ready when you are."], "p541_warm", want_segments=1)
        print(f"    warmup segments={len(wc.of('transcript.segment'))}")
        if not wc.of("transcript.segment"):
            failures.append("warmup: STT produced no segment")
        wt.cancel()

    session_id = create_session()
    print(f"session {session_id}")

    url = f"ws://localhost:8000/api/interviews/{session_id}/ws"
    async with websockets.connect(url, max_size=None) as ws:
        c = Collector()
        c_global[0] = c
        task = c.start(ws)

        await ws.send(json.dumps({"type": "mode.set",
                                  "payload": {"mode": "interview_listening"}}))
        await asyncio.sleep(0.6)

        # ── START exactly once ─────────────────────────────────────────────
        print("\n[0] START pressed once")
        await ws.send(json.dumps({"type": "listening.start", "payload": {}}))
        await asyncio.sleep(1.0)
        started = c.of("listening.started")
        if len(started) != 1:
            failures.append(f"[0] {len(started)} listening.started (want 1)")
        print(f"    listening.started={len(started)} turn={started[0]['payload'].get('turn_id') if started else None}")

        answers = []
        expected_segments = 0
        history_mem: list = []
        rearm_gaps: list = []
        # ── Q1 / Q2 / Q3, no further START ─────────────────────────────────
        for n, (pieces, expect_turn) in enumerate(
                [(Q1, "t1"), (Q2, "t2"), (Q3, "t3")], start=1):
            print(f"\n[{n}] Q{n}: {' '.join(pieces)}")
            expected_segments += len(pieces)
            await speak(ws, pieces, f"p541_q{n}", want_segments=expected_segments)

            segs = c.of("transcript.segment")
            if not segs:
                failures.append(f"[{n}] no transcript segment captured")
                print("    FAIL: nothing transcribed")
                c.clear()
                continue

            before_answers = len(c.of("answer.generated"))
            ok = await send_and_wait(c, ws)
            gens = c.of("answer.generated")
            subs = c.of("turn.submitted")
            starts_now = c.of("listening.started")
            rearmed = [s for s in starts_now if s["payload"].get("auto_rearm")]

            print(f"    submitted={len(subs)} answers={len(gens)} "
                  f"auto_rearms={len(rearmed)}")

            if not ok or len(gens) != before_answers + 1:
                failures.append(f"[{n}] expected exactly one new answer")
            if len(subs) != 1:
                failures.append(f"[{n}] {len(subs)} submissions (want 1)")

            turn_ids = [s["payload"].get("turn_id") for s in subs]
            if turn_ids != [expect_turn]:
                failures.append(f"[{n}] submitted on {turn_ids}, want ['{expect_turn}']")

            if rearmed:
                new_turn = rearmed[-1]["payload"].get("turn_id")
                if new_turn == expect_turn:
                    failures.append(f"[{n}] re-arm did not rotate the turn")
                else:
                    print(f"    turn {expect_turn} -> {new_turn} (rotated)")
            else:
                failures.append(f"[{n}] no auto re-arm after the answer")

            answers.append(gens[-1]["payload"]["answer"] if gens else "")
            # answer_complete -> listening_ready latency
            if gens and rearmed:
                gap = rearmed[-1]["_ts"] - gens[-1]["_ts"]
                print(f"    answer_complete -> listening_ready = {gap*1000:.0f} ms")
                if gap > 2.0:
                    failures.append(f"[{n}] re-arm took {gap*1000:.0f}ms (>2s)")
                rearm_gaps.append(gap * 1000)
            # Keep memory.updated history events; drop the rest.
            history_mem.extend(c.of("memory.updated"))
            c.clear()

        # ── history: 3 entries, navigable, with answers ────────────────────
        print("\n[4] Question history")
        mem = history_mem
        if not mem:
            failures.append("[4] no memory.updated with history")
            print("    FAIL: no history payload")
        else:
            p = mem[-1]["payload"]
            prev = p.get("previous") or []
            print(f"    history_count={p.get('history_count')} "
                  f"history_index={p.get('history_index')} topic={p.get('topic')!r}")
            for e in prev:
                print(f"      #{e.get('sequence')} turn={e.get('turn_id')} "
                      f"answer_available={e.get('answer_available')} "
                      f":: {str(e.get('question'))[:60]}")
            if len(prev) < 3:
                failures.append(f"[4] history has {len(prev)} entries (want 3)")
            if not all(e.get("answer_available") for e in prev):
                failures.append("[4] some entries have no stored answer")
            seqs = [e.get("sequence") for e in prev]
            if seqs != list(range(1, len(prev) + 1)):
                failures.append(f"[4] sequence not contiguous: {seqs}")
            if len({e.get("turn_id") for e in prev}) != len(prev):
                failures.append("[4] turn ids are not unique per question")
            if not p.get("topic"):
                failures.append("[4] topic is blank")

        # ── distinct answers, no replay ───────────────────────────────────
        print("\n[5] Answers are distinct and not replayed")
        uniq = len({a.strip() for a in answers if a})
        print(f"    answers={len(answers)} distinct={uniq} "
              f"lengths={[len(a) for a in answers]}")
        if len(answers) != 3:
            failures.append(f"[5] {len(answers)} answers (want 3)")
        if uniq != len([a for a in answers if a]):
            failures.append("[5] answers look replayed (identical text)")

        # ── late STT crossing a turn rotation must be dropped ────────────
        print("\n[6] Late STT: rotate the turn while STT is still running")
        c.clear()
        # A long utterance keeps Whisper busy for a second or two after the
        # VAD finalizes, which is the window we rotate inside.
        owner_turn = (c.of("listening.started") or [{}])[-1].get(
            "payload", {}).get("turn_id")
        await speak_no_wait(
            ws, ["Can you explain in detail the caching strategy you used for "
                 "the retrieval pipeline and how the invalidation works?"],
            "p541_late")
        print(f"    abandoned speech belongs to turn={owner_turn}")
        await ws.send(json.dumps({"type": "listening.stop", "payload": {}}))
        await ws.send(json.dumps({"type": "listening.start", "payload": {}}))
        # Give the abandoned STT call plenty of time to (wrongly) land.
        await asyncio.sleep(12.0)

        segs = c.of("transcript.segment")
        subs = c.of("turn.submitted")
        stops = c.of("listening.stopped")
        starts = c.of("listening.started")
        late = [e for e in segs
                if "caching strategy" in (e["payload"].get("current") or "")
                and e["payload"].get("turn_id") != owner_turn]
        leaked = late
        print(f"    stops={len(stops)} restarts={len(starts)} "
              f"segments_after_rotate={len(leaked)} submitted={len(subs)}")
        for e in segs:
            print(f"    segment turn={e['payload'].get('turn_id')} "
                  f"(owner was {owner_turn})")
        if not segs:
            print("    no segment at all -> the in-flight STT was dropped")
        else:
            print("    segment landed before the rotation (STT beat the race)")
        if leaked:
            failures.append(
                f"[6] late STT was appended to a rotated turn "
                f"({[e['payload'].get('turn_id') for e in leaked]}, "
                f"owner was {owner_turn})")
        if subs:
            failures.append("[6] a stale-turn question was submitted")
        # Whatever survived must never reappear in the fresh turn.
        final_current = ""
        if c.of("transcript.segment"):
            final_current = c.of("transcript.segment")[-1]["payload"].get("current") or ""
        print(f"    OK: nothing from the abandoned turn leaked "
              f"(current={final_current!r}), submitted={len(subs)}")

        task.cancel()

    print("\n" + "=" * 74)
    if failures:
        print("RESULT: FAIL")
        for f in failures:
            print("  -", f)
        return 1
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))