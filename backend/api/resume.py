"""Resume upload and management routes."""
from __future__ import annotations

import json
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, BackgroundTasks
from sqlalchemy.orm import Session

from db.database import get_db
from db.models import Resume
from api.schemas import ResumeResponse, OKResponse
from api.profile import get_profile
from services.document_parser import validate_file, extract_text, save_upload
from agents.resume_agent import ResumeAgent
from ai.provider import get_provider
from knowledge.ingestion import ingest_document

router = APIRouter(prefix="/api/resume", tags=["resume"])


async def _process_resume(resume_id: int, profile_id: int):
    """Background task: run AI parsing and knowledge ingestion."""
    from db.database import SessionLocal
    db = SessionLocal()
    try:
        resume = db.query(Resume).filter_by(id=resume_id).first()
        profile = resume.profile if resume else None
        if not resume or not profile:
            return

        agent = ResumeAgent(db, get_provider(), session_id=None)
        await agent.run("parse_resume", resume=resume, profile=profile)

        # Ingest into knowledge base
        await ingest_document(
            db=db,
            profile_id=profile_id,
            source_type="resume",
            source_id=resume.id,
            title=f"Resume: {resume.filename}",
            content=resume.raw_text or "",
            metadata={"filename": resume.filename},
        )
    finally:
        db.close()


@router.get("", response_model=list[ResumeResponse])
def list_resumes(db: Session = Depends(get_db)):
    profile = get_profile(db)
    return db.query(Resume).filter_by(profile_id=profile.id).order_by(Resume.created_at.desc()).all()


@router.get("/{resume_id}", response_model=ResumeResponse)
def get_resume(resume_id: int, db: Session = Depends(get_db)):
    profile = get_profile(db)
    resume = db.query(Resume).filter_by(id=resume_id, profile_id=profile.id).first()
    if not resume:
        raise HTTPException(status_code=404, detail="Resume not found")
    return resume


@router.post("", response_model=ResumeResponse)
async def upload_resume(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    profile = get_profile(db)
    content = await file.read()

    try:
        safe_name = validate_file(file.filename or "resume.pdf", content)
        text = extract_text(file.filename or "resume.pdf", content)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    file_path = await save_upload(safe_name, content)

    # Deactivate old resumes
    db.query(Resume).filter_by(profile_id=profile.id, is_active=True).update({"is_active": False})

    resume = Resume(
        profile_id=profile.id,
        filename=safe_name,
        file_path=file_path,
        raw_text=text,
        is_active=True,
    )
    db.add(resume)
    db.commit()
    db.refresh(resume)

    # AI parsing runs in background
    background_tasks.add_task(_process_resume, resume.id, profile.id)

    return resume


@router.delete("/{resume_id}", response_model=OKResponse)
def delete_resume(resume_id: int, db: Session = Depends(get_db)):
    profile = get_profile(db)
    resume = db.query(Resume).filter_by(id=resume_id, profile_id=profile.id).first()
    if not resume:
        raise HTTPException(status_code=404, detail="Resume not found")
    db.delete(resume)
    db.commit()
    return OKResponse(message="Resume deleted")
