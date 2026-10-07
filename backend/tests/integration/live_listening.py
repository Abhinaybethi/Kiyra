"""LIVE end-to-end Interview Listening Mode check against the real backend.

This is NOT a unit test. It drives a real WebSocket session against a running
FastAPI backend + Bonsai, with real audio frames, the real VAD and the real
faster-whisper STT. Speech is synthesized with Windows SAPI TTS because there
is no human at the keyboard; everything downstream of the microphone is real.

The file name deliberately does not start with ``test_`` so pytest never
collects it (it needs a live backend, GPU model and Windows SAPI).

Run:  cd backend && uv run python tests\\integration\\live_listening.py
"""
from __future__ import annotations

import asyncio
import io
import json
import re
import subprocess
import sys
import time
import wave
from pathlib import Path

import numpy as np
import requests
import websockets

BACKEND = "http://localhost:8000"
SR = 16000
FRAME_MS = 250          # matches desktop/audio_loopback.py
VOICE_SPEED = 0         # SAPI rate (-10..10)


# ── speech synthesis ─────────────────────────────────────────────────────────
def tts_wav(text: str, path: Path) -> Path:
    """Synthesize `text` to a 16 kHz mono WAV via Windows SAPI."""
    ps = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        f"$s.Rate = {VOICE_SPEED}; "
        f"$s.SetOutputToWaveFile('{path}'); "
        f"$s.Speak('{text}'); $s.Dispose()"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True,
                   capture_output=True, timeout=120)
    # Resample whatever SAPI wrote to 16 kHz mono int16.
    with wave.open(str(path), "rb") as wf:
        ch, sw, sr, n = (wf.getnchannels(), wf.getsampwidth(),
                         wf.getframerate(), wf.getnframes())
        raw = wf.readframes(n)
    pcm = np.frombuffer(raw, dtype=np.int16)
    if ch > 1:
        pcm = pcm.reshape(-1, ch)[:, 0]
    if sw > 2:
        pcm = (pcm.astype(np.int32) >> 8).astype(np.int16)
    if sr != SR:
        idx = (np.arange(int(n * SR / sr)) * sr / SR).astype(np.int64)
        pcm = pcm[np.clip(idx, 0, len(pcm) - 1)]
    pcm = pcm.astype(np.int16)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(SR)
        out.writeframes(pcm.tobytes())
    return path


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as wf:
        raw = wf.readframes(wf.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def wav_frame(samples: np.ndarray) -> bytes:
    pcm = (np.clip(samples, -1, 1) * 32767).astype(np.int16)
    bio = io.BytesIO()
    with wave.open(bio, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SR)
        wf.writeframes(pcm.tobytes())
    return bio.getvalue()


def frame_stream(samples: np.ndarray, chunk: int = SR * FRAME_MS // 1000):
    for i in range(0, len(samples), chunk):
        yield samples[i:i + chunk]


def silence(seconds: float) -> np.ndarray:
    return np.zeros(int(SR * seconds), dtype=np.float32)


# ── session ──────────────────────────────────────────────────────────────────
def create_session() -> int:
    r = requests.post(f"{BACKEND}/api/interviews", json={
        "mode": "live",
        "interview_type": "mixed",
        "title": "Phase 5.4 live listening verification",
    }, timeout=30)
    r.raise_for_status()
    return r.json()["id"]


def looks_replayed(new: str, old: str, min_shared_words: int = 12,
                   min_shared_ratio: float = 0.35) -> bool:
    """True when `new` restates or continues `old` instead of answering fresh.

    The shared opening run must dominate the new answer — a first-person voice
    may legitimately re-open sentences the same way ("I built a real-time data
    processing system…"), so 20 identical words inside a 100-word answer is
    not a replay, while a 79-word continuation of a previous answer is.
    """
    a, b = " ".join((new or "").split()), " ".join((old or "").split())
    if not a or not b:
        return False
    if a == b:
        return True
    shared = 0
    for wa, wb in zip(a.split(), b.split()):
        if wa != wb:
            break
        shared += 1
    return (shared >= min_shared_words
            and shared / max(1, len(a.split())) >= min_shared_ratio)


async def run_turn(ws, pieces, label, auto_send_delay=0.0, send=True):
    """Speak `pieces` with pauses, then (optionally) press SEND. Returns events."""
    seen = []
    task = asyncio.create_task(_collect(ws, seen))
    await ws.send(json.dumps({"type": "mode.set",
                              "payload": {"mode": "interview_listening"}}))
    await asyncio.sleep(0.4)
    await ws.send(json.dumps({"type": "listening.start", "payload": {}}))
    await asyncio.sleep(0.6)

    t0 = time.monotonic()
    for piece in pieces:
        wav = tts_wav(piece, WORK / f"{label}_{len(seen)}.wav")
        for frame in frame_stream(read_wav(wav)):
            await ws.send(wav_frame(frame))
            await asyncio.sleep(FRAME_MS / 1000.0)
        # A real pause between sentences -> the VAD finalizes a segment.
        for frame in frame_stream(silence(1.2)):
            await ws.send(wav_frame(frame))
            await asyncio.sleep(FRAME_MS / 1000.0)
    speak_s = time.monotonic() - t0

    # Give the VAD/STT time to settle, WITHOUT sending anything.
    await asyncio.sleep(3.0)
    pre_send = list(seen)

    submitted = 0
    if send:
        await ws.send(json.dumps({"type": "turn.send", "payload": {}}))
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if any(e["type"] == "answer.generated" for e in seen):
                break
            await asyncio.sleep(0.3)
        submitted = sum(1 for e in seen if e["type"] == "turn.submitted")
    else:
        await asyncio.sleep(1.0)

    task.cancel()
    return pre_send, seen, speak_s, submitted


async def _collect(ws, sink):
    try:
        async for raw in ws:
            data = json.loads(raw)
            sink.append(data)
            t = data["type"]
            p = data.get("payload", {})
            if t == "transcript.segment":
                print(f"    [segment ] can_send={p.get('can_send')} "
                      f"dup={p.get('duplicate')} :: {p.get('current')}")
            elif t == "question.detected":
                print(f"    [question] {p.get('question')}")
            elif t == "answer.generating":
                print(f"    [generating] {p.get('request_id')}")
            elif t == "turn.submitted":
                print(f"    [SEND    ] {p.get('question')}")
            elif t == "answer.generated":
                print(f"    [ANSWER  ] {p.get('answer','')[:220]}")
            elif t == "perf":
                print(f"    [perf    ] stt={p.get('stt_ms')}ms "
                      f"rag={p.get('retrieval_ms')}ms ttft={p.get('llm_ttft_ms')}ms "
                      f"total={p.get('total_ms')}ms listen={p.get('listen_ms')}ms")
            elif t == "memory.updated":
                print(f"    [memory  ] points={len(p.get('memory_points') or [])} "
                      f"prev={len(p.get('previous') or [])} topic={p.get('topic')!r}")
            elif t == "session.error":
                print(f"    [ERROR   ] {p.get('code')}: {p.get('message')}")
            elif t == "answer.cancelled":
                print(f"    [cancelled] {p.get('reason')}")
    except asyncio.CancelledError:
        raise
    except Exception:
        pass


WORK = Path(__file__).resolve().parent / "_live_audio"


async def main():
    WORK.mkdir(parents=True, exist_ok=True)
    print("=" * 72)
    print("PHASE 5.4 LIVE LISTENING VERIFICATION (real backend + real STT + Bonsai)")
    print("=" * 72)
    session_id = create_session()
    print(f"session {session_id}")

    failures = []
    url = f"ws://localhost:8000/api/interviews/{session_id}/ws"
    async with websockets.connect(url, max_size=None) as ws:
        # ── CASE 1: silence + speech must never auto-submit ────────────────
        print("\n[1] VAD pauses must NOT auto-submit (no SEND pressed)")
        pre, all_ev, secs, _ = await run_turn(
            ws, ["Can you explain the architecture of your project?"],
            "case1", send=False)
        kinds = [e["type"] for e in all_ev]
        if "turn.submitted" in kinds or "answer.generated" in kinds:
            failures.append("case1: auto-submitted without SEND")
            print("    FAIL: generation happened without SEND")
        else:
            segs = [e for e in all_ev if e["type"] == "transcript.segment"]
            print(f"    OK: {len(segs)} segment(s), no generation "
                  f"({secs:.1f}s of speech)")

        # ── CASE 2: multi-pause question merges into ONE question ──────────
        print("\n[2] Three pauses -> one merged question -> exactly one SEND")
        pre, all_ev, secs, submitted = await run_turn(
            ws, ["Can you explain my project",
                 "and tell me what architecture you used",
                 "and why you selected FastAPI for the backend?"],
            "case2", send=True)
        kinds = [e["type"] for e in all_ev]
        segs = [e for e in all_ev if e["type"] == "transcript.segment"]
        subs = [e for e in all_ev if e["type"] == "turn.submitted"]
        gens = [e for e in all_ev if e["type"] == "answer.generated"]
        print(f"    segments={len(segs)} submitted={submitted} "
              f"answers={len(gens)}")
        if submitted != 1:
            failures.append(f"case2: {submitted} submissions (want 1)")
        if len(gens) != 1:
            failures.append(f"case2: {len(gens)} answers (want 1)")
        if len(segs) < 2:
            failures.append(f"case2: only {len(segs)} segment(s); STT may not "
                            f"have split the pauses")
        if subs and len(re.findall(r"\S+", subs[0]["payload"]["question"])) < 8:
            failures.append("case2: submitted question too short")
        prev_answer = gens[-1]["payload"].get("answer", "") if gens else ""

        # ── CASE 3: RAG concept -> must stay generic, no GitHub, no replay ─
        print("\n[3] Generic RAG concept question (must stay generic, no GitHub)")
        pre, all_ev, secs, submitted = await run_turn(
            ws, ["What is RAG and how does it work in general?"], "case3")
        gen = [e for e in all_ev if e["type"] == "answer.generated"]
        if gen:
            p = gen[-1]["payload"]
            ans = p.get("answer", "")
            # `project` is reported on answer.generating, not answer.generated.
            generating = [e for e in all_ev
                          if e["type"] == "answer.generating"]
            project = (generating[-1]["payload"].get("project")
                       if generating else None)
            if project is not None:
                failures.append("case3: generic question was project-grounded")
            if "PROJECT EVIDENCE" in (p.get("prompt") or ""):
                failures.append("case3: PROJECT EVIDENCE leaked into a generic Q")
            if looks_replayed(ans, prev_answer):
                failures.append("case3: replayed/continued the previous answer")
            if "rag" not in ans.lower():
                failures.append(f"case3: answer never addresses RAG: {ans[:140]!r}")
            print(f"    project={project!r} rag_note={p.get('rag')!r} "
                  f"replayed={looks_replayed(ans, prev_answer)} chars={len(ans)}")
            print(f"    {ans[:160]}")
            prev_answer = ans
        else:
            failures.append("case3: no answer produced")

        # ── CASE 4: Node.js coding -> fenced code block ────────────────────
        print("\n[4] Node.js coding question (expect a fenced code block)")
        pre, all_ev, secs, submitted = await run_turn(
            ws, ["Write a Node.js function",
                 "that reads a JSON file",
                 "and returns the parsed object"], "case4")
        gen = [e for e in all_ev if e["type"] == "answer.generated"]
        if gen:
            ans = gen[-1]["payload"].get("answer", "")
            has_fence = "```" in ans
            print(f"    fenced_code_block={has_fence} chars={len(ans)}")
            if not has_fence:
                failures.append("case4: Node.js answer had no fenced code block")
            if looks_replayed(ans, prev_answer):
                failures.append("case4: replayed/continued the previous answer")
            prev_answer = ans
        else:
            failures.append("case4: no answer produced")

        # ── CASE 5: follow-up reference resolution ("the token" -> JWT) ────
        print("\n[5] Follow-up: 'the token' must resolve to the earlier JWT topic")
        pre, all_ev, secs, submitted = await run_turn(
            ws, ["How long should the access token expiry be",
                 "and how do you refresh it?"], "case5")
        gen = [e for e in all_ev if e["type"] == "answer.generated"]
        mem = [e for e in all_ev if e["type"] == "memory.updated"]
        if gen:
            ans = gen[-1]["payload"].get("answer", "")
            print(f"    OK: answer={len(ans)} chars, "
                  f"memory_snapshots={len(mem)}")
            if looks_replayed(ans, prev_answer):
                failures.append("case5: replayed/continued the previous answer")
            if "token" not in ans.lower():
                failures.append(f"case5: answer never addresses the token: "
                                f"{ans[:140]!r}")
        else:
            failures.append("case5: no answer produced")

    print("\n" + "=" * 72)
    if failures:
        print("RESULT: FAIL")
        for f in failures:
            print("  -", f)
        return 1
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))