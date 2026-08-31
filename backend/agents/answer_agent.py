"""Answer generation agent — produces candidate-specific answers."""
from __future__ import annotations

from agents.base import BaseAgent, AgentResult


BEHAVIORAL_SYSTEM = """You are a senior interview coach helping a candidate answer behavioral questions.
Generate a concise, natural answer using the STAR framework (Situation, Task, Action, Result).
Use only the candidate's actual experience provided in context.
If context is missing, acknowledge it and guide the candidate on what to say.
Return ONLY valid JSON:
{
  "answer": "the full suggested answer in 2-4 paragraphs, natural language",
  "key_points": ["point 1", "point 2", "point 3"],
  "star": {
    "situation": "situation description",
    "task": "task description",
    "action": "action description",
    "result": "result description"
  },
  "follow_up_questions": ["likely follow-up 1", "likely follow-up 2"],
  "missing_context": "what candidate needs to fill in from their own experience, or null",
  "confidence": 0.85
}"""

TECHNICAL_SYSTEM = """You are a senior technical interviewer helping a candidate answer technical questions.
Generate a clear, accurate technical answer.
Structure: Direct answer → explanation → example → tradeoffs.
Return ONLY valid JSON:
{
  "answer": "the full technical answer",
  "key_points": ["point 1", "point 2"],
  "direct_answer": "one sentence direct answer",
  "explanation": "detailed explanation",
  "example": "code or real example",
  "tradeoffs": ["tradeoff 1"],
  "follow_up_questions": ["likely follow-up"],
  "confidence": 0.9
}"""

CODING_SYSTEM = """You are helping a candidate in a coding interview.
Provide a clear approach, then code/pseudocode, then complexity analysis.
Return ONLY valid JSON:
{
  "answer": "conversational explanation of the solution",
  "approach": "algorithm/strategy explanation",
  "code": "clean code solution with comments",
  "complexity": {"time": "O(?)", "space": "O(?)"},
  "edge_cases": ["edge case 1"],
  "key_points": ["point 1"],
  "alternative_approaches": ["alternative 1"],
  "confidence": 0.9
}"""

HR_SYSTEM = """You are helping a candidate answer HR and general interview questions.
Be authentic, professional, and concise.
Return ONLY valid JSON:
{
  "answer": "natural, genuine answer",
  "key_points": ["point 1", "point 2"],
  "tone_tips": "brief tone/delivery advice",
  "follow_up_questions": ["likely follow-up"],
  "confidence": 0.85
}"""


class AnswerAgent(BaseAgent):
    name = "answer_agent"

    async def _execute(self, task: str, **kwargs) -> AgentResult:
        question: str = kwargs.get("question", task)
        question_type: str = kwargs.get("question_type", "unknown")
        context: str = kwargs.get("context", "")  # retrieved from knowledge base
        candidate_name: str = kwargs.get("candidate_name", "the candidate")

        if not question:
            return AgentResult(success=False, error="No question provided")

        # Select system prompt by question type
        if question_type in ("behavioral", "hr"):
            system = BEHAVIORAL_SYSTEM if question_type == "behavioral" else HR_SYSTEM
        elif question_type == "coding":
            system = CODING_SYSTEM
        elif question_type == "technical":
            system = TECHNICAL_SYSTEM
        else:
            # Default to technical for unknown
            system = TECHNICAL_SYSTEM

        context_block = f"\n\nCandidate context (use this to personalize the answer):\n{context}" if context else ""

        user_prompt = f"""Interview question: {question}
Candidate: {candidate_name}{context_block}

Generate a suggested answer."""

        answer_data = await self._chat_json(system, user_prompt, temperature=0.5)

        if not answer_data:
            return AgentResult(success=False, error="Failed to generate answer")

        return AgentResult(success=True, data=answer_data, model_used=self.provider.model_name)
