"""Interview session routes — practice and live modes."""
from __future__ import annotations

import json
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

from db.database import get_db
from db.models import InterviewSession, InterviewQuestion, InterviewResponse as IRModel, InterviewStatus
from api.schemas import (
    InterviewCreate, InterviewResponse, QuestionResponse,
    AnswerSubmit, AnswerResponse, FeedbackResponse, OKResponse,
    AnswerSuggestionRequest, AnswerSuggestionResponse,
)
from api.profile import get_profile
from agents.orchestrator import InterviewOrchestrator
from ai.provider import get_provider
from realtime.session import LiveSession

router = APIRouter(prefix="/api/interviews", tags=["interviews"])


@router.get("", response_model=list[InterviewResponse])
def list_sessions(db: Session = Depends(get_db)):
    profile = get_profile(db)
    sessions = (
        db.query(InterviewSession)
        .filter_by(profile_id=profile.id)
        .order_by(InterviewSession.created_at.desc())
        .all()
    )
    return sessions


@router.get("/{session_id}", response_model=InterviewResponse)
def get_session(session_id: int, db: Session = Depends(get_db)):
    profile = get_profile(db)
    session = db.query(InterviewSession).filter_by(id=session_id, profile_id=profile.id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


@router.post("", response_model=InterviewResponse)
def create_session(data: InterviewCreate, db: Session = Depends(get_db)):
    profile = get_profile(db)
    session = InterviewSession(
        profile_id=profile.id,
        job_description_id=data.job_description_id,
        interview_type=data.interview_type,
        mode=data.mode,
        difficulty=data.difficulty,
        target_role=data.target_role or profile.target_role,
        target_tech=json.dumps(data.target_tech) if data.target_tech else None,
        question_count=max(1, min(data.question_count, 20)),
        title=data.title,
        status=InterviewStatus.PENDING,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


@router.post("/{session_id}/start", response_model=QuestionResponse)
async def start_session(session_id: int, db: Session = Depends(get_db)):
    """Start interview: updates status and generates first question."""
    profile = get_profile(db)
    session = db.query(InterviewSession).filter_by(id=session_id, profile_id=profile.id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.status == InterviewStatus.COMPLETED:
        raise HTTPException(status_code=400, detail="Session already completed")

    session.status = InterviewStatus.IN_PROGRESS
    session.started_at = datetime.utcnow()
    db.commit()

    orchestrator = InterviewOrchestrator(db, get_provider(db=db))
    question_text = await orchestrator.generate_next_question(session)
    if not question_text:
        raise HTTPException(status_code=500, detail="Failed to generate question")

    question = db.query(InterviewQuestion).filter_by(session_id=session_id).order_by(
        InterviewQuestion.order_index.desc()
    ).first()
    return question


@router.get("/{session_id}/questions", response_model=list[QuestionResponse])
def get_questions(session_id: int, db: Session = Depends(get_db)):
    profile = get_profile(db)
    session = db.query(InterviewSession).filter_by(id=session_id, profile_id=profile.id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return db.query(InterviewQuestion).filter_by(session_id=session_id).order_by(InterviewQuestion.order_index).all()


@router.post("/{session_id}/questions/{question_id}/answer", response_model=dict)
async def submit_answer(
    session_id: int,
    question_id: int,
    data: AnswerSubmit,
    db: Session = Depends(get_db),
):
    """Submit candidate answer and get next question (or complete interview)."""
    profile = get_profile(db)
    session = db.query(InterviewSession).filter_by(id=session_id, profile_id=profile.id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    orchestrator = InterviewOrchestrator(db, get_provider(db=db))
    response = await orchestrator.process_answer(session, question_id, data.content, data.method)

    # Count actual answers (InterviewResponse) for this session
    answered_count = (
        db.query(IRModel)
        .join(InterviewQuestion, IRModel.question_id == InterviewQuestion.id)
        .filter(InterviewQuestion.session_id == session_id)
        .count()
    )
    is_complete = answered_count >= session.question_count

    next_question = None
    if not is_complete:
        next_question_text = await orchestrator.generate_next_question(session, data.content)
        if next_question_text:
            next_question = db.query(InterviewQuestion).filter_by(session_id=session_id).order_by(
                InterviewQuestion.order_index.desc()
            ).first()
    else:
        # Mark session complete and set ended_at
        session.status = InterviewStatus.COMPLETED
        session.ended_at = datetime.utcnow()
        db.commit()

    return {
        "response_id": response.id,
        "is_complete": is_complete,
        "next_question": QuestionResponse.model_validate(next_question) if next_question else None,
    }


@router.post("/{session_id}/complete", response_model=dict)
async def complete_session(session_id: int, db: Session = Depends(get_db)):
    """Mark session complete and generate feedback."""
    profile = get_profile(db)
    session = db.query(InterviewSession).filter_by(id=session_id, profile_id=profile.id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    orchestrator = InterviewOrchestrator(db, get_provider(db=db))
    feedback = await orchestrator.complete_interview(session)
    return {"feedback": feedback, "session_id": session_id}


@router.get("/{session_id}/feedback", response_model=FeedbackResponse)
def get_feedback(session_id: int, db: Session = Depends(get_db)):
    from db.models import InterviewFeedback
    profile = get_profile(db)
    session = db.query(InterviewSession).filter_by(id=session_id, profile_id=profile.id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    fb = db.query(InterviewFeedback).filter_by(session_id=session_id).first()
    if not fb:
        raise HTTPException(status_code=404, detail="Feedback not yet generated")
    return fb


@router.delete("/{session_id}", response_model=OKResponse)
def delete_session(session_id: int, db: Session = Depends(get_db)):
    profile = get_profile(db)
    session = db.query(InterviewSession).filter_by(id=session_id, profile_id=profile.id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    db.delete(session)
    db.commit()
    return OKResponse(message="Session deleted")


@router.post("/suggest-answer", response_model=AnswerSuggestionResponse)
async def suggest_answer(data: AnswerSuggestionRequest, db: Session = Depends(get_db)):
    """Generate an AI answer suggestion for any question (standalone endpoint)."""
    profile = get_profile(db)
    session = None
    if data.session_id:
        session = db.query(InterviewSession).filter_by(id=data.session_id, profile_id=profile.id).first()

    orchestrator = InterviewOrchestrator(db, get_provider(db=db))
    if session:
        answer_data = await orchestrator.generate_answer_suggestion(
            session, data.question, data.question_type
        )
        # model_used read from provider
        provider = get_provider(db=db)
        model_used = getattr(provider, "model_name", "default")
    else:
        # No session — still generate using profile context
        from knowledge.retrieval import retrieve_context
        from agents.answer_agent import AnswerAgent
        context = await retrieve_context(profile.id, data.question, top_k=3)
        provider = get_provider(db=db)
        agent = AnswerAgent(db, provider)
        result = await agent.run(
            data.question,
            question=data.question,
            question_type=data.question_type,
            context=context,
            candidate_name=profile.name,
        )
        if not result.success:
            raise HTTPException(status_code=500, detail=f"Answer generation failed: {result.error}")
        answer_data = result.data or {}
        model_used = str(result.model_used) if result.model_used and isinstance(result.model_used, str) else str(getattr(provider, "model_name", "default"))

    return AnswerSuggestionResponse(
        answer=answer_data.get("answer"),
        key_points=answer_data.get("key_points"),
        star=answer_data.get("star"),
        follow_up_questions=answer_data.get("follow_up_questions"),
        confidence=answer_data.get("confidence"),
        missing_context=answer_data.get("missing_context"),
        model_used=model_used,
    )


# ── WebSocket ──────────────────────────────────────────────────────────

@router.websocket("/{session_id}/ws")
async def interview_websocket(session_id: int, websocket: WebSocket, db: Session = Depends(get_db)):
    """WebSocket endpoint for live interview assistance."""
    await websocket.accept()

    try:
        profile = get_profile(db)
    except HTTPException:
        await websocket.close(code=4001, reason="No profile found")
        return

    session = db.query(InterviewSession).filter_by(id=session_id, profile_id=profile.id).first()
    if not session:
        await websocket.close(code=4004, reason="Session not found")
        return

    if session.status == InterviewStatus.PENDING:
        session.status = InterviewStatus.IN_PROGRESS
        session.started_at = datetime.utcnow()
        db.commit()

    live = LiveSession(websocket, session, db)
    await live.run()
