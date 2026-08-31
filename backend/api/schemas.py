"""Shared Pydantic schemas used across API routes."""
from __future__ import annotations

from datetime import datetime
from typing import Optional, Any
from pydantic import BaseModel, ConfigDict


class OKResponse(BaseModel):
    ok: bool = True
    message: str = "Success"


# ── Profile ────────────────────────────────────────────────────────────────

class ProfileCreate(BaseModel):
    name: str
    target_role: Optional[str] = None
    experience_level: Optional[str] = None
    years_of_experience: Optional[float] = None
    preferred_technologies: Optional[list[str]] = None
    summary: Optional[str] = None


class ProfileUpdate(BaseModel):
    name: Optional[str] = None
    target_role: Optional[str] = None
    experience_level: Optional[str] = None
    years_of_experience: Optional[float] = None
    preferred_technologies: Optional[list[str]] = None
    summary: Optional[str] = None


class ProfileResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    user_id: int
    name: str
    target_role: Optional[str]
    experience_level: Optional[str]
    years_of_experience: Optional[float]
    preferred_technologies: Optional[str]
    summary: Optional[str]
    created_at: datetime
    updated_at: datetime


# ── Resume ─────────────────────────────────────────────────────────────────

class ResumeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    profile_id: int
    filename: str
    parsed_data: Optional[dict]
    is_active: bool
    created_at: datetime


class ResumeSectionUpdate(BaseModel):
    content: str


# ── Job Description ────────────────────────────────────────────────────────

class JobDescriptionCreate(BaseModel):
    title: str
    company: Optional[str] = None
    raw_text: str


class JobDescriptionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    profile_id: int
    title: str
    company: Optional[str]
    raw_text: str
    parsed_data: Optional[dict]
    competency_map: Optional[dict]
    is_active: bool
    created_at: datetime


# ── Interview Session ──────────────────────────────────────────────────────

class InterviewCreate(BaseModel):
    interview_type: str = "mixed"
    mode: str = "practice"
    difficulty: str = "medium"
    target_role: Optional[str] = None
    target_tech: Optional[list[str]] = None
    question_count: int = 5
    job_description_id: Optional[int] = None
    title: Optional[str] = None


class InterviewResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    profile_id: int
    interview_type: str
    mode: str
    difficulty: Optional[str]
    target_role: Optional[str]
    question_count: int
    status: str
    title: Optional[str]
    started_at: Optional[datetime]
    ended_at: Optional[datetime]
    created_at: datetime


class QuestionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    session_id: int
    content: str
    question_type: str
    order_index: int
    is_follow_up: bool
    ai_classification: Optional[dict]
    asked_at: datetime


class AnswerSubmit(BaseModel):
    content: str
    method: str = "text"


class AnswerResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    question_id: int
    content: str
    method: str
    word_count: Optional[int]
    answered_at: datetime


class FeedbackResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    session_id: int
    overall_score: Optional[float]
    technical_score: Optional[float]
    communication_score: Optional[float]
    confidence_score: Optional[float]
    relevance_score: Optional[float]
    strengths: Optional[str]
    weaknesses: Optional[str]
    missed_opportunities: Optional[str]
    recommended_topics: Optional[str]
    improvement_plan: Optional[str]
    raw_analysis: Optional[dict]
    created_at: datetime


# ── Settings / Models ──────────────────────────────────────────────────────

class ModelConfigUpdate(BaseModel):
    provider: Optional[str] = None
    model_name: Optional[str] = None
    embedding_model: Optional[str] = None
    transcription_model: Optional[str] = None
    provider_url: Optional[str] = None
    api_key_env: Optional[str] = None


class ModelConfigResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    provider: str
    model_name: str
    embedding_model: str
    transcription_model: str
    provider_url: Optional[str]
    is_active: bool


class SettingUpdate(BaseModel):
    value: str


class AnswerSuggestionRequest(BaseModel):
    question: str
    question_type: str = "unknown"
    session_id: Optional[int] = None


class AnswerSuggestionResponse(BaseModel):
    answer: Optional[str] = None
    key_points: Optional[list[str]] = None
    star: Optional[dict] = None
    follow_up_questions: Optional[list[str]] = None
    confidence: Optional[float] = None
    missing_context: Optional[str] = None
    model_used: str = ""
