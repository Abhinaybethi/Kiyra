"""WebSocket session manager for live interview assistance."""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from typing import Optional

from fastapi import WebSocket, WebSocketDisconnect

from realtime.transcription import transcribe_audio_bytes, detect_question
from agents.orchestrator import InterviewOrchestrator
from db.models import InterviewSession, TranscriptSegment
from config import settings


def _event(event_type: str, payload: dict) -> str:
    return json.dumps({"type": event_type, "payload": payload, "ts": time.time()})


class LiveSession:
    """Manages a single live interview WebSocket connection."""

    def __init__(self, websocket: WebSocket, session: InterviewSession, db):
        self.ws = websocket
        self.session = session
        self.db = db
        self.orchestrator = InterviewOrchestrator(db)
        self._audio_buffer: list[bytes] = []
        self._last_transcript: str = ""
        self._running = True

    async def send(self, event_type: str, payload: dict):
        try:
            await self.ws.send_text(_event(event_type, payload))
        except Exception:
            self._running = False

    async def run(self):
        """Main WebSocket loop."""
        await self.send("session.connected", {
            "session_id": self.session.id,
            "mode": self.session.mode,
        })

        try:
            while self._running:
                try:
                    message = await asyncio.wait_for(self.ws.receive(), timeout=settings.ws_heartbeat_interval)
                except asyncio.TimeoutError:
                    # Heartbeat
                    await self.send("ping", {})
                    continue

                if message["type"] == "websocket.disconnect":
                    break

                if message["type"] == "websocket.receive":
                    if "bytes" in message and message["bytes"]:
                        await self._handle_audio(message["bytes"])
                    elif "text" in message and message["text"]:
                        await self._handle_text_message(message["text"])

        except WebSocketDisconnect:
            pass
        except Exception as e:
            await self.send("session.error", {"code": "internal", "message": str(e), "recoverable": True})

    async def _handle_audio(self, audio_bytes: bytes):
        """Process incoming audio chunk."""
        await self.send("audio.received", {"size": len(audio_bytes)})

        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None, transcribe_audio_bytes, audio_bytes, "wav"
            )
        except Exception as e:
            await self.send("session.error", {
                "code": "transcription_failed",
                "message": f"Transcription error: {e}",
                "recoverable": True,
            })
            return

        if not result.text:
            return

        # Store transcript segment
        segment = TranscriptSegment(
            session_id=self.session.id,
            speaker="unknown",
            content=result.text,
            timestamp_ms=int(time.time() * 1000),
        )
        self.db.add(segment)
        self.db.commit()

        await self.send("transcript.final", {
            "text": result.text,
            "duration": result.duration,
            "timestamp": time.time(),
        })

        # Question detection
        is_question, confidence = detect_question(result.text)
        if is_question:
            await self._handle_question_detected(result.text, confidence, segment.id)

    async def _handle_question_detected(self, question: str, confidence: float, segment_id: int):
        """Detected a question — generate answer suggestion."""
        if segment_id == -1:
            segment = TranscriptSegment(
                session_id=self.session.id,
                speaker="interviewer",
                content=question,
                is_question=True,
                confidence=confidence,
                timestamp_ms=int(time.time() * 1000),
            )
            self.db.add(segment)
            self.db.commit()
            segment_id = segment.id
        else:
            segment = self.db.query(TranscriptSegment).filter_by(id=segment_id).first()
            if segment:
                segment.is_question = True
                segment.speaker = "interviewer"
                segment.confidence = confidence
                self.db.commit()

        await self.send("question.detected", {
            "question": question,
            "confidence": confidence,
            "segment_id": segment_id,
        })

        await self.send("answer.generating", {"agent": "answer_agent"})

        try:
            answer_data = await self.orchestrator.generate_answer_suggestion(
                self.session, question, question_type="unknown"
            )
            await self.send("answer.generated", answer_data)
        except Exception as e:
            await self.send("session.error", {
                "code": "answer_generation_failed",
                "message": str(e),
                "recoverable": True,
            })

    async def _handle_text_message(self, text: str):
        """Handle text control messages from frontend."""
        try:
            msg = json.loads(text)
        except json.JSONDecodeError:
            return

        msg_type = msg.get("type")

        if msg_type == "generate_answer":
            # Manual trigger
            question = msg.get("payload", {}).get("question", "")
            if question:
                await self._handle_question_detected(question, 1.0, -1)

        elif msg_type == "transcript.text":
            # Text-based transcript input
            content = msg.get("payload", {}).get("text", "")
            if content:
                segment = TranscriptSegment(
                    session_id=self.session.id,
                    speaker=msg.get("payload", {}).get("speaker", "unknown"),
                    content=content,
                    timestamp_ms=int(time.time() * 1000),
                )
                self.db.add(segment)
                self.db.commit()

                is_question, confidence = detect_question(content)
                if is_question or msg.get("payload", {}).get("force_answer"):
                    await self._handle_question_detected(content, confidence, segment.id)

        elif msg_type == "pong":
            pass  # heartbeat response

        elif msg_type == "session.end":
            self._running = False
