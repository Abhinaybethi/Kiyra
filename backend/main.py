"""FastAPI application entry point."""
from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
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

    if settings.dev_auth_bypass:
        print("[AUTH] WARNING: KIYRA_DEV_AUTH_BYPASS is enabled — every API "
              "and WebSocket route accepts unauthenticated requests. "
              "Local development only; never expose this backend publicly.")

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
        model-load / prompt-eval cost (a cold Ollama load can take ~20s, longer
        than any sane first-token deadline). Best-effort: a missing server just
        logs and moves on — the real request still runs normally.

        The provider MUST be resolved through the DB (`get_provider(db=...)`):
        the active `ModelConfiguration` row is authoritative, and warming the
        env-default provider (e.g. Bonsai) left the real model (Ollama) cold.
        """
        if os.environ.get("TESTING"):
            return  # never touch the network under the test suite
        db = None
        try:
            import asyncio as _a
            from ai.provider import get_provider
            db = SessionLocal()
            provider = get_provider(db=db)

            async def _go():
                try:
                    if hasattr(provider, "warmup"):
                        ok = await provider.warmup()
                        print(f"[Warmup] LLM ready={ok} model={provider.model_name}")
                        return
                    await provider.complete(
                        [{"role": "user", "content": "ping"}],
                        stream=False, max_tokens=1,
                    )
                except Exception as e:
                    print(f"[Warmup] LLM request failed: {e}")

            _a.run(_go())
        except Exception as e:
            print(f"[Warmup] LLM failed: {e}")
        finally:
            try:
                if db is not None:
                    db.close()
            except Exception:
                pass

    def _warn_gpu_power():
        """Tell the user when the GPU is power-capped (battery).

        A laptop GPU on battery runs at roughly half the throughput of AC
        power, which looks exactly like a broken/slow model. Saying it once
        at startup saves rediscovering it every time answers feel sluggish.
        """
        try:
            from ai.gpu_status import gpu_power_state
            state = gpu_power_state()
            if state.get("on_battery"):
                print("[GPU] WARNING: %s (battery %s%%, power plan %r)"
                      % (state.get("note"), state.get("percent"),
                         state.get("scheme")))
            elif state.get("note"):
                print("[GPU] WARNING: %s (power plan %r)"
                      % (state.get("note"), state.get("scheme")))
            else:
                print("[GPU] power=%s on_battery=%s"
                      % (state.get("scheme"), state.get("on_battery")))
        except Exception as e:
            print(f"[GPU] power state unavailable: {e}")

    def _tune_gpu():
        """Put the local model on the fast GPU.

        On a hybrid-graphics laptop the dGPU runs at a reduced TGP unless
        Windows is told to prefer it (measured: 30 W -> 60 W, which is worth
        ~20-30% of the answer latency). The preference is written to the
        per-user registry, so it survives restarts and shows up in
        Windows Settings -> Display -> Graphics as "High performance".

        Best-effort: a failure here must never stop the backend.
        """
        if os.environ.get("TESTING"):
            return
        try:
            from ai.gpu_tuning import ensure_high_performance_gpu
            result = ensure_high_performance_gpu()
            if result.get("applied"):
                print("[GPU] assigned to high-performance GPU: %s"
                      % ", ".join(result["applied"]))
            if result.get("failed"):
                print("[GPU] could not set the GPU preference for: %s"
                      % ", ".join(result["failed"]))
            if result.get("plan") and not result.get("plan_is_fast"):
                print("[GPU] NOTE: power plan is %r — a performance plan "
                      "(Best performance) makes answers ~20%% faster"
                      % result["plan"])
        except Exception as e:
            print(f"[GPU] tuning skipped: {e}")

    _threading.Thread(target=_warm_whisper, name="whisper-warmup", daemon=True).start()
    _threading.Thread(target=_warm_embeddings, name="embeddings-warmup", daemon=True).start()
    _threading.Thread(target=_warm_llm, name="llm-warmup", daemon=True).start()
    _threading.Thread(target=_warn_gpu_power, name="gpu-power-check", daemon=True).start()
    _threading.Thread(target=_tune_gpu, name="gpu-tune", daemon=True).start()

    yield
    # Shutdown: nothing to clean up


app = FastAPI(
    title="AI Interview Platform API",
    version="1.0.0",
    description="Local-first AI interview assistance platform",
    lifespan=lifespan,
    # The route surface is internal. No unauthenticated schema or docs pages.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
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

from api.deps import require_account
from api.profile import router as profile_router
from api.auth import router as auth_router
from api.resume import router as resume_router
from api.jobs import router as jobs_router
from api.knowledge import router as knowledge_router
from api.interviews import (
    router as interviews_router,
    websocket_router as interviews_ws_router,
)
from api.analytics import router as analytics_router
from api.settings import router as settings_router
from api.vision import router as vision_router

# Every application route requires a valid session. Only the auth router
# (register/login/resend/verify/status) and /health stay public. The live
# WebSocket is mounted separately with its own dependency so the HTTP-only
# header guard never rejects a browser's ?token connection.
_PROTECTED_ROUTERS = (
    profile_router, resume_router, jobs_router, knowledge_router,
    interviews_router, analytics_router, settings_router, vision_router,
)
app.include_router(auth_router)
for _router in _PROTECTED_ROUTERS:
    app.include_router(_router, dependencies=[Depends(require_account)])
app.include_router(interviews_ws_router)


@app.get("/health")
async def health():
    # The GPU power state is included because a battery-capped laptop GPU
    # halves inference throughput — the single most common "answers got slow"
    # cause, and invisible from the API response alone.
    from ai.gpu_status import gpu_power_state
    gpu = gpu_power_state()
    # …plus whether the local model is actually assigned to the fast GPU.
    # On a hybrid-graphics laptop the dGPU otherwise runs at a reduced TGP
    # (30 W instead of 60 W here), which costs ~20-30% of the latency.
    try:
        from ai.gpu_tuning import ensure_high_performance_gpu
        tuning = ensure_high_performance_gpu()
        gpu["tuning"] = {
            "fast_gpu_assigned": not tuning.get("failed"),
            "plan_is_fast": tuning.get("plan_is_fast", False),
            "plan": tuning.get("plan", ""),
        }
    except Exception:
        pass
    return {
        "status": "ok",
        "version": "1.0.0",
        "gpu": gpu,
    }


@app.get("/api/transcription/health", dependencies=[Depends(require_account)])
async def transcription_health():
    from realtime.transcription import is_available
    available = is_available()
    return {
        "available": available,
        "model": settings.transcription_model,
        "message": None if available else "Run: pip install faster-whisper",
    }
