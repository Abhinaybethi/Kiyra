"""AI Provider abstraction layer with retry, fallback, and capability metadata.

This module provides provider implementations (Ollama, OpenAI-compatible) and
an authoritative factory `get_provider(...)` which accepts an optional SQLAlchemy
Session. When a DB session is provided, an active ModelConfiguration row (if
present) is preferred as the runtime configuration. Environment-based settings
remain as fallbacks.
"""
from __future__ import annotations

import asyncio
import json
import time
import os
from abc import ABC, abstractmethod
from typing import AsyncIterator, Optional, Any, Dict
import httpx

from config import settings
from sqlalchemy.orm import Session
from db.models import ModelConfiguration


# Model capability registry
MODEL_CAPABILITIES: Dict[str, dict] = {
    "llama3.2:3b": {
        "context_window": 128000,
        "speed_tier": "fast",
        "supports_json": True,
        "recommended_for": "realtime_assist",
        "description": "Meta Llama 3.2 3B - Ultra fast, low memory footprint.",
    },
    "llama3.1:8b": {
        "context_window": 128000,
        "speed_tier": "balanced",
        "supports_json": True,
        "recommended_for": "practice_interviews",
        "description": "Meta Llama 3.1 8B - High quality reasoning and question asking.",
    },
    "mistral:7b": {
        "context_window": 32768,
        "speed_tier": "balanced",
        "supports_json": True,
        "recommended_for": "general",
        "description": "Mistral 7B - Excellent reasoning and concise responses.",
    },
    "qwen2.5-coder:7b": {
        "context_window": 128000,
        "speed_tier": "fast",
        "supports_json": True,
        "recommended_for": "coding_technical",
        "description": "Qwen 2.5 Coder 7B - Specialized for coding and system design.",
    },
    "deepseek-r1:8b": {
        "context_window": 64000,
        "speed_tier": "reasoning",
        "supports_json": True,
        "recommended_for": "deep_evaluations",
        "description": "DeepSeek R1 8B - Advanced step-by-step reasoning.",
    },
}


class AIProvider(ABC):
    """Abstract base for all AI providers."""

    @abstractmethod
    async def complete(
        self,
        messages: list[dict],
        stream: bool = False,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        **kwargs,
    ) -> str | AsyncIterator[str]:
        ...

    @abstractmethod
    async def embed(self, text: str) -> list[float]:
        ...

    @abstractmethod
    async def health_check(self) -> bool:
        ...

    @property
    @abstractmethod
    def model_name(self) -> str:
        ...

    def get_capabilities(self) -> dict:
        """Return model metadata or sensible defaults."""
        return MODEL_CAPABILITIES.get(
            self.model_name,
            {
                "context_window": 8192,
                "speed_tier": "standard",
                "supports_json": True,
                "recommended_for": "general",
                "description": f"Configured model: {self.model_name}",
            },
        )


class OllamaProvider(AIProvider):
    """Ollama local model provider with exponential retry and timeout handling."""

    def __init__(
        self,
        base_url: str = None,
        model: str = None,
        timeout: int = None,
        max_retries: int = 2,
    ):
        self._base_url = (base_url or settings.ollama_base_url or "").rstrip("/")
        self._model = model or settings.model_name
        self._timeout = timeout or settings.ollama_timeout
        self._max_retries = max_retries

    @property
    def model_name(self) -> str:
        return self._model

    async def complete(
        self,
        messages: list[dict],
        stream: bool = False,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        **kwargs,
    ) -> str | AsyncIterator[str]:
        if not self._base_url:
            raise RuntimeError("Ollama base URL is not configured")

        payload = {
            "model": self._model,
            "messages": messages,
            "stream": stream,
            "options": {"temperature": temperature},
        }
        if max_tokens:
            payload["options"]["num_predict"] = max_tokens

        if stream:
            client = httpx.AsyncClient(timeout=self._timeout)
            return self._stream_complete(client, payload)

        last_err = None
        for attempt in range(self._max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.post(f"{self._base_url}/api/chat", json=payload)
                    resp.raise_for_status()
                    data = resp.json()
                    return data["message"]["content"]
            except (httpx.ConnectError, httpx.TimeoutException) as e:
                last_err = e
                if attempt < self._max_retries:
                    await asyncio.sleep(0.5 * (2 ** attempt))
                else:
                    raise RuntimeError(
                        f"Ollama is unreachable at {self._base_url}. Ensure 'ollama serve' is running. Error: {e}"
                    )
            except httpx.HTTPStatusError as e:
                raise RuntimeError(f"Ollama returned HTTP error {e.response.status_code}: {e.response.text}")

        raise RuntimeError(f"Ollama request failed: {last_err}")

    async def _stream_complete(self, client: httpx.AsyncClient, payload: dict) -> AsyncIterator[str]:
        try:
            async with client.stream("POST", f"{self._base_url}/api/chat", json=payload) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if line:
                        try:
                            chunk = json.loads(line)
                            if content := chunk.get("message", {}).get("content"):
                                yield content
                            if chunk.get("done"):
                                break
                        except json.JSONDecodeError:
                            continue
        finally:
            await client.aclose()

    async def embed(self, text: str) -> list[float]:
        if not self._base_url:
            raise RuntimeError("Ollama base URL is not configured")
        payload = {"model": settings.embedding_model, "input": text}
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(f"{self._base_url}/api/embed", json=payload)
            resp.raise_for_status()
            data = resp.json()
            embeddings = data.get("embeddings", [])
            return embeddings[0] if embeddings else []

    async def health_check(self) -> bool:
        if not self._base_url:
            return False
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{self._base_url}/api/tags")
                return resp.status_code == 200
        except Exception:
            return False

    async def list_models(self) -> list[str]:
        if not self._base_url:
            return []
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{self._base_url}/api/tags")
                resp.raise_for_status()
                return [m["name"] for m in resp.json().get("models", [])]
        except Exception:
            return []


class OpenAICompatibleProvider(AIProvider):
    """Works for OpenAI, OpenRouter, LMStudio, vLLM, any OpenAI-compatible API."""

    def __init__(
        self,
        base_url: str = None,
        api_key: str = None,
        model: str = None,
        timeout: int = 120,
        max_retries: int = 2,
    ):
        self._base_url = (base_url or settings.openai_base_url or "").rstrip("/")
        self._api_key = api_key or settings.openai_api_key or ""
        self._model = model or settings.openai_model
        self._timeout = timeout
        self._max_retries = max_retries

    @property
    def model_name(self) -> str:
        return self._model

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    async def complete(
        self,
        messages: list[dict],
        stream: bool = False,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        **kwargs,
    ) -> str | AsyncIterator[str]:
        if not self._base_url:
            raise RuntimeError("OpenAI-compatible base URL is not configured")
        if not self._api_key:
            raise RuntimeError("OpenAI-compatible API key is not configured in environment")

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": temperature,
            "stream": stream,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens

        if stream:
            client = httpx.AsyncClient(timeout=self._timeout)
            return self._stream_complete(client, payload)

        last_err = None
        for attempt in range(self._max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.post(
                        f"{self._base_url}/chat/completions",
                        json=payload,
                        headers=self._headers(),
                    )
                    resp.raise_for_status()
                    return resp.json()["choices"][0]["message"]["content"]
            except (httpx.ConnectError, httpx.TimeoutException) as e:
                last_err = e
                if attempt < self._max_retries:
                    await asyncio.sleep(0.5 * (2 ** attempt))
                else:
                    raise RuntimeError(f"OpenAI endpoint unreachable: {e}")
            except httpx.HTTPStatusError as e:
                raise RuntimeError(f"API returned HTTP error {e.response.status_code}: {e.response.text}")

        raise RuntimeError(f"API request failed: {last_err}")

    async def _stream_complete(self, client: httpx.AsyncClient, payload: dict) -> AsyncIterator[str]:
        try:
            async with client.stream(
                "POST", f"{self._base_url}/chat/completions",
                json=payload, headers=self._headers()
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if line.startswith("data: "):
                        data = line[6:]
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                            if content := chunk["choices"][0]["delta"].get("content"):
                                yield content
                        except (json.JSONDecodeError, KeyError, IndexError):
                            continue
        finally:
            await client.aclose()

    async def embed(self, text: str) -> list[float]:
        if not self._base_url or not self._api_key:
            raise RuntimeError("Embedding endpoint or API key not configured")
        payload = {"model": "text-embedding-ada-002", "input": text}
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{self._base_url}/embeddings",
                json=payload,
                headers=self._headers(),
            )
            resp.raise_for_status()
            return resp.json()["data"][0]["embedding"]

    async def health_check(self) -> bool:
        if not self._base_url:
            return False
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{self._base_url}/models", headers=self._headers())
                return resp.status_code == 200
        except Exception:
            return False


def _provider_from_model_type(provider_type: str, model_name: str, base_url: Optional[str], api_key: Optional[str]) -> AIProvider:
    provider_type = (provider_type or "ollama").lower()
    if provider_type == "ollama":
        return OllamaProvider(base_url=base_url, model=model_name)
    elif provider_type in ("openai", "openai_compatible", "openrouter", "lmstudio"):
        return OpenAICompatibleProvider(base_url=base_url, api_key=api_key, model=model_name)
    else:
        # default to Ollama
        return OllamaProvider(base_url=base_url, model=model_name)


def get_provider(override_model: str = None, db: Optional[Session] = None) -> AIProvider:
    """Factory: returns the configured provider with configured model.

    Priority:
      1. If a DB session is provided and an active ModelConfiguration exists, prefer its values.
      2. Otherwise fall back to environment-based settings via config.settings.

    Note: API keys are read from environment variables whose name may be stored in
    the ModelConfiguration.api_key_env column. We never persist raw API keys in the DB.
    """
    provider_type = settings.model_provider.lower()
    model_name = override_model or settings.model_name
    base_url = None
    api_key = None

    if db is not None:
        try:
            cfg = db.query(ModelConfiguration).filter_by(is_active=True).first()
            if cfg:
                provider_type = (cfg.provider or provider_type).lower()
                model_name = override_model or (cfg.model_name or model_name)
                base_url = cfg.provider_url
                if cfg.api_key_env:
                    api_key = os.environ.get(cfg.api_key_env)
        except Exception:
            # If DB read fails for any reason, fall back to environment settings
            cfg = None

    # If no api_key supplied from DB, use env-level fallback
    if not api_key:
        api_key = os.environ.get("OPENAI_API_KEY") or settings.openai_api_key

    # If base_url not provided by DB, use configured environment defaults per provider
    if not base_url:
        if provider_type == "ollama":
            base_url = settings.ollama_base_url
        else:
            base_url = settings.openai_base_url

    return _provider_from_model_type(provider_type, model_name, base_url, api_key)
