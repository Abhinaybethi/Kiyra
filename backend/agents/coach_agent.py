"""Coach agent — evaluates candidate responses and generates interview feedback."""
from __future__ import annotations

import json

from agents.base import BaseAgent, AgentResult
from db.models import InterviewSession, InterviewFeedback


EVALUATION_SYSTEM = """You are a senior interview coach evaluating a candidate's interview performance.
Analyze all questions and responses, then return ONLY valid JSON:
{
  "overall_score": 7.5,
  "technical_score": 8.0,
  "communication_score": 7.0,
  "confidence_score": 6.5,
  "relevance_score": 8.0,
  "strengths": ["strength 1", "strength 2", "strength 3"],
  "weaknesses": ["weakness 1", "weakness 2"],
  "missed_opportunities": "what the candidate could have highlighted but didn't",
  "recommended_topics": ["topic to study 1", "topic 2"],
  "improvement_plan": "specific, actionable 2-3 sentence plan for improvement",
  "per_question_feedback": [
    {
      "question": "the question",
      "score": 7.5,
      "feedback": "specific feedback for this answer",
      "star_used": false,
      "was_complete": true
    }
  ]
}
Scores are 0-10. Be honest but constructive. Note: these are AI estimates, not objective assessments."""

QUESTION_EVAL_SYSTEM = """Evaluate this single interview answer. Return ONLY valid JSON:
{
  "score": 7.5,
  "feedback": "specific, actionable feedback in 1-2 sentences",
  "star_used": false,
  "was_complete": true,
  "key_strength": "what they did well",
  "key_improvement": "what to improve"
}"""


class CoachAgent(BaseAgent):
    name = "coach_agent"

    async def _execute(self, task: str, **kwargs) -> AgentResult:
        session: InterviewSession = kwargs.get("session")
        if not session:
            return AgentResult(success=False, error="No session provided")

        # Build transcript for evaluation
        qa_pairs = []
        for question in session.questions:
            q_text = question.content
            a_text = question.response.content if question.response else "[No answer provided]"
            qa_pairs.append(f"Q: {q_text}\nA: {a_text}")

        full_transcript = "\n\n---\n\n".join(qa_pairs)
        role = session.target_role or "the target role"

        user_prompt = f"""Interview for: {role}
Interview type: {session.interview_type}

Full Q&A:
{full_transcript[:8000]}

Evaluate this interview performance."""

        feedback_data = await self._chat_json(EVALUATION_SYSTEM, user_prompt, temperature=0.3)

        if not feedback_data:
            return AgentResult(success=False, error="Failed to generate feedback")

        # Store feedback in DB
        existing = self.db.query(InterviewFeedback).filter_by(session_id=session.id).first()
        if existing:
            fb = existing
        else:
            fb = InterviewFeedback(session_id=session.id)
            self.db.add(fb)

        fb.overall_score = feedback_data.get("overall_score")
        fb.technical_score = feedback_data.get("technical_score")
        fb.communication_score = feedback_data.get("communication_score")
        fb.confidence_score = feedback_data.get("confidence_score")
        fb.relevance_score = feedback_data.get("relevance_score")
        fb.strengths = json.dumps(feedback_data.get("strengths", []))
        fb.weaknesses = json.dumps(feedback_data.get("weaknesses", []))
        fb.missed_opportunities = feedback_data.get("missed_opportunities")
        fb.recommended_topics = json.dumps(feedback_data.get("recommended_topics", []))
        fb.improvement_plan = feedback_data.get("improvement_plan")
        fb.raw_analysis = feedback_data

        self.db.commit()
        return AgentResult(success=True, data=feedback_data, model_used=self.provider.model_name)

    async def evaluate_single_response(self, question: str, answer: str) -> AgentResult:
        """Quick evaluation of a single answer — used in practice mode."""
        user_prompt = f"Question: {question}\n\nAnswer: {answer}"
        data = await self._chat_json(QUESTION_EVAL_SYSTEM, user_prompt, temperature=0.3)
        return AgentResult(success=bool(data), data=data, model_used=self.provider.model_name)
