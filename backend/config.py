"""Application configuration from environment variables."""
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional
import os


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Database
    database_url: str = "sqlite:///./data/interview_platform.db"
    db_echo: bool = False

    # AI Provider
    model_provider: str = "ollama"
    model_name: str = "llama3:latest"
    embedding_model: str = "nomic-embed-text"
    transcription_model: str = "base"
    ollama_base_url: str = "http://localhost:11434"
    ollama_timeout: int = 120

    # OpenAI-compatible fallback
    openai_api_key: Optional[str] = None
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"

    # App
    app_name: str = "AI Interview Platform"
    debug: bool = False
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"
    upload_dir: str = "./uploads"
    data_dir: str = "./data"
    chroma_dir: str = "./data/chroma"
    max_upload_bytes: int = 10 * 1024 * 1024  # 10MB

    # Real-time
    ws_heartbeat_interval: int = 30

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",")]


settings = Settings()
