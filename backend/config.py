"""Application configuration from environment variables — Bonsai-first, production-ready."""
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import AliasChoices, Field
from typing import Optional
import os


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ─── Database ────────────────────────────────────────────────────────────
    database_url: str = "sqlite:///./data/interview_platform.db"
    db_echo: bool = False

    # ─── AI Provider (BONSAI via local llama.cpp by default) ─────────────────
    # Primary provider. Accepts AI_PROVIDER or MODEL_PROVIDER in the environment.
    model_provider: str = Field(
        default="bonsai",
        validation_alias=AliasChoices("ai_provider", "model_provider"),
        serialization_alias="model_provider",
    )
    model_name: str = "bonsai-8b"  # Identifier; llama.cpp serves whatever model was loaded at startup
    embedding_model: str = "all-MiniLM-L6-v2"  # Local sentence-transformers model for RAG
    transcription_model: str = "base"  # faster-whisper base model (~140MB)

    # Bonsai (llama.cpp llama-server, OpenAI-compatible endpoint)
    bonsai_base_url: str = "http://127.0.0.1:8080/v1"
    bonsai_model: str = "bonsai-8b"
    bonsai_api_key: str = "local"  # Dummy value required by OpenAI-compatible wire protocol; NOT a real key
    bonsai_timeout: int = 120

    # Legacy: Ollama (only used when explicitly configured via provider="ollama")
    ollama_base_url: str = "http://localhost:11434"
    ollama_timeout: int = 120
    ollama_keep_alive: str = "5m"  # Keep model in memory for fast inference

    # ─── App ──────────────────────────────────────────────────────────────────
    app_name: str = "Kiyra — Interview Copilot"
    debug: bool = False
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"
    upload_dir: str = "./uploads"
    data_dir: str = "./data"
    chroma_dir: str = "./data/chroma"
    max_upload_bytes: int = 10 * 1024 * 1024  # 10MB

    # ─── Real-Time Audio Pipeline ─────────────────────────────────────────────
    ws_heartbeat_interval: int = 30  # WebSocket heartbeat (seconds)
    audio_chunk_duration: float = 0.25  # Capture frames in 250ms chunks (100–500ms window)
    audio_queue_max: int = 200  # Bounded audio frame queue (desktop + backend ingestion)
    transcription_queue_max: int = 10  # Prevent memory overflow
    audio_sample_rate: int = 16000

    # VAD / utterance segmentation
    speech_start_threshold: float = 0.015  # RMS energy to consider speech active
    speech_end_silence_ms: int = 300  # Trailing silence before finalizing an utterance (150–250 target zone)
    minimum_speech_duration: float = 0.25  # Utterances shorter than this are noise
    maximum_utterance_duration: float = 12.0  # Hard cap — force-finalize long speech
    stt_timeout: float = 8.0  # Max allowed STT wall time before we give up

    # Early / partial transcription (incremental STT + speculative preparation)
    realtime_partial_speech_s: float = 1.2  # Accumulated speech before the first partial STT fires
    realtime_partial_interval_s: float = 1.2  # Min speech growth between partial STT calls
    realtime_prefetch_confidence: float = 0.5  # Conf needed to at least prefetch RAG on a partial
    realtime_speculative_enabled: bool = True  # Master switch for speculative generation
    realtime_speculative_min_confidence: float = 0.62  # Conf needed to start speculation
    realtime_spec_validation_threshold: float = 0.70  # F1 partial/final similarity to reuse spec
    realtime_spec_max_ms: float = 4000.0  # Cap: abandon buffered speculation after this long
    realtime_spec_max_chars: int = 2400  # Cap: abandon speculation once buffered output reaches this

    # Question detection
    question_min_confidence: float = 0.55  # Threshold to treat an utterance as a question
    question_cooldown_seconds: float = 3.0  # Ignore new questions for this long after an answer starts
    question_dedup_window: int = 24  # Recent-question buffer for near-duplicate suppression

    # Answer generation (real-time mode)
    answer_generation_timeout: int = 15  # Max 15s between tokens once streaming
    realtime_first_token_timeout: int = 90  # First-token deadline: CPU-only llama can need ~75s for a cold prompt eval
    answer_mode: str = "short"  # short | medium | detailed (real-time default: short)
    answer_max_tokens: int = 320  # Cap answer length to keep TTFT low (legacy / detailed path)
    realtime_max_output_tokens: int = 140  # Realtime cap — short first-answer, low prompt cost
    rag_top_k: int = 3  # Context chunks retrieved per question (non-realtime)
    rag_max_context_chars: int = 2000  # Cap RAG context so prompts stay lean (non-realtime)
    rag_timeout: float = 3.0  # Hard ceiling for non-realtime RAG
    realtime_rag_timeout: float = 0.8  # Real-time ceiling — slow RAG must never block first answer
    realtime_top_k: int = 2  # Realtime retrieval depth (small = fast = lean prompt)
    realtime_max_context_chars: int = 900  # Realtime RAG context budget

    # Session memory + retrieval cache (Phase 3)
    realtime_memory_turns: int = 6  # Turns kept in the live context window
    realtime_memory_summary_chars: int = 700  # Compact summary cap for the prompt
    realtime_memory_cache_ttl_s: float = 120.0  # Retrieval cache entry lifetime
    realtime_memory_cache_entries: int = 16  # Max cached retrieval entries per session
    realtime_memory_hit_sim: float = 0.85  # Query similarity needed for a cache hit
    realtime_memory_recent_turns: int = 3  # Turns surfaced in RECENT CONVERSATION

    # Latency budgets (Phase 13) — used for [LATENCY_BUDGET_EXCEEDED] logging
    realtime_queue_budget_ms: int = 150
    realtime_partial_budget_ms: int = 500
    realtime_detect_budget_ms: int = 50
    realtime_rag_budget_ms: int = 500
    realtime_ttft_budget_ms: int = 800
    realtime_first_answer_budget_ms: int = 1500

    # ─── Stealth Mode (Desktop App) ───────────────────────────────────────────
    stealth_window_alpha: float = 0.92  # 92% opacity (balances visibility + stealth)
    stealth_auto_hide_delay: int = 10  # Auto-hide answer after 10 seconds
    stealth_screen_exclusion: bool = True  # Use SetWindowDisplayAffinity on Windows

    # ─── RAG & Knowledge ──────────────────────────────────────────────────────
    knowledge_chunk_size: int = 512  # tokens per chunk
    knowledge_chunk_overlap: int = 50  # overlap tokens
    knowledge_similarity_threshold: float = 0.3  # Min similarity score

    # ─── Agent Caching ───────────────────────────────────────────────────────
    cache_answers: bool = True  # Cache similar question answers
    cache_ttl: int = 3600  # Cache TTL in seconds

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",")]


settings = Settings()