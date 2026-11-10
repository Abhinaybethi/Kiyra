"""FastAPI application entry point."""
from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from config import settings
from db.database import Base, engine
from ai.provider import ProviderUnavailableError

# The backend's stdout is frequently a redirected pipe / log file. On Windows
# that defaults to a legacy code page (cp1252), so printing a transcript that
# contains any non-ASCII character (CJK, accented letters, emoji) raised
# UnicodeEncodeError — which propagated out of the WebSocket loop and tore the
# live session down. Force UTF-8 and never crash on an unencodable char.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: ensure dirs exist and create tables
    os.makedirs(settings.data_dir, exist_ok=True)
    os.makedirs(settings.upload_dir, exist_ok=True)
    os.makedirs(settings.chroma_dir, exist_ok=True)

    Base.metadata.create_all(bind=engine)

    # Ensure default user exists
    from db.database import SessionLocal
    from db.models import User, ModelConfiguration
    db = SessionLocal()
    try:
        if not db.query(User).filter_by(username="local").first():
            db.add(User(username="local"))
        if not db.query(ModelConfiguration).filter_by(is_active=True).first():
            db.add(ModelConfiguration())
        db.commit()
    finally:
        db.close()

    # Warm up the Whisper + embedding models off the critical path so the first
    # utterance in a live session doesn't eat the STT/RAG time budgets with
    # one-time model loading or downloads.
    import threading as _threading

    def _warm_whisper():
        try:
            from realtime.transcription import _get_model, _get_partial_model
            _get_model()
            # Also warm the dedicated partial lane so the first preview call
            # doesn't pay a one-time model-load behind its own lock.
            _get_partial_model()
        except Exception as e:
            print(f"[Warmup] whisper failed: {e}")

    def _warm_embeddings():
        try:
            from knowledge.embeddings import embed_text
            embed_text("warmup")
        except Exception as e:
            print(f"[Warmup] embeddings failed: {e}")
        # If the vector store was reset (corruption) or is simply empty while
        # the SQL knowledge base has chunks, rebuild the derived index so RAG
        # works without the user re-uploading anything. Runs after the
        # embedding model is warm above.
        if os.environ.get("TESTING"):
            return
        try:
            from knowledge.reindex import reindex_empty_collections
            reindex_empty_collections()
        except Exception as e:
            print(f"[Warmup] knowledge re-index failed: {e}")

    def _warm_llm():
        """Prime the local LLM so the FIRST live answer doesn't pay the cold
        model-load / prompt-eval cost (CPU-only llama can otherwise stall the
        first answer for tens of seconds). Best-effort: a missing server just
        logs and moves on — the real request still runs normally."""
        if os.environ.get("TESTING"):
            return  # never touch the network under the test suite
        try:
            import asyncio as _a
            from ai.provider import get_provider
            provider = get_provider()

            async def _go():
                try:
                    await provider.complete(
                        [{"role": "user", "content": "ping"}],
                        stream=False, max_tokens=1,
                    )
                except Exception:
                    pass

            _a.run(_go())
        except Exception as e:
            print(f"[Warmup] LLM failed: {e}")

    _threading.Thread(target=_warm_whisper, name="whisper-warmup", daemon=True).start()
    _threading.Thread(target=_warm_embeddings, name="embeddings-warmup", daemon=True).start()
    _threading.Thread(target=_warm_llm, name="llm-warmup", daemon=True).start()

    yield
    # Shutdown: nothing to clean up


app = FastAPI(
    title="AI Interview Platform API",
    version="1.0.0",
    description="Local-first AI interview assistance platform",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Global error handler (no stack trace leaks) ───────────────────────────

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    if settings.debug:
        import traceback
        detail = traceback.format_exc()
    else:
        detail = "An internal error occurred."
    return JSONResponse(status_code=500, content={"detail": detail})


@app.exception_handler(ProviderUnavailableError)
async def provider_unavailable_handler(request: Request, exc: ProviderUnavailableError):
    """Surface clear domain-level errors instead of deep connection failures."""
    return JSONResponse(status_code=503, content={"detail": str(exc)})


# ── Routes ─────────────────────────────────────────────────────────────────

from api.profile import router as profile_router
from api.resume import router as resume_router
from api.jobs import router as jobs_router
from api.knowledge import router as knowledge_router
from api.interviews import router as interviews_router
from api.analytics import router as analytics_router
from api.settings import router as settings_router
from api.vision import router as vision_router

app.include_router(profile_router)
app.include_router(resume_router)
app.include_router(jobs_router)
app.include_router(knowledge_router)
app.include_router(interviews_router)
app.include_router(analytics_router)
app.include_router(settings_router)
app.include_router(vision_router)


@app.get("/health")
async def health():
    return {"status": "ok", "version": "1.0.0"}


@app.get("/api/transcription/health")
async def transcription_health():
    from realtime.transcription import is_available
    available = is_available()
    return {
        "available": available,
        "model": settings.transcription_model,
        "message": None if available else "Run: pip install faster-whisper",
    }
