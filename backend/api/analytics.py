"""Analytics routes — real data from database."""
from __future__ import annotations

import json
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import func

from db.database import get_db
from db.models import InterviewSession, InterviewFeedback, InterviewStatus, InterviewQuestion
from api.profile import get_profile

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


@router.get("/dashboard")
def get_dashboard(db: Session = Depends(get_db)):
    """Real dashboard metrics from DB."""
    profile = get_profile(db)
    pid = profile.id

    total = db.query(func.count(InterviewSession.id)).filter_by(profile_id=pid).scalar() or 0
    completed = db.query(func.count(InterviewSession.id)).filter_by(
        profile_id=pid, status=InterviewStatus.COMPLETED
    ).scalar() or 0

    # Average scores from feedback
    feedback_rows = (
        db.query(InterviewFeedback)
        .join(InterviewSession)
        .filter(InterviewSession.profile_id == pid)
        .all()
    )

    avg_overall = None
    avg_technical = None
    avg_communication = None

    if feedback_rows:
        scores_overall = [f.overall_score for f in feedback_rows if f.overall_score is not None]
        scores_tech = [f.technical_score for f in feedback_rows if f.technical_score is not None]
        scores_comm = [f.communication_score for f in feedback_rows if f.communication_score is not None]

        avg_overall = round(sum(scores_overall) / len(scores_overall), 1) if scores_overall else None
        avg_technical = round(sum(scores_tech) / len(scores_tech), 1) if scores_tech else None
        avg_communication = round(sum(scores_comm) / len(scores_comm), 1) if scores_comm else None

    # Recent sessions
    recent = (
        db.query(InterviewSession)
        .filter_by(profile_id=pid)
        .order_by(InterviewSession.created_at.desc())
        .limit(5)
        .all()
    )

    recent_data = []
    for s in recent:
        fb = db.query(InterviewFeedback).filter_by(session_id=s.id).first()
        recent_data.append({
            "id": s.id,
            "title": s.title or f"{s.interview_type.title()} Interview",
            "type": s.interview_type,
            "status": s.status,
            "created_at": s.created_at.isoformat(),
            "overall_score": fb.overall_score if fb else None,
        })

    # Score trend (last 10 completed sessions)
    trend_sessions = (
        db.query(InterviewSession)
        .filter(InterviewSession.profile_id == pid, InterviewSession.status == InterviewStatus.COMPLETED)
        .order_by(InterviewSession.ended_at.asc())
        .limit(10)
        .all()
    )

    score_trend = []
    for s in trend_sessions:
        fb = db.query(InterviewFeedback).filter_by(session_id=s.id).first()
        score_trend.append({
            "date": s.ended_at.isoformat() if s.ended_at else s.created_at.isoformat(),
            "score": fb.overall_score if fb else None,
            "type": s.interview_type,
        })

    return {
        "total_interviews": total,
        "completed_interviews": completed,
        "avg_overall_score": avg_overall,
        "avg_technical_score": avg_technical,
        "avg_communication_score": avg_communication,
        "recent_sessions": recent_data,
        "score_trend": score_trend,
        "profile_name": profile.name,
        "target_role": profile.target_role,
    }


@router.get("/session/{session_id}")
def get_session_analytics(session_id: int, db: Session = Depends(get_db)):
    """Detailed analytics for a single session."""
    profile = get_profile(db)
    session = db.query(InterviewSession).filter_by(id=session_id, profile_id=profile.id).first()
    if not session:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Session not found")

    questions = db.query(InterviewQuestion).filter_by(session_id=session_id).all()
    fb = db.query(InterviewFeedback).filter_by(session_id=session_id).first()

    qa_data = []
    for q in questions:
        r = q.response
        qa_data.append({
            "question": q.content,
            "question_type": q.question_type,
            "answer": r.content if r else None,
            "word_count": r.word_count if r else 0,
            "method": r.method if r else None,
        })

    return {
        "session_id": session_id,
        "interview_type": session.interview_type,
        "status": session.status,
        "duration_minutes": (
            round((session.ended_at - session.started_at).total_seconds() / 60, 1)
            if session.ended_at and session.started_at else None
        ),
        "questions": qa_data,
        "feedback": {
            "overall_score": fb.overall_score if fb else None,
            "technical_score": fb.technical_score if fb else None,
            "communication_score": fb.communication_score if fb else None,
            "confidence_score": fb.confidence_score if fb else None,
            "relevance_score": fb.relevance_score if fb else None,
            "strengths": json.loads(fb.strengths) if fb and fb.strengths else [],
            "weaknesses": json.loads(fb.weaknesses) if fb and fb.weaknesses else [],
            "improvement_plan": fb.improvement_plan if fb else None,
            "recommended_topics": json.loads(fb.recommended_topics) if fb and fb.recommended_topics else [],
        } if fb else None,
    }
