"""Profile and user management routes."""
from __future__ import annotations

import json
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from db.database import get_db
from db.models import User, CandidateProfile
from api.schemas import ProfileCreate, ProfileUpdate, ProfileResponse, OKResponse

router = APIRouter(prefix="/api/profile", tags=["profile"])


def get_or_create_user(db: Session) -> User:
    """Local-first: always use the single local user."""
    user = db.query(User).filter_by(username="local").first()
    if not user:
        user = User(username="local")
        db.add(user)
        db.commit()
        db.refresh(user)
    return user


def get_profile(db: Session) -> CandidateProfile:
    user = get_or_create_user(db)
    profile = db.query(CandidateProfile).filter_by(user_id=user.id).first()
    if not profile:
        profile = CandidateProfile(
            user_id=user.id,
            name="Candidate",
            target_role="Software Engineer",
            experience_level="mid",
            years_of_experience=3.0,
        )
        db.add(profile)
        db.commit()
        db.refresh(profile)
    return profile


@router.get("", response_model=ProfileResponse | None)
def read_profile(db: Session = Depends(get_db)):
    """Get the current profile. Returns null if not set up yet."""
    user = get_or_create_user(db)
    profile = db.query(CandidateProfile).filter_by(user_id=user.id).first()
    return profile


@router.post("", response_model=ProfileResponse)
def create_profile(data: ProfileCreate, db: Session = Depends(get_db)):
    user = get_or_create_user(db)
    existing = db.query(CandidateProfile).filter_by(user_id=user.id).first()
    if existing:
        raise HTTPException(status_code=409, detail="Profile already exists. Use PATCH to update.")

    profile = CandidateProfile(
        user_id=user.id,
        name=data.name,
        target_role=data.target_role,
        experience_level=data.experience_level,
        years_of_experience=data.years_of_experience,
        preferred_technologies=json.dumps(data.preferred_technologies) if data.preferred_technologies else None,
        summary=data.summary,
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


@router.patch("", response_model=ProfileResponse)
def update_profile(data: ProfileUpdate, db: Session = Depends(get_db)):
    profile = get_profile(db)
    updates = data.model_dump(exclude_unset=True)
    for field, value in updates.items():
        if field == "preferred_technologies" and isinstance(value, list):
            value = json.dumps(value)
        setattr(profile, field, value)
    db.commit()
    db.refresh(profile)
    return profile
