"""Application configuration from environment variables — Ollama-first, production-ready."""
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional
import os


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ─── Database ────────────────────────────────────────────────────────────
    database_url: str = "sqlite:///./data/interview_platform.db"
    db_echo: bool = False

    # ─── AI Provider (OLLAMA ONLY) ────────────────────────────────────────────
    model_provider: str = "ollama"
    model_name: str = "llama3.2:3b"  # Fast, 3GB model for real-time on consumer hardware
    embedding_model: str = "nomic-embed-text"  # 274M, fast local embeddings
    transcription_model: str = "base"  # faster-whisper base model (~140MB)
    
    # Ollama settings
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
    
    # ─── Performance Tuning ───────────────────────────────────────────────────
    ws_heartbeat_interval: int = 30  # WebSocket heartbeat (seconds)
    audio_chunk_duration: float = 2.0  # Process audio in 2-second chunks
    transcription_queue_max: int = 10  # Prevent memory overflow
    answer_generation_timeout: int = 15  # Max 15s per answer generation
    
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
