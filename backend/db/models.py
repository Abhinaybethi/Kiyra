"""All SQLAlchemy ORM models."""
from datetime import datetime
from typing import Optional
import json

from sqlalchemy import (
    Integer, String, Text, Float, Boolean, DateTime, ForeignKey,
    JSON, Index, Enum as SAEnum
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
import enum

from .database import Base


class InterviewType(str, enum.Enum):
    HR = "hr"
    BEHAVIORAL = "behavioral"
    TECHNICAL = "technical"
    CODING = "coding"
    SYSTEM_DESIGN = "system_design"
    MIXED = "mixed"


class InterviewStatus(str, enum.Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    ABANDONED = "abandoned"


class SkillProficiency(str, enum.Enum):
    BEGINNER = "beginner"
    INTERMEDIATE = "intermediate"
    ADVANCED = "advanced"
    EXPERT = "expert"


class QuestionType(str, enum.Enum):
    HR = "hr"
    BEHAVIORAL = "behavioral"
    TECHNICAL = "technical"
    CODING = "coding"
    SYSTEM_DESIGN = "system_design"
    FOLLOW_UP = "follow_up"
    UNKNOWN = "unknown"


class ResponseMethod(str, enum.Enum):
    VOICE = "voice"
    TEXT = "text"


class AgentStatus(str, enum.Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class AIProviderType(str, enum.Enum):
    OLLAMA = "ollama"
    OPENAI_COMPATIBLE = "openai_compatible"


# ── User & Profile ─────────────────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(50), unique=True, default="local")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    profile: Mapped[Optional["CandidateProfile"]] = relationship(back_populates="user", uselist=False)


class CandidateProfile(Base):
    __tablename__ = "candidate_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    target_role: Mapped[Optional[str]] = mapped_column(String(200))
    experience_level: Mapped[Optional[str]] = mapped_column(String(50))  # junior/mid/senior/lead
    years_of_experience: Mapped[Optional[float]] = mapped_column(Float)
    preferred_technologies: Mapped[Optional[str]] = mapped_column(Text)  # JSON array
    summary: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user: Mapped["User"] = relationship(back_populates="profile")
    resumes: Mapped[list["Resume"]] = relationship(back_populates="profile")
    skills: Mapped[list["Skill"]] = relationship(back_populates="profile")
    projects: Mapped[list["Project"]] = relationship(back_populates="profile")
    job_descriptions: Mapped[list["JobDescription"]] = relationship(back_populates="profile")
    interview_sessions: Mapped[list["InterviewSession"]] = relationship(back_populates="profile")


# ── Resume ─────────────────────────────────────────────────────────────────

class Resume(Base):
    __tablename__ = "resumes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("candidate_profiles.id"))
    filename: Mapped[str] = mapped_column(String(255))
    file_path: Mapped[Optional[str]] = mapped_column(String(500))
    raw_text: Mapped[Optional[str]] = mapped_column(Text)
    parsed_data: Mapped[Optional[dict]] = mapped_column(JSON)  # structured extraction
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    profile: Mapped["CandidateProfile"] = relationship(back_populates="resumes")
    sections: Mapped[list["ResumeSection"]] = relationship(back_populates="resume", cascade="all, delete-orphan")


class ResumeSection(Base):
    __tablename__ = "resume_sections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    resume_id: Mapped[int] = mapped_column(ForeignKey("resumes.id"))
    section_type: Mapped[str] = mapped_column(String(50))  # experience, education, skills, projects, etc.
    title: Mapped[Optional[str]] = mapped_column(String(200))
    content: Mapped[str] = mapped_column(Text)
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    meta_data: Mapped[Optional[dict]] = mapped_column("metadata", JSON)

    resume: Mapped["Resume"] = relationship(back_populates="sections")


# ── Job Description ────────────────────────────────────────────────────────

class JobDescription(Base):
    __tablename__ = "job_descriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("candidate_profiles.id"))
    title: Mapped[str] = mapped_column(String(200))
    company: Mapped[Optional[str]] = mapped_column(String(200))
    raw_text: Mapped[str] = mapped_column(Text)
    parsed_data: Mapped[Optional[dict]] = mapped_column(JSON)  # required_skills, responsibilities, etc.
    competency_map: Mapped[Optional[dict]] = mapped_column(JSON)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    profile: Mapped["CandidateProfile"] = relationship(back_populates="job_descriptions")
    interview_sessions: Mapped[list["InterviewSession"]] = relationship(back_populates="job_description")


# ── Skills & Projects ──────────────────────────────────────────────────────

class Skill(Base):
    __tablename__ = "skills"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("candidate_profiles.id"))
    name: Mapped[str] = mapped_column(String(100))
    category: Mapped[Optional[str]] = mapped_column(String(50))  # language, framework, tool, etc.
    proficiency: Mapped[Optional[str]] = mapped_column(String(20))
    years_used: Mapped[Optional[float]] = mapped_column(Float)

    profile: Mapped["CandidateProfile"] = relationship(back_populates="skills")

    __table_args__ = (Index("ix_skills_profile", "profile_id"),)


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("candidate_profiles.id"))
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[Optional[str]] = mapped_column(Text)
    technologies: Mapped[Optional[str]] = mapped_column(Text)  # JSON array
    role: Mapped[Optional[str]] = mapped_column(String(100))
    outcomes: Mapped[Optional[str]] = mapped_column(Text)
    url: Mapped[Optional[str]] = mapped_column(String(500))
    start_date: Mapped[Optional[str]] = mapped_column(String(20))
    end_date: Mapped[Optional[str]] = mapped_column(String(20))

    profile: Mapped["CandidateProfile"] = relationship(back_populates="projects")


# ── Knowledge Base ─────────────────────────────────────────────────────────

class KnowledgeDocument(Base):
    __tablename__ = "knowledge_documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("candidate_profiles.id"))
    source_type: Mapped[str] = mapped_column(String(50))  # resume, job_description, project, manual
    source_id: Mapped[Optional[int]] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(300))
    content: Mapped[str] = mapped_column(Text)
    meta_data: Mapped[Optional[dict]] = mapped_column("metadata", JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    chunks: Mapped[list["KnowledgeChunk"]] = relationship(back_populates="document", cascade="all, delete-orphan")

    __table_args__ = (Index("ix_knowledge_profile", "profile_id"),)


class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("knowledge_documents.id"))
    content: Mapped[str] = mapped_column(Text)
    chunk_index: Mapped[int] = mapped_column(Integer)
    embedding_id: Mapped[Optional[str]] = mapped_column(String(100))  # ChromaDB doc id
    meta_data: Mapped[Optional[dict]] = mapped_column("metadata", JSON)

    document: Mapped["KnowledgeDocument"] = relationship(back_populates="chunks")


# ── Interview Session ──────────────────────────────────────────────────────

class InterviewSession(Base):
    __tablename__ = "interview_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("candidate_profiles.id"))
    job_description_id: Mapped[Optional[int]] = mapped_column(ForeignKey("job_descriptions.id"))
    title: Mapped[Optional[str]] = mapped_column(String(200))
    interview_type: Mapped[str] = mapped_column(String(30), default=InterviewType.MIXED)
    mode: Mapped[str] = mapped_column(String(20), default="practice")  # practice | live
    difficulty: Mapped[Optional[str]] = mapped_column(String(20))  # easy, medium, hard
    target_role: Mapped[Optional[str]] = mapped_column(String(200))
    target_tech: Mapped[Optional[str]] = mapped_column(Text)  # JSON array
    question_count: Mapped[int] = mapped_column(Integer, default=5)
    status: Mapped[str] = mapped_column(String(20), default=InterviewStatus.PENDING)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    profile: Mapped["CandidateProfile"] = relationship(back_populates="interview_sessions")
    job_description: Mapped[Optional["JobDescription"]] = relationship(back_populates="interview_sessions")
    questions: Mapped[list["InterviewQuestion"]] = relationship(back_populates="session", cascade="all, delete-orphan")
    transcript_segments: Mapped[list["TranscriptSegment"]] = relationship(back_populates="session", cascade="all, delete-orphan")
    feedback: Mapped[Optional["InterviewFeedback"]] = relationship(back_populates="session", uselist=False, cascade="all, delete-orphan")
    agent_runs: Mapped[list["AgentRun"]] = relationship(back_populates="session", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_sessions_profile", "profile_id"),
        Index("ix_sessions_status", "status"),
    )


class InterviewQuestion(Base):
    __tablename__ = "interview_questions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("interview_sessions.id"))
    content: Mapped[str] = mapped_column(Text)
    question_type: Mapped[str] = mapped_column(String(30), default=QuestionType.UNKNOWN)
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    is_follow_up: Mapped[bool] = mapped_column(Boolean, default=False)
    parent_question_id: Mapped[Optional[int]] = mapped_column(ForeignKey("interview_questions.id"))
    ai_classification: Mapped[Optional[dict]] = mapped_column(JSON)
    asked_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    session: Mapped["InterviewSession"] = relationship(back_populates="questions")
    response: Mapped[Optional["InterviewResponse"]] = relationship(back_populates="question", uselist=False)

    __table_args__ = (Index("ix_questions_session", "session_id"),)


class InterviewResponse(Base):
    __tablename__ = "interview_responses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    question_id: Mapped[int] = mapped_column(ForeignKey("interview_questions.id"), unique=True)
    content: Mapped[str] = mapped_column(Text)
    method: Mapped[str] = mapped_column(String(10), default=ResponseMethod.TEXT)
    ai_suggestion: Mapped[Optional[str]] = mapped_column(Text)
    ai_suggestion_data: Mapped[Optional[dict]] = mapped_column(JSON)  # structured points, star, etc.
    duration_seconds: Mapped[Optional[float]] = mapped_column(Float)
    word_count: Mapped[Optional[int]] = mapped_column(Integer)
    answered_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    question: Mapped["InterviewQuestion"] = relationship(back_populates="response")


class TranscriptSegment(Base):
    __tablename__ = "transcript_segments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("interview_sessions.id"))
    speaker: Mapped[str] = mapped_column(String(20), default="unknown")  # interviewer, candidate, unknown
    content: Mapped[str] = mapped_column(Text)
    timestamp_ms: Mapped[Optional[int]] = mapped_column(Integer)
    is_question: Mapped[bool] = mapped_column(Boolean, default=False)
    confidence: Mapped[Optional[float]] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    session: Mapped["InterviewSession"] = relationship(back_populates="transcript_segments")

    __table_args__ = (Index("ix_transcript_session", "session_id"),)


class InterviewFeedback(Base):
    __tablename__ = "interview_feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("interview_sessions.id"), unique=True)
    overall_score: Mapped[Optional[float]] = mapped_column(Float)
    technical_score: Mapped[Optional[float]] = mapped_column(Float)
    communication_score: Mapped[Optional[float]] = mapped_column(Float)
    confidence_score: Mapped[Optional[float]] = mapped_column(Float)
    relevance_score: Mapped[Optional[float]] = mapped_column(Float)
    strengths: Mapped[Optional[str]] = mapped_column(Text)  # JSON array
    weaknesses: Mapped[Optional[str]] = mapped_column(Text)  # JSON array
    missed_opportunities: Mapped[Optional[str]] = mapped_column(Text)
    recommended_topics: Mapped[Optional[str]] = mapped_column(Text)  # JSON array
    improvement_plan: Mapped[Optional[str]] = mapped_column(Text)
    raw_analysis: Mapped[Optional[dict]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    session: Mapped["InterviewSession"] = relationship(back_populates="feedback")


# ── Agent Runs ─────────────────────────────────────────────────────────────

class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[Optional[int]] = mapped_column(ForeignKey("interview_sessions.id"))
    agent_name: Mapped[str] = mapped_column(String(50))
    task: Mapped[Optional[str]] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(20), default=AgentStatus.RUNNING)
    model_used: Mapped[Optional[str]] = mapped_column(String(100))
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer)
    error: Mapped[Optional[str]] = mapped_column(Text)
    token_usage: Mapped[Optional[dict]] = mapped_column(JSON)

    session: Mapped[Optional["InterviewSession"]] = relationship(back_populates="agent_runs")

    __table_args__ = (Index("ix_agent_runs_session", "session_id"),)


# ── Model Configuration & Settings ─────────────────────────────────────────

class ModelConfiguration(Base):
    __tablename__ = "model_configurations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(50), default="ollama")
    model_name: Mapped[str] = mapped_column(String(100), default="llama3.2:3b")
    embedding_model: Mapped[str] = mapped_column(String(100), default="nomic-embed-text")
    transcription_model: Mapped[str] = mapped_column(String(50), default="base")
    provider_url: Mapped[Optional[str]] = mapped_column(String(500))
    api_key_env: Mapped[Optional[str]] = mapped_column(String(100))  # env var name, not the key itself
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ApplicationSettings(Base):
    __tablename__ = "application_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(100), unique=True)
    value: Mapped[Optional[str]] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(50), default="general")
    description: Mapped[Optional[str]] = mapped_column(String(500))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
