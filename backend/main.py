"""FastAPI application entry point."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from config import settings
from db.database import Base, engine


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


# ── Routes ─────────────────────────────────────────────────────────────────

from api.profile import router as profile_router
from api.resume import router as resume_router
from api.jobs import router as jobs_router
from api.knowledge import router as knowledge_router
from api.interviews import router as interviews_router
from api.analytics import router as analytics_router
from api.settings import router as settings_router

app.include_router(profile_router)
app.include_router(resume_router)
app.include_router(jobs_router)
app.include_router(knowledge_router)
app.include_router(interviews_router)
app.include_router(analytics_router)
app.include_router(settings_router)


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
