"""Interview Orchestrator — coordinates the full interview flow."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Optional, AsyncIterator

from sqlalchemy.orm import Session

from ai.provider import AIProvider, get_provider
from agents.question_agent import QuestionUnderstandingAgent
from agents.answer_agent import AnswerAgent
from agents.coach_agent import CoachAgent
from db.models import (
    InterviewSession, InterviewQuestion, InterviewResponse,
    InterviewStatus, CandidateProfile, QuestionType
)
from knowledge.retrieval import retrieve_context


INTERVIEWER_SYSTEM = """You are a professional interviewer conducting a {interview_type} interview for a {role} position.
Ask one question at a time. Be professional and conversational.
After an answer, decide whether to ask a follow-up or move to the next question.
Keep track of what has been covered.

Candidate profile:
{candidate_context}

Current question number: {question_num} of {total_questions}
Previous questions asked: {previous_questions}

Instructions:
- Ask exactly ONE question
- Do not provide the answer or hints
- Do not explain why you're asking
- If this is a follow-up, reference the previous answer briefly
- Return ONLY the question text, nothing else"""


class InterviewOrchestrator:
    def __init__(self, db: Session, provider: Optional[AIProvider] = None):
        self.db = db
        self.provider = provider or get_provider()

    async def generate_next_question(
        self,
        session: InterviewSession,
        previous_answer: Optional[str] = None,
    ) -> Optional[str]:
        """Generate the next interview question."""
        profile = session.profile
        questions_so_far = [q.content for q in session.questions]
        question_num = len(questions_so_far) + 1

        if question_num > session.question_count:
            return None  # Interview complete

        # Build candidate context
        candidate_context = self._build_candidate_context(profile)

        system = INTERVIEWER_SYSTEM.format(
            interview_type=session.interview_type,
            role=session.target_role or "the position",
            candidate_context=candidate_context[:2000],
            question_num=question_num,
            total_questions=session.question_count,
            previous_questions=json.dumps(questions_so_far[-3:]) if questions_so_far else "None yet",
        )

        user_prompt = "Ask the next interview question."
        if previous_answer and questions_so_far:
            user_prompt = f"The candidate just answered: '{previous_answer[:500]}'\n\nAsk the next question."

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_prompt},
        ]
        result = await self.provider.complete(messages, temperature=0.8)
        question_text = result.strip() if isinstance(result, str) else ""

        if not question_text:
            return None

        # Classify question
        q_agent = QuestionUnderstandingAgent(self.db, self.provider, session.id)
        classification_result = await q_agent.run(question_text, question=question_text)
        classification = classification_result.data or {}

        # Store question in DB
        iq = InterviewQuestion(
            session_id=session.id,
            content=question_text,
            question_type=classification.get("question_type", QuestionType.UNKNOWN),
            order_index=question_num - 1,
            is_follow_up=question_num > 1 and bool(previous_answer),
            ai_classification=classification,
        )
        self.db.add(iq)
        self.db.commit()

        return question_text

    async def generate_answer_suggestion(
        self,
        session: InterviewSession,
        question_text: str,
        question_type: str = "unknown",
    ) -> dict:
        """Generate an AI answer suggestion for a question."""
        profile = session.profile
        candidate_name = profile.name if profile else "the candidate"

        # Retrieve relevant context from knowledge base
        context = await retrieve_context(
            profile_id=profile.id if profile else None,
            query=question_text,
            top_k=3,
        )

        answer_agent = AnswerAgent(self.db, self.provider, session.id)
        result = await answer_agent.run(
            question_text,
            question=question_text,
            question_type=question_type,
            context=context,
            candidate_name=candidate_name,
        )
        return result.data or {}

    async def process_answer(
        self,
        session: InterviewSession,
        question_id: int,
        answer_text: str,
        method: str = "text",
    ) -> InterviewResponse:
        """Store candidate answer and optionally evaluate it."""
        iq = self.db.query(InterviewQuestion).filter_by(id=question_id, session_id=session.id).first()
        if not iq:
            raise ValueError("Question not found in session")

        response = InterviewResponse(
            question_id=question_id,
            content=answer_text,
            method=method,
            word_count=len(answer_text.split()),
        )
        self.db.add(response)
        self.db.commit()
        return response

    async def complete_interview(self, session: InterviewSession) -> dict:
        """Mark session complete and run coach evaluation."""
        session.status = InterviewStatus.COMPLETED
        session.ended_at = datetime.utcnow()
        self.db.commit()

        coach = CoachAgent(self.db, self.provider, session.id)
        result = await coach.run("evaluate_interview", session=session)
        feedback_data = result.data or {}

        # Ensure feedback row exists in database
        if feedback_data:
            from db.models import InterviewFeedback
            fb = self.db.query(InterviewFeedback).filter_by(session_id=session.id).first()
            if not fb:
                fb = InterviewFeedback(session_id=session.id)
                self.db.add(fb)
            fb.overall_score = feedback_data.get("overall_score")
            fb.technical_score = feedback_data.get("technical_score")
            fb.communication_score = feedback_data.get("communication_score")
            fb.confidence_score = feedback_data.get("confidence_score")
            fb.relevance_score = feedback_data.get("relevance_score")
            fb.strengths = json.dumps(feedback_data.get("strengths", [])) if isinstance(feedback_data.get("strengths"), list) else feedback_data.get("strengths")
            fb.weaknesses = json.dumps(feedback_data.get("weaknesses", [])) if isinstance(feedback_data.get("weaknesses"), list) else feedback_data.get("weaknesses")
            fb.missed_opportunities = feedback_data.get("missed_opportunities")
            fb.recommended_topics = json.dumps(feedback_data.get("recommended_topics", [])) if isinstance(feedback_data.get("recommended_topics"), list) else feedback_data.get("recommended_topics")
            fb.improvement_plan = feedback_data.get("improvement_plan")
            fb.raw_analysis = feedback_data
            self.db.commit()

        return feedback_data

    def _build_candidate_context(self, profile: Optional[CandidateProfile]) -> str:
        if not profile:
            return "No candidate profile available."

        lines = [f"Candidate: {profile.name}"]
        if profile.target_role:
            lines.append(f"Target Role: {profile.target_role}")
        if profile.experience_level:
            lines.append(f"Experience Level: {profile.experience_level}")
        if profile.skills:
            skill_names = [s.name for s in profile.skills[:15]]
            lines.append(f"Key Skills: {', '.join(skill_names)}")
        if profile.projects:
            proj_names = [p.name for p in profile.projects[:5]]
            lines.append(f"Projects: {', '.join(proj_names)}")
        return "\n".join(lines)
