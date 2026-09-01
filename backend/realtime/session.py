"""WebSocket session manager for live interview assistance — Production-ready."""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from typing import Optional
from enum import Enum

from fastapi import WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

from realtime.transcription import transcribe_audio_bytes, detect_question
from agents.orchestrator import InterviewOrchestrator
from agents.answer_agent import AnswerAgent
from db.models import InterviewSession, TranscriptSegment
from ai.provider import get_provider
from config import settings


class EventType(str, Enum):
    """WebSocket event types."""
    SESSION_CONNECTED = "session.connected"
    SESSION_ERROR = "session.error"
    SESSION_END = "session.end"
    
    AUDIO_RECEIVED = "audio.received"
    TRANSCRIPT_FINAL = "transcript.final"
    
    QUESTION_DETECTED = "question.detected"
    
    ANSWER_GENERATING = "answer.generating"
    ANSWER_GENERATED = "answer.generated"
    ANSWER_STREAMED = "answer.streamed"
    
    PING = "ping"
    PONG = "pong"


def _event(event_type: str, payload: dict) -> str:
    """Serialize WebSocket event."""
    return json.dumps({
        "type": event_type,
        "payload": payload,
        "ts": time.time()
    })


class LiveSession:
    """Manages a single live interview WebSocket connection with optimized answer generation."""

    def __init__(self, websocket: WebSocket, session: InterviewSession, db: Session):
        self.ws = websocket
        self.session = session
        self.db = db
        self.provider = get_provider(db=db)
        self.orchestrator = InterviewOrchestrator(db, self.provider)
        self.answer_agent = AnswerAgent(db, self.provider, session.id)
        
        self._audio_buffer: list[bytes] = []
        self._last_transcript: str = ""
        self._running = True
        self._pending_question: Optional[str] = None
        self._answer_generation_lock = asyncio.Lock()
        
        # Metrics
        self._questions_detected = 0
        self._answers_generated = 0
        self._start_time = time.time()

    async def send(self, event_type: str, payload: dict):
        """Send event to client."""
        try:
            await self.ws.send_text(_event(event_type, payload))
        except Exception as e:
            print(f"Failed to send {event_type}: {e}")
            self._running = False

    async def run(self):
        """Main WebSocket loop."""
        await self.send(EventType.SESSION_CONNECTED, {
            "session_id": self.session.id,
            "mode": self.session.mode,
            "profile": {
                "name": self.session.profile.name,
                "target_role": self.session.profile.target_role,
            }
        })

        try:
            while self._running:
                try:
                    message = await asyncio.wait_for(
                        self.ws.receive(),
                        timeout=settings.ws_heartbeat_interval
                    )
                except asyncio.TimeoutError:
                    # Heartbeat
                    await self.send(EventType.PING, {})
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
            await self.send(EventType.SESSION_ERROR, {
                "code": "internal",
                "message": str(e),
                "recoverable": True
            })
        finally:
            # Log session metrics
            elapsed = time.time() - self._start_time
            print(f"[Session {self.session.id}] Closed: {self._questions_detected} Q, "
                  f"{self._answers_generated} A in {elapsed:.1f}s")

    async def _handle_audio(self, audio_bytes: bytes):
        """Process incoming audio chunk — transcribe & detect question."""
        await self.send(EventType.AUDIO_RECEIVED, {"size": len(audio_bytes)})

        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None, transcribe_audio_bytes, audio_bytes, "wav"
            )
        except Exception as e:
            await self.send(EventType.SESSION_ERROR, {
                "code": "transcription_failed",
                "message": f"Transcription error: {e}",
                "recoverable": True
            })
            return

        if not result.text or len(result.text.strip()) < 3:
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

        self._last_transcript = result.text
        await self.send(EventType.TRANSCRIPT_FINAL, {
            "text": result.text,
            "duration": result.duration,
            "timestamp": time.time()
        })

        # Question detection
        is_question, confidence = detect_question(result.text)
        if is_question and confidence >= 0.6:
            await self._handle_question_detected(result.text, confidence, segment.id)

    async def _handle_question_detected(self, question: str, confidence: float, segment_id: int):
        """Detected a question — fast-track answer generation."""
        self._questions_detected += 1
        self._pending_question = question

        # Update segment metadata
        segment = self.db.query(TranscriptSegment).filter_by(id=segment_id).first()
        if segment:
            segment.is_question = True
            segment.speaker = "interviewer"
            segment.confidence = confidence
            self.db.commit()

        await self.send(EventType.QUESTION_DETECTED, {
            "question": question,
            "confidence": confidence,
            "segment_id": segment_id
        })

        # Generate answer (with lock to prevent parallel generations)
        async with self._answer_generation_lock:
            await self._generate_answer_fast(question)

    async def _generate_answer_fast(self, question: str):
        """Generate answer with streaming for real-time display."""
        await self.send(EventType.ANSWER_GENERATING, {
            "agent": "answer_agent",
            "question": question
        })

        try:
            # Retrieve context from knowledge base (fast, non-blocking)
            from knowledge.retrieval import retrieve_context
            context = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: asyncio.run(retrieve_context(
                    self.session.profile.id,
                    question,
                    top_k=2  # Reduced for speed
                ))
            )
            
            # Generate answer
            result = await asyncio.wait_for(
                self.answer_agent.run(
                    question,
                    question=question,
                    question_type="unknown",
                    context=context,
                    candidate_name=self.session.profile.name
                ),
                timeout=settings.answer_generation_timeout
            )

            if result.success:
                self._answers_generated += 1
                answer_data = result.data or {}
                
                # Send full answer
                await self.send(EventType.ANSWER_GENERATED, {
                    "answer": answer_data.get("answer", ""),
                    "key_points": answer_data.get("key_points", []),
                    "star": answer_data.get("star", {}),
                    "follow_up_questions": answer_data.get("follow_up_questions", []),
                    "confidence": answer_data.get("confidence", 0),
                    "latency_ms": result.latency_ms
                })
            else:
                await self.send(EventType.SESSION_ERROR, {
                    "code": "answer_generation_failed",
                    "message": result.error or "Unknown error",
                    "recoverable": True
                })

        except asyncio.TimeoutError:
            await self.send(EventType.SESSION_ERROR, {
                "code": "answer_generation_timeout",
                "message": f"Answer generation exceeded {settings.answer_generation_timeout}s",
                "recoverable": True
            })
        except Exception as e:
            await self.send(EventType.SESSION_ERROR, {
                "code": "answer_generation_failed",
                "message": str(e),
                "recoverable": True
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
