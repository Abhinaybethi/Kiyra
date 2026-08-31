"""Job description management routes."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session

from db.database import get_db
from db.models import JobDescription
from api.schemas import JobDescriptionCreate, JobDescriptionResponse, OKResponse
from api.profile import get_profile
from agents.jd_agent import JobDescriptionAgent
from ai.provider import get_provider
from knowledge.ingestion import ingest_document

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


async def _process_jd(jd_id: int, profile_id: int):
    from db.database import SessionLocal
    db = SessionLocal()
    try:
        jd = db.query(JobDescription).filter_by(id=jd_id).first()
        if not jd:
            return
        agent = JobDescriptionAgent(db, get_provider())
        await agent.run("analyze_jd", job_description=jd)

        await ingest_document(
            db=db,
            profile_id=profile_id,
            source_type="job_description",
            source_id=jd.id,
            title=f"JD: {jd.title} at {jd.company or 'Company'}",
            content=jd.raw_text,
            metadata={"title": jd.title, "company": jd.company},
        )
    finally:
        db.close()


@router.get("", response_model=list[JobDescriptionResponse])
def list_jobs(db: Session = Depends(get_db)):
    profile = get_profile(db)
    return db.query(JobDescription).filter_by(profile_id=profile.id).order_by(JobDescription.created_at.desc()).all()


@router.get("/{jd_id}", response_model=JobDescriptionResponse)
def get_job(jd_id: int, db: Session = Depends(get_db)):
    profile = get_profile(db)
    jd = db.query(JobDescription).filter_by(id=jd_id, profile_id=profile.id).first()
    if not jd:
        raise HTTPException(status_code=404, detail="Job description not found")
    return jd


@router.post("", response_model=JobDescriptionResponse)
async def create_job(
    data: JobDescriptionCreate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    profile = get_profile(db)

    # Deactivate old active JDs
    db.query(JobDescription).filter_by(profile_id=profile.id, is_active=True).update({"is_active": False})

    jd = JobDescription(
        profile_id=profile.id,
        title=data.title,
        company=data.company,
        raw_text=data.raw_text,
        is_active=True,
    )
    db.add(jd)
    db.commit()
    db.refresh(jd)

    background_tasks.add_task(_process_jd, jd.id, profile.id)
    return jd


@router.delete("/{jd_id}", response_model=OKResponse)
def delete_job(jd_id: int, db: Session = Depends(get_db)):
    profile = get_profile(db)
    jd = db.query(JobDescription).filter_by(id=jd_id, profile_id=profile.id).first()
    if not jd:
        raise HTTPException(status_code=404, detail="Job description not found")
    db.delete(jd)
    db.commit()
    return OKResponse(message="Job description deleted")
