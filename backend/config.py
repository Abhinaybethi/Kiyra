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
    # Keep the model resident between turns. Ollama's default is 5 minutes; an
    # interview easily has longer pauses, and a cold reload is ~20s (longer than
    # the first-token deadline), which silently produced empty answers.
    ollama_keep_alive: str = "30m"
    # Pin the context window. Ollama otherwise allocates the model's full
    # context (16k+ KV cells) and can reload the model whenever the requested
    # context changes. Live prompts are ~1-2k tokens, so 4096 is ample and keeps
    # VRAM low (the overlay may share a 6 GB laptop GPU).
    ollama_num_ctx: int = 4096
    # Prefill batch sizes. Ollama's default (512) processes the prompt in
    # small batches, which costs ~1.1-2.1 s for a live prompt on this
    # laptop GPU.
    #
    # Measured on the RTX 3050 / qwen3.5:4b (time to the FIRST streamed token
    # on the real production prompt, 1192 tokens):
    #
    #     no batch     3979-4038 ms   prefill 1067-1092 ms
    #     num_batch 1024  4430-4614 ms  prefill 1787-1840 ms  (SLOWER — the
    #                         prompt no longer fits in one batch)
    #     num_batch 2048  2917-3013 ms  prefill  156- 263 ms  (best)
    #
    # CAUTION when re-measuring: Ollama caches the KV state of a prompt, so
    # replaying the SAME prompt twice reports a tiny prefill that has
    # nothing to do with the setting (measured: 154 ms cached vs 2380 ms
    # for the same prompt with one changed token). Always measure with a
    # UNIQUE prompt — that is what the live path sends on every turn.
    #
    # On the live path (800-1900 token prompts) the prefill settles at
    # ~1.7-2.0 ms/token and the batch size moves the total TTFT by less
    # than the run-to-run noise, so 2048 is kept: it keeps the whole live
    # prompt in ONE batch (the 1858-token coding prompt measured 2348 ms
    # prefill at 2048 vs 2996 ms at 512) and costs ~300 MiB more VRAM
    # (4.05 of 6.1 GB).
    #
    # These MUST stay identical on every request (including the startup
    # warmup): Ollama reloads the runner when they change, and a reload costs
    # ~20 s. Set them back to 1024 (or 512) on a GPU with less than ~5 GB.
    ollama_num_batch: int = 2048
    ollama_num_ubatch: int = 2048

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
    # Adaptive VAD: track the mic's noise floor so a room noisier than the
    # absolute threshold doesn't read as perpetual speech (never-ending
    # utterances, empty transcripts). Seed sits above typical room noise; the
    # detector adapts down to the real floor and requires speech to exceed it.
    vad_adaptive: bool = True
    vad_noise_floor_seed: float = 0.05  # initial floor estimate (RMS)
    vad_noise_floor_ratio: float = 2.2  # speech must exceed floor * this
    speech_end_silence_ms: int = 150  # Trailing silence before finalizing an utterance (150–250 target zone)
    minimum_speech_duration: float = 0.25  # Utterances shorter than this are noise
    maximum_utterance_duration: float = 12.0  # Hard cap — force-finalize long speech
    stt_timeout: float = 3.0  # Max allowed STT wall time before we give up
    # One bounded retry after stt_timeout. Absorbs a cold Whisper load without
    # ever discarding the candidate's audio (losing speech is unrecoverable).
    stt_retry_timeout: float = 25.0

    # Whisper model size: tiny (fastest, ~40MB), base (default, ~140MB),
    # small (~500MB), medium (~1.5GB). Override with KIYRA_TRANSCRIPTION_MODEL.
    # Smaller = faster TTFT, lower accuracy; larger = slower, more accurate.
    transcription_model: str = "base"  # faster-whisper base model (~140MB)
    # Whisper vocabulary hint. faster-whisper's `initial_prompt` biases the
    # decoder toward these spellings. Without it the `base` model hears the
    # product name "Kiyra" as "CURE", and the answer then confidently describes
    # a caching layer that was never built. Deliberately short: a long hint
    # biases short clips (and can be echoed back on near-silent audio).
    # Override with TRANSCRIPTION_INITIAL_PROMPT (no KIYRA_ prefix — this
    # Settings class sets no env_prefix).
    transcription_initial_prompt: str = (
        "Kiyra, an interview copilot. FastAPI, Next.js, TypeScript, Python, "
        "WebSocket, Whisper, Ollama, PostgreSQL, SQLAlchemy, Alembic, "
        "Tkinter, RAG, embeddings, asyncio, uvicorn, pytest, Docker, "
        "Kubernetes, Redis, Celery, machine learning"
    )
    # Append the candidate's own project and skill names from the database so
    # the hint matches the vocabulary of the interview actually being taken.
    transcription_vocab_from_profile: bool = True

    # Partial / speculative STT interval — how often to run speculative
    # transcription on accumulated speech. Lower = faster feedback but more
    # CPU load. Override with KIYRA_REALTIME_PARTIAL_INTERVAL_S.
    realtime_partial_interval_s: float = 1.0  # was 1.2 — faster partial loop
    realtime_partial_speech_s: float = 1.0  # fire first partial after 1s of speech

    # ── Interview Listening Mode (Phase 5.4) ─────────────────────────────
    # The user, not VAD, decides where the question ends. A turn is only
    # submitted by an explicit SEND.
    turn_min_useful_chars: int = 8  # shorter transcripts cannot be sent
    turn_final_flush_timeout: float = 3.0  # bounded wait for the trailing STT

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
    # First-token deadline. A *warm* Ollama answers in ~2-3s, but a cold model
    # load (disk -> VRAM) can take ~20s. The old 10s cap aborted the load, which
    # unloaded the model again, so every turn paid the cold cost and produced
    # zero tokens. Keep this above a worst-case cold load; warm turns are still
    # fast because the first token arrives as soon as it is ready.
    realtime_first_token_timeout: int = 60
    answer_mode: str = "detailed"  # short | medium | detailed (real-time default: detailed)
    answer_max_tokens: int = 300  # Default cap (legacy / detailed path)

    # Per-question-type output caps, in TOKENS (the word targets live in
    # `realtime/prompts.py::_LENGTH_TARGETS`).
    #
    # These must be sized FROM the word target, not equal to it: prose runs
    # ~1.2 tokens/word and code ~1.6, so a cap below the band's top truncates
    # the answer mid-sentence — the candidate read aloud "...which required
    # dedicated engineering resources to maintain stability" with no ending.
    # Caps sit ~15% above the token cost of the word band's upper end so an
    # on-target answer always finishes, and only a runaway answer is clipped.
    answer_max_tokens_by_type: dict = {
        "conceptual": 270,   # 150-190 words -> ~230 tokens
        "coding": 520,       # 220-300 words -> ~490 tokens (code is denser)
        "behavioral": 270,   # 150-190 words -> ~230 tokens
        "debugging": 360,    # 180-230 words -> ~320 tokens
        "sql": 340,          # 160-210 words -> ~315 tokens
        "default": 270,
    }
    realtime_max_output_tokens: int = 300  # Realtime default cap

    # Standalone RAG defaults (kept for backward compatibility with callers
    # that don't pass per-type values). Per-type overrides live in the
    # _by_type dicts below.
    rag_top_k: int = 6  # Context chunks retrieved per question (default)
    rag_max_context_chars: int = 2500  # Cap RAG context (default)

    # Per-question-type RAG depth. Coding pulls more chunks (code examples,
    # multiple approaches); conceptual stays lean for speed.
    rag_top_k_by_type: dict = {
        "conceptual": 4,
        "coding": 8,
        "behavioral": 4,
        "debugging": 6,
        "sql": 5,
        "default": 6,
    }
    rag_max_context_chars: int = 2000  # Default cap
    rag_max_context_chars_by_type: dict = {
        "conceptual": 1600,
        "coding": 3200,   # more code snippets fit
        "behavioral": 1800,
        "debugging": 2400,
        "sql": 2200,
        "default": 2000,
    }
    rag_timeout: float = 3.0  # Hard ceiling for non-realtime RAG
    realtime_rag_timeout: float = 0.5  # Real-time ceiling — slow RAG must never block first answer

    # ── Hybrid retrieval / passage selection ────────────────────────────────
    # A document shorter than this is returned WHOLE (there is nothing to
    # select, and the complete answer is what the model needs). Longer
    # documents are cut into sentence-aligned passages and the relevant ones
    # are re-assembled verbatim.
    rag_short_doc_chars: int = 1200
    rag_passage_max_chars: int = 320   # Max size of one selected passage
    rag_passage_min_chars: int = 40    # Merge a smaller trailing passage back
    rag_passages_per_doc: int = 3      # Passages kept per selected document
    # MMR: 1.0 = pure relevance, 0.0 = pure diversity. 0.7 keeps the best
    # passage while still dropping near-duplicates.
    rag_mmr_lambda: float = 0.7
    realtime_top_k: int = 6  # Realtime retrieval depth (default)
    realtime_top_k_by_type: dict = {
        "conceptual": 4,
        "coding": 8,
        "behavioral": 4,
        "debugging": 6,
        "sql": 5,
        "default": 6,
    }
    realtime_max_context_chars: int = 2500  # Realtime RAG context budget (default)
    realtime_max_context_chars_by_type: dict = {
        "conceptual": 1600,
        "coding": 2800,
        "behavioral": 1800,
        "debugging": 2400,
        "sql": 2200,
        "default": 2200,
    }

    # Per-question-type temperature. Coding needs determinism (low), conceptual
    # can be a touch more expressive (higher).
    temperature_by_type: dict = {
        "conceptual": 0.5,
        "coding": 0.2,
        "behavioral": 0.6,
        "debugging": 0.3,
        "sql": 0.2,
        "default": 0.5,
    }

    # Keywords that signal a coding question — routes to the coding answer
    # pipeline (code blocks, complexity, edge cases).
    coding_keywords: list = [
        "implement", "write code", "function", "algorithm", "complexity",
        "big o", "time complexity", "space complexity", "code example",
        "code block", "snippet", "execute", "runtime", "compile", "debug",
        "stack overflow", "segmentation fault", "pointer", "recursion",
        "data structure", "array", "linked list", "tree", "graph", "heap",
        "sorting", "searching", "dynamic programming", "greedy", "backtracking",
    ]

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

    # ─── Authentication (desktop sign-in) ────────────────────────────────────
    # Real-world email-OTP auth for the desktop overlay. Accounts are created
    # with full name + phone + email; every sign-in is email + a 6-digit OTP.
    auth_enabled: bool = True                # False blocks new sign-ins (existing sessions still work)
    auth_otp_ttl_seconds: int = 600          # OTP lifetime (10 minutes)
    auth_otp_length: int = 6                 # digits in the OTP
    auth_otp_max_attempts: int = 5           # wrong guesses before the code dies
    auth_otp_resend_cooldown_seconds: int = 30
    auth_otp_max_per_hour: int = 6           # abuse ceiling per email
    auth_session_days: int = 30              # desktop session lifetime
    auth_otp_secret: str = ""                # HMAC key; random per process if unset
    auth_dev_otp: bool = False               # force OTP into the API response (dev only)

    # SMTP transport. When unset, the OTP is printed to the backend console and
    # returned as ``dev_code`` so a local install works with zero config.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_use_tls: bool = True
    smtp_use_ssl: bool = False
    smtp_timeout: float = 15.0

    # ─── Stealth Mode (Desktop App) ───────────────────────────────────────────
    stealth_window_alpha: float = 0.92  # 92% opacity (balances visibility + stealth)
    stealth_auto_hide_delay: int = 10  # Auto-hide answer after 10 seconds
    stealth_screen_exclusion: bool = True  # Use SetWindowDisplayAffinity on Windows

    # ─── RAG & Knowledge ──────────────────────────────────────────────────────
    knowledge_chunk_size: int = 512  # tokens per chunk
    knowledge_chunk_overlap: int = 50  # overlap tokens
    knowledge_similarity_threshold: float = 0.3  # Min similarity score

    # ─── GitHub Project Intelligence (Phase 5.2) ─────────────────────────────
    # Project-aware retrieval: the question analyzer marks project questions and
    # the GitHub retriever pulls repository evidence — never the whole repo and
    # never direct repo access for Bonsai. Token is read from GITHUB_TOKEN and
    # must never be logged.
    kiyra_github_enabled: bool = True
    kiyra_github_token: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("kiyra_github_token", "github_token"),
    )
    # Single-repo, env-only configuration (simplest production setup).
    kiyra_github_owner: str = ""
    kiyra_github_repo: str = ""
    kiyra_github_project_id: str = ""
    kiyra_github_project_name: str = ""
    kiyra_github_default_branch: str = "main"
    # Multi-project configuration: JSON array of project sources.
    kiyra_github_projects_file: str = "./data/github_projects.json"
    # Retrieval limits — keep Bonsai's context lean (default 6 chunks).
    kiyra_github_top_k: int = 6
    kiyra_github_snapshot_ttl_s: float = 600.0  # cache tree+blobs this long
    kiyra_github_http_timeout: float = 15.0
    kiyra_github_max_file_bytes: int = 300_000
    kiyra_github_max_files: int = 400  # hard cap on indexed files per repo
    kiyra_github_real_timeout: float = 3.0  # hard ceiling inside the live path

    # ─── Agent Caching ───────────────────────────────────────────────────────
    cache_answers: bool = True  # Cache similar question answers
    cache_ttl: int = 3600  # Cache TTL in seconds

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",")]


settings = Settings()