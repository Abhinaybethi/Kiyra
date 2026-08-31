"""Seed script for development and demo data.

Usage:
  cd backend && uv run python seed.py
"""
import json
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db.database import Base, engine, SessionLocal
from db.models import (
    User, CandidateProfile, Resume, ResumeSection, Skill, Project,
    JobDescription, InterviewSession, InterviewQuestion, InterviewResponse,
    InterviewFeedback, ModelConfiguration, ApplicationSettings,
    InterviewType, InterviewStatus, QuestionType
)


def seed():
    print("[+] Seeding demo data...")
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()

    try:
        # 1. User & Profile
        user = db.query(User).filter_by(username="local").first()
        if not user:
            user = User(username="local")
            db.add(user)
            db.commit()
            db.refresh(user)

        profile = db.query(CandidateProfile).filter_by(user_id=user.id).first()
        if not profile:
            profile = CandidateProfile(
                user_id=user.id,
                name="Alex Morgan",
                target_role="Senior Full-Stack Engineer",
                experience_level="senior",
                years_of_experience=6.0,
                preferred_technologies=json.dumps(["TypeScript", "React", "Next.js", "Python", "FastAPI", "PostgreSQL", "Docker"]),
                summary="Full-stack engineer with 6 years of experience building high-throughput distributed web systems, AI workflows, and modern React SPAs.",
            )
            db.add(profile)
            db.commit()
            db.refresh(profile)

        # 2. Skills
        skills_data = [
            ("Python", "language", "expert", 6.0),
            ("TypeScript", "language", "advanced", 5.0),
            ("React / Next.js", "framework", "expert", 5.0),
            ("FastAPI", "framework", "expert", 4.0),
            ("PostgreSQL", "database", "advanced", 5.0),
            ("Docker", "tool", "advanced", 4.0),
            ("Redis", "database", "intermediate", 3.0),
            ("System Design", "other", "advanced", 4.0),
        ]
        for name, cat, prof, yrs in skills_data:
            if not db.query(Skill).filter_by(profile_id=profile.id, name=name).first():
                db.add(Skill(profile_id=profile.id, name=name, category=cat, proficiency=prof, years_used=yrs))

        # 3. Projects
        projects_data = [
            (
                "Distributed Event Streaming Platform",
                "Architected an event-driven telemetry pipeline processing 50k events/sec using Python, Kafka, and Redis.",
                ["Python", "Kafka", "Redis", "Docker"],
                "Tech Lead",
                "Reduced processing latency by 45% and scaled system to handle 10x peak traffic.",
            ),
            (
                "AI-Powered Search & Ingestion Engine",
                "Built an end-to-end vector search engine with local embeddings and semantic retrieval for 500k+ technical documents.",
                ["Next.js", "FastAPI", "ChromaDB", "Python"],
                "Senior Engineer",
                "Achieved sub-100ms vector search response times with 92% retrieval relevance.",
            )
        ]
        for name, desc, techs, role, outcomes in projects_data:
            if not db.query(Project).filter_by(profile_id=profile.id, name=name).first():
                db.add(Project(
                    profile_id=profile.id,
                    name=name,
                    description=desc,
                    technologies=json.dumps(techs),
                    role=role,
                    outcomes=outcomes,
                ))

        # 4. Job Description
        jd = db.query(JobDescription).filter_by(profile_id=profile.id).first()
        if not jd:
            jd = JobDescription(
                profile_id=profile.id,
                title="Senior Staff Software Engineer",
                company="Apex Cloud Technologies",
                raw_text="""We are seeking a Senior Staff Software Engineer to lead architecture for our real-time cloud analytics platform.
Responsibilities:
- Design and scale distributed backend microservices in Python / Go.
- Drive frontend architecture using Next.js, React, and TypeScript.
- Mentor junior and mid-level engineers.
- Collaborate with product teams on high-availability architecture.
Requirements:
- 5+ years building distributed backend services and scalable web applications.
- Strong proficiency with Python, TypeScript, SQL databases, and containerization.
- Experience with real-time streaming architectures (WebSockets, gRPC, Kafka).
- Exceptional communication and system design skills.""",
                parsed_data={
                    "seniority_level": "senior",
                    "role_type": "fullstack",
                    "required_skills": ["Python", "TypeScript", "SQL", "Docker", "System Design", "WebSockets"],
                    "responsibilities": ["Lead architecture", "Scale distributed backend", "Drive Next.js frontend", "Mentor engineers"],
                },
                competency_map={
                    "technical": ["Python internals", "FastAPI async", "React rendering lifecycle", "Database indexing"],
                    "system_design": ["Event streaming", "High availability", "Caching strategies", "Load balancing"],
                    "behavioral": ["Technical leadership", "Mentorship", "Cross-functional collaboration", "Handling production incidents"],
                },
                is_active=True,
            )
            db.add(jd)
            db.commit()
            db.refresh(jd)

        # 5. Completed Mock Interview with Feedback
        session = db.query(InterviewSession).filter_by(profile_id=profile.id).first()
        if not session:
            session = InterviewSession(
                profile_id=profile.id,
                job_description_id=jd.id,
                title="Technical & System Design Practice (Demo)",
                interview_type=InterviewType.MIXED,
                mode="practice",
                difficulty="medium",
                target_role="Senior Full-Stack Engineer",
                question_count=3,
                status=InterviewStatus.COMPLETED,
                started_at=datetime.utcnow() - timedelta(hours=2),
                ended_at=datetime.utcnow() - timedelta(hours=1, minutes=30),
            )
            db.add(session)
            db.commit()
            db.refresh(session)

            q1 = InterviewQuestion(
                session_id=session.id,
                content="Walk me through how you designed the distributed event streaming pipeline at your previous company and how you handled high throughput.",
                question_type=QuestionType.SYSTEM_DESIGN,
                order_index=0,
            )
            q2 = InterviewQuestion(
                session_id=session.id,
                content="How does Python's asyncio event loop handle I/O bound concurrency vs CPU bound tasks, and what are the architectural tradeoffs in FastAPI?",
                question_type=QuestionType.TECHNICAL,
                order_index=1,
            )
            q3 = InterviewQuestion(
                session_id=session.id,
                content="Tell me about a time you faced a critical production incident or architecture disagreement and how you resolved it.",
                question_type=QuestionType.BEHAVIORAL,
                order_index=2,
            )
            db.add_all([q1, q2, q3])
            db.commit()

            r1 = InterviewResponse(
                question_id=q1.id,
                content="In my event streaming project, we used Kafka as the durable log buffer with consumer groups partitioned by entity ID. To handle 50k events/sec, we implemented batching at the producer layer with snappy compression, and utilized Redis for deduplication within a 5-minute sliding window. This reduced database write amplification significantly.",
                method="text",
                word_count=52,
            )
            r2 = InterviewResponse(
                question_id=q2.id,
                content="FastAPI runs asynchronous endpoints directly in the asyncio single-threaded event loop, making it lightweight for I/O operations like database queries or external HTTP calls. For CPU-bound tasks, we offload to a ProcessPoolExecutor or background task worker like Celery to prevent blocking the event loop.",
                method="text",
                word_count=48,
            )
            r3 = InterviewResponse(
                question_id=q3.id,
                content="During a Black Friday spike, a database connection pool exhausted due to unindexed queries. I coordinated the war room, stabilized traffic with rate-limiting at our API gateway, and analyzed pg_stat_activity to pinpoint the slow queries. We rolled out hotfix indexes within 25 minutes with zero data loss.",
                method="text",
                word_count=49,
            )
            db.add_all([r1, r2, r3])
            db.commit()

            feedback = InterviewFeedback(
                session_id=session.id,
                overall_score=8.7,
                technical_score=9.0,
                communication_score=8.5,
                confidence_score=8.5,
                relevance_score=9.0,
                strengths=json.dumps([
                    "Clear architectural articulation with concrete throughput numbers and metrics",
                    "Strong grasp of asyncio fundamentals and CPU vs I/O concurrency tradeoffs",
                    "Effective STAR framing when explaining production incident management"
                ]),
                weaknesses=json.dumps([
                    "Could detail memory backpressure strategies under unexpected broker lag in Q1"
                ]),
                missed_opportunities="Highlighting automated alerting and SLO error budgets during the incident resolution answer.",
                recommended_topics=json.dumps(["Kafka consumer lag mitigation", "Asyncio memory profiling", "SLO/SLA definition"]),
                improvement_plan="Practice mentioning proactive observability (Prometheus/Grafana metrics) when discussing distributed systems architecture.",
            )
            db.add(feedback)
            db.commit()

        # 6. Model Config & Settings
        if not db.query(ModelConfiguration).filter_by(is_active=True).first():
            db.add(ModelConfiguration(
                provider="ollama",
                model_name="llama3.2:3b",
                embedding_model="nomic-embed-text",
                transcription_model="base",
                is_active=True,
            ))
            db.commit()

        print(" Demo seed data populated successfully.")
    finally:
        db.close()


if __name__ == "__main__":
    seed()
