"""Base agent class and agent result types."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Any

from sqlalchemy.orm import Session

from ai.provider import AIProvider, get_provider
from db.models import AgentRun, AgentStatus


@dataclass
class AgentResult:
    success: bool
    data: Any = None
    error: Optional[str] = None
    latency_ms: int = 0
    model_used: str = ""


class BaseAgent:
    """All agents inherit from this."""

    name: str = "base_agent"

    def __init__(
        self,
        db: Session,
        provider: Optional[AIProvider] = None,
        session_id: Optional[int] = None,
    ):
        self.db = db
        self.provider = provider or get_provider()
        self.session_id = session_id

    async def run(self, task: str, **kwargs) -> AgentResult:
        """Override in subclasses. Logs run to DB."""
        run = AgentRun(
            session_id=self.session_id,
            agent_name=self.name,
            task=task[:500] if task else None,
            started_at=datetime.utcnow(),
            status=AgentStatus.RUNNING,
            model_used=self.provider.model_name,
        )
        self.db.add(run)
        self.db.flush()

        start = time.monotonic()
        try:
            result = await self._execute(task, **kwargs)
            run.status = AgentStatus.COMPLETED
            run.ended_at = datetime.utcnow()
            run.latency_ms = int((time.monotonic() - start) * 1000)
            self.db.commit()
            return result
        except Exception as e:
            run.status = AgentStatus.FAILED
            run.ended_at = datetime.utcnow()
            run.latency_ms = int((time.monotonic() - start) * 1000)
            run.error = str(e)[:1000]
            self.db.commit()
            return AgentResult(success=False, error=str(e))

    async def _execute(self, task: str, **kwargs) -> AgentResult:
        raise NotImplementedError

    async def _chat(
        self,
        system: str,
        user: str,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> str:
        """Simple chat helper."""
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        result = await self.provider.complete(messages, temperature=temperature, max_tokens=max_tokens)
        return result if isinstance(result, str) else ""

    async def _chat_json(
        self,
        system: str,
        user: str,
        temperature: float = 0.3,
    ) -> dict:
        """Chat and parse JSON response."""
        import json
        import re
        response = await self._chat(system, user, temperature=temperature)
        # Extract JSON from response (model may wrap in markdown)
        json_match = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", response)
        if json_match:
            response = json_match.group(1)
        try:
            return json.loads(response.strip())
        except json.JSONDecodeError:
            # Try to find raw JSON object
            obj_match = re.search(r"\{[\s\S]+\}", response)
            if obj_match:
                return json.loads(obj_match.group())
            return {}
