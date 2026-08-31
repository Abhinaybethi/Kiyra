"""Job Description analysis agent."""
from __future__ import annotations

from agents.base import BaseAgent, AgentResult
from db.models import JobDescription


SYSTEM_PROMPT = """You are a job description analyst. Analyze the job description and return ONLY valid JSON:
{
  "required_skills": ["skill1", "skill2"],
  "preferred_skills": ["skill1"],
  "responsibilities": ["responsibility1"],
  "likely_interview_areas": ["area1"],
  "technology_expectations": ["tech1"],
  "experience_required": "X years or null",
  "seniority_level": "junior|mid|senior|lead|principal",
  "role_type": "frontend|backend|fullstack|devops|data|ml|mobile|other",
  "competency_map": {
    "technical": ["competency1"],
    "behavioral": ["competency1"],
    "system_design": ["competency1"],
    "coding": ["competency1"]
  },
  "key_differentiators": ["what would make a standout candidate"]
}"""


class JobDescriptionAgent(BaseAgent):
    name = "job_description_agent"

    async def _execute(self, task: str, **kwargs) -> AgentResult:
        jd: JobDescription = kwargs.get("job_description")

        if not jd or not jd.raw_text:
            return AgentResult(success=False, error="No job description text provided")

        parsed = await self._chat_json(SYSTEM_PROMPT, f"Analyze this job description:\n\n{jd.raw_text[:6000]}")

        if not parsed:
            return AgentResult(success=False, error="Failed to analyze job description")

        jd.parsed_data = parsed
        jd.competency_map = parsed.get("competency_map", {})
        self.db.commit()

        return AgentResult(success=True, data=parsed, model_used=self.provider.model_name)
