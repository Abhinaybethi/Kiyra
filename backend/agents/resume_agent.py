"""Resume parsing agent."""
from __future__ import annotations

import json
from typing import Optional

from agents.base import BaseAgent, AgentResult
from db.models import Resume, ResumeSection, Skill, Project, CandidateProfile


SYSTEM_PROMPT = """You are a resume parser. Extract structured information from the resume text.
Return ONLY valid JSON with this exact structure:
{
  "name": "candidate name or null",
  "email": "email or null",
  "phone": "phone or null",
  "location": "location or null",
  "summary": "professional summary or null",
  "skills": [{"name": "skill", "category": "language|framework|tool|cloud|database|other", "proficiency": "beginner|intermediate|advanced|expert"}],
  "experience": [{"company": "name", "title": "role", "start_date": "YYYY-MM", "end_date": "YYYY-MM or Present", "description": "summary", "technologies": ["tech1"]}],
  "education": [{"institution": "name", "degree": "degree", "field": "field", "graduation_year": "YYYY"}],
  "projects": [{"name": "name", "description": "description", "technologies": ["tech"], "outcomes": "results or impact", "url": "url or null"}],
  "certifications": [{"name": "cert", "issuer": "issuer", "year": "YYYY"}],
  "sections": [{"type": "section_type", "title": "display title", "content": "raw content"}]
}
Be accurate. Do not invent information. If something is missing, use null."""


class ResumeAgent(BaseAgent):
    name = "resume_agent"

    async def _execute(self, task: str, **kwargs) -> AgentResult:
        resume: Resume = kwargs.get("resume")
        profile: CandidateProfile = kwargs.get("profile")

        if not resume or not resume.raw_text:
            return AgentResult(success=False, error="No resume text provided")

        parsed = await self._chat_json(SYSTEM_PROMPT, f"Parse this resume:\n\n{resume.raw_text[:8000]}")

        if not parsed:
            return AgentResult(success=False, error="Failed to parse resume — model returned no JSON")

        # Store parsed data on resume
        resume.parsed_data = parsed
        self.db.flush()

        # Store sections
        for i, section in enumerate(parsed.get("sections", [])):
            existing = self.db.query(ResumeSection).filter_by(
                resume_id=resume.id, section_type=section.get("type", "other")
            ).first()
            if not existing:
                rs = ResumeSection(
                    resume_id=resume.id,
                    section_type=section.get("type", "other"),
                    title=section.get("title"),
                    content=section.get("content", ""),
                    order_index=i,
                )
                self.db.add(rs)

        # Upsert skills
        if profile and parsed.get("skills"):
            for skill_data in parsed["skills"]:
                name = skill_data.get("name", "").strip()
                if not name:
                    continue
                existing = self.db.query(Skill).filter_by(profile_id=profile.id, name=name).first()
                if not existing:
                    self.db.add(Skill(
                        profile_id=profile.id,
                        name=name,
                        category=skill_data.get("category", "other"),
                        proficiency=skill_data.get("proficiency", "intermediate"),
                    ))

        # Upsert projects
        if profile and parsed.get("projects"):
            for proj_data in parsed["projects"]:
                name = proj_data.get("name", "").strip()
                if not name:
                    continue
                existing = self.db.query(Project).filter_by(profile_id=profile.id, name=name).first()
                if not existing:
                    techs = proj_data.get("technologies", [])
                    self.db.add(Project(
                        profile_id=profile.id,
                        name=name,
                        description=proj_data.get("description"),
                        technologies=json.dumps(techs) if techs else None,
                        outcomes=proj_data.get("outcomes"),
                        url=proj_data.get("url"),
                    ))

        # Update profile name if not set
        if profile and parsed.get("name") and not profile.name:
            profile.name = parsed["name"]

        self.db.commit()
        return AgentResult(success=True, data=parsed, model_used=self.provider.model_name)
