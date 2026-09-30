"""AI Provider abstraction layer with retry and capability metadata.

Primary inference backend: **Bonsai** running through a local llama.cpp
`llama-server` (OpenAI-compatible `/v1` endpoint). The OpenAI-compatible
wire protocol is used only as an HTTP client to the local server — no OpenAI
cloud endpoints and no OpenAI API keys are used.

Provider resolution (`get_provider(...)`):
  1. An active `ModelConfiguration` row in the DB (if a session is provided) is
     preferred as runtime configuration.
  2. Otherwise the environment-based settings apply (`AI_PROVIDER` / `MODEL_PROVIDER`).
  3. The default provider is always `bonsai` (local llama.cpp server).
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


class AIProviderError(RuntimeError):
    """Base error for AI provider failures — surfaced to API users as-is."""


class ProviderUnavailableError(AIProviderError):
    """Raised when the inference backend cannot be reached/used."""


# Model capability registry
MODEL_CAPABILITIES: Dict[str, dict] = {
    "bonsai-8b": {
        "context_window": 8192,
        "speed_tier": "balanced",
        "supports_json": True,
        "recommended_for": "general",
        "description": "Bonsai 8B via local llama.cpp llama-server — private, local-first inference.",
    },
    "bonsai": {
        "context_window": 8192,
        "speed_tier": "balanced",
        "supports_json": True,
        "recommended_for": "general",
        "description": "Bonsai GGUF model via local llama.cpp llama-server.",
    },
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

    async def list_models(self) -> list[str]:
        """List models exposed by the server. Default: unknown."""
        return []

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


class BonsaiProvider(AIProvider):
    """Bonsai GGUF model served by a local llama.cpp `llama-server`.

    Talks to the OpenAI-compatible `/v1` endpoint exposed by llama-server.
    The only supported base URL is a local (or otherwise user-configured)
    llama.cpp instance. This class never contacts OpenAI cloud APIs.
    """

    provider_label = "bonsai"

    def __init__(
        self,
        base_url: str = None,
        api_key: str = None,
        model: str = None,
        timeout: int = None,
        max_retries: int = 2,
    ):
        self._base_url = (base_url or settings.bonsai_base_url or "").rstrip("/")
        self._api_key = api_key or settings.bonsai_api_key or ""
        self._model = model or settings.bonsai_model or settings.model_name
        self._timeout = timeout or settings.bonsai_timeout
        self._max_retries = max_retries
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def model_name(self) -> str:
        return self._model

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _require_base_url(self):
        if not self._base_url:
            raise ProviderUnavailableError(
                "AI provider is not configured. Set BONSAI_BASE_URL "
                "(e.g. http://127.0.0.1:8080/v1) or a ModelConfiguration.provider_url."
            )

    async def _shared_client(self) -> httpx.AsyncClient:
        """Lazily built connection-pooled client shared by non-stream requests.

        Streaming requests use their own client (so cancellation can tear the
        client down without disturbing the shared connection pool).
        """
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=self._timeout,
                limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
                headers=self._headers(),
            )
        return self._client

    async def aclose(self):
        if self._client is not None and not getattr(self._client, "is_closed", True):
            await self._client.aclose()
        self._client = None

    @staticmethod
    def _log_llm_metrics(request_id: str, ttft_ms: float, total_ms: float, tokens: int):
        print(
            "[LLM] request_id=%s provider=bonsai model=%s ttft=%.0fms total=%.0fms tokens=%d"
            % (request_id or "-", settings.bonsai_model, ttft_ms, total_ms, tokens)
        )

    async def complete(
        self,
        messages: list[dict],
        stream: bool = False,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        **kwargs,
    ) -> str | AsyncIterator[str]:
        self._require_base_url()

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": temperature,
            "stream": stream,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens

        if stream:
            # Reuse the *shared* connection-pooled client. Cancellation tears
            # down only the affected connection, not the pool. One client per
            # provider instance ⇒ keep-alive across every question in a session.
            client = await self._shared_client()
            return self._stream_complete(client, payload, kwargs.get("request_id"))

        last_err: Optional[Exception] = None
        request_id = kwargs.get("request_id")
        started = time.monotonic()
        for attempt in range(self._max_retries + 1):
            try:
                client = await self._shared_client()
                resp = await client.post(
                    f"{self._base_url}/chat/completions",
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                total_ms = (time.monotonic() - started) * 1000
                self._log_llm_metrics(
                    request_id, ttft_ms=total_ms, total_ms=total_ms,
                    tokens=max(0, len(content) // 4),
                )
                return content
            except (httpx.ConnectError, httpx.TimeoutException) as e:
                last_err = e
                if attempt < self._max_retries:
                    await asyncio.sleep(0.5 * (2 ** attempt))
                else:
                    raise ProviderUnavailableError(
                        "Bonsai inference server is unavailable. "
                        f"Start llama-server and verify BONSAI_BASE_URL ({self._base_url}). "
                        f"Error: {e}"
                    ) from e
            except httpx.HTTPStatusError as e:
                raise AIProviderError(
                    f"Bonsai generation failed (HTTP {e.response.status_code}): {e.response.text[:400]}"
                ) from e
            except (KeyError, IndexError) as e:
                raise AIProviderError(
                    "Bonsai generation failed: unexpected response shape from llama-server."
                ) from e

        raise ProviderUnavailableError(f"Bonsai request failed: {last_err}")

    async def _stream_complete(
        self, client: httpx.AsyncClient, payload: dict, request_id: str = None
    ) -> AsyncIterator[str]:
        started = time.monotonic()
        ttft: Optional[float] = None
        token_count = 0
        try:
            async with client.stream(
                "POST",
                f"{self._base_url}/chat/completions",
                json=payload,
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if line.startswith("data: "):
                        data = line[6:]
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                            delta = chunk["choices"][0]["delta"]
                            if content := delta.get("content"):
                                if ttft is None:
                                    ttft = (time.monotonic() - started) * 1000
                                token_count += 1
                                yield content
                        except (json.JSONDecodeError, KeyError, IndexError):
                            continue
        finally:
            if ttft is not None:
                self._log_llm_metrics(
                    request_id,
                    ttft_ms=ttft,
                    total_ms=(time.monotonic() - started) * 1000,
                    tokens=token_count,
                )
            # The client is the shared pool — do NOT close it here. The stream
            # context manager has already returned/closed the connection.

    async def embed(self, text: str) -> list[float]:
        self._require_base_url()
        payload = {"model": self._model, "input": text}
        try:
            client = await self._shared_client()
            resp = await client.post(
                f"{self._base_url}/embeddings",
                json=payload,
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json()["data"][0]["embedding"]
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise ProviderUnavailableError(
                "Bonsai embedding endpoint is unavailable. Ensure llama-server is running "
                "with embedding support."
            ) from e
        except (KeyError, IndexError, httpx.HTTPStatusError) as e:
            raise AIProviderError(f"Bonsai embeddings failed: {e}") from e

    async def health_check(self) -> bool:
        self._require_base_url()
        try:
            client = await self._shared_client()
            resp = await client.get(f"{self._base_url}/models", timeout=5)
            return resp.status_code == 200
        except Exception:
            return False

    async def list_models(self) -> list[str]:
        if not self._base_url:
            return []
        try:
            client = await self._shared_client()
            resp = await client.get(f"{self._base_url}/models", timeout=10)
            resp.raise_for_status()
            data = resp.json()
            models = []
            for item in data.get("data", []):
                if mid := item.get("id"):
                    models.append(str(mid))
            return models
        except Exception:
            return []


class OllamaProvider(AIProvider):
    """Legacy Ollama local model provider — only used when explicitly configured.

    Kept for optional compatibility. It is NOT a fallback and must never be the
    implicit default. Select it explicitly with provider="ollama" via
    `AI_PROVIDER`/`MODEL_PROVIDER` or the active ModelConfiguration.
    """

    provider_label = "ollama"

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
            raise ProviderUnavailableError("Ollama base URL is not configured")

        payload: dict[str, Any] = {
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

        last_err: Optional[Exception] = None
        for attempt in range(self._max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.post(f"{self._base_url}/api/chat", json=payload)
                    resp.raise_for_status()
                    return resp.json()["message"]["content"]
            except (httpx.ConnectError, httpx.TimeoutException) as e:
                last_err = e
                if attempt < self._max_retries:
                    await asyncio.sleep(0.5 * (2 ** attempt))
                else:
                    raise ProviderUnavailableError(
                        f"Ollama is unreachable at {self._base_url}. Ensure 'ollama serve' is running. Error: {e}"
                    ) from e
            except httpx.HTTPStatusError as e:
                raise AIProviderError(
                    f"Ollama returned HTTP error {e.response.status_code}: {e.response.text[:400]}"
                ) from e

        raise ProviderUnavailableError(f"Ollama request failed: {last_err}")

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
            raise ProviderUnavailableError("Ollama base URL is not configured")
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


# Providers that talk to a local (or user-configured) OpenAI-compatible endpoint.
# "openai"/"openrouter" are intentionally mapped here so the client never
# silently defaults to a cloud host — the base URL must be configured explicitly
# and the default always points at the local llama.cpp server.
_OPENAI_COMPATIBLE_ALIASES = {
    "bonsai",
    "openai_compatible",
    "openai",
    "openrouter",
    "lmstudio",
    "vllm",
}


def _provider_from_model_type(
    provider_type: str,
    model_name: str,
    base_url: Optional[str],
    api_key: Optional[str],
) -> AIProvider:
    provider_type = (provider_type or "bonsai").strip().lower()
    if provider_type == "ollama":
        return OllamaProvider(base_url=base_url, model=model_name)
    return BonsaiProvider(base_url=base_url, api_key=api_key, model=model_name)


async def ensure_provider_ready(provider: AIProvider) -> None:
    """Fail fast when the inference backend is unreachable.

    Used before long-running AI workflows so callers get a clear, domain-level
    503 instead of a connection error buried deep inside orchestration.
    """
    if not isinstance(provider, BonsaiProvider):
        return  # Only the default Bonsai path requires a local llama-server preflight.
    try:
        if not await provider.health_check():
            raise ProviderUnavailableError(
                "Bonsai inference server is unavailable. "
                "Start llama-server and verify BONSAI_BASE_URL."
            )
    except ProviderUnavailableError:
        raise
    except Exception as e:
        raise ProviderUnavailableError(
            f"Bonsai inference server is unavailable. "
            f"Start llama-server and verify BONSAI_BASE_URL. Error: {e}"
        ) from e


def get_provider(override_model: str = None, db: Optional[Session] = None) -> AIProvider:
    """Factory: returns the configured provider with the configured model.

    Resolution order:
      1. Active `ModelConfiguration` row in the DB (when `db` is provided).
      2. Environment settings (`AI_PROVIDER`/`MODEL_PROVIDER`, `BONSAI_*`).

    The default provider is `bonsai`. No OpenAI API key is ever required.
    """
    provider_type = settings.model_provider.lower()
    model_name = override_model or settings.model_name
    base_url: Optional[str] = None
    api_key: Optional[str] = None

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

    # Resolve per-provider defaults from settings (never from OpenAI cloud env).
    if provider_type == "ollama":
        base_url = base_url or settings.ollama_base_url
    else:
        provider_type = provider_type if provider_type in _OPENAI_COMPATIBLE_ALIASES else "bonsai"
        base_url = base_url or settings.bonsai_base_url
        api_key = api_key or settings.bonsai_api_key

    print(f"[AI_PROVIDER] provider={provider_type}")
    print(f"[AI_PROVIDER] base_url={base_url}")
    print(f"[AI_PROVIDER] model={model_name}")

    return _provider_from_model_type(provider_type, model_name, base_url, api_key)