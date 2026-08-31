"""Question Understanding Agent — classifies interview questions."""
from __future__ import annotations

from agents.base import BaseAgent, AgentResult


SYSTEM_PROMPT = """You classify interview questions. Return ONLY valid JSON:
{
  "question_type": "hr|behavioral|technical|coding|system_design|follow_up|unknown",
  "intent": "brief description of what interviewer wants to learn",
  "technical_topic": "specific technical topic or null",
  "behavioral_topic": "specific behavioral topic or null",
  "is_coding_question": false,
  "requires_star_answer": false,
  "difficulty": "easy|medium|hard",
  "confidence": 0.95,
  "suggested_approach": "brief hint on how to approach this answer"
}"""


class QuestionUnderstandingAgent(BaseAgent):
    name = "question_understanding_agent"

    async def _execute(self, task: str, **kwargs) -> AgentResult:
        question: str = kwargs.get("question", task)

        if not question:
            return AgentResult(success=False, error="No question provided")

        classification = await self._chat_json(
            SYSTEM_PROMPT,
            f"Classify this interview question:\n\n{question}"
        )

        return AgentResult(success=True, data=classification, model_used=self.provider.model_name)
