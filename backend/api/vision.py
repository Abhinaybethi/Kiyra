"""Visual question pipeline — explicit user-uploaded images / screenshots.

Flow: image → validate → OCR → text → question extraction → RAG → Bonsai → answer.

Privacy-first: only images the user explicitly uploads are processed. OCR runs
entirely locally (RapidOCR ONNX). OCR failures are surfaced as clear recoverable
errors — never silently blank answers.
"""
from __future__ import annotations

import asyncio
import time

from fastapi import (
    APIRouter, Depends, File, HTTPException, UploadFile,
)
from sqlalchemy.orm import Session

from api.profile import get_profile
from api.schemas import VisualAnalysisResponse, VisualExtractResponse
from config import settings
from db.database import get_db
from db.models import TranscriptSegment
from knowledge.retrieval import retrieve_context_sync
from realtime.prompts import realtime_system, build_user_prompt
from realtime.question_detector import detect as detect_question
from ai.provider import get_provider, ensure_provider_ready
from vision.ocr import extract_text, extract_question

router = APIRouter(prefix="/api/vision", tags=["vision"])

ALLOWED_TYPES = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/bmp": "bmp",
}


async def _read_validated_image(file: UploadFile) -> bytes:
    """Read + validate an uploaded image. Raises 415/400/413 → shared by both
    vision endpoints so OCR-only and analyze paths enforce identical rules."""
    if file.content_type not in ALLOWED_TYPES:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported image type '{file.content_type}'. Use {', '.join(sorted(ALLOWED_TYPES))}.",
        )
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Empty file uploaded")
    if len(content) > settings.max_upload_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"Image exceeds {settings.max_upload_bytes // (1024 * 1024)}MB limit",
        )
    return content


@router.post("/extract", response_model=VisualExtractResponse)
async def extract_image_text(file: UploadFile = File(...)):
    """OCR an image and return ONLY the extracted text + question.

    Lightweight question-input stage for the overlay's screenshot flow: the
    overlay captures the screen, extracts the question here (local ONNX OCR),
    then feeds it through the SAME WebSocket real-time pipeline as voice/manual
    questions — never a separate answer engine. No provider/RAG/DB work.
    """
    content = await _read_validated_image(file)

    started = time.monotonic()
    try:
        ocr = await asyncio.to_thread(extract_text, content)
    except RuntimeError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"OCR failed: {e}")

    question = extract_question(ocr["text"])
    is_question, _conf = detect_question(question if question else ocr["text"])

    return VisualExtractResponse(
        extracted_text=ocr["text"],
        line_count=ocr.get("line_count", 0),
        question=question or (ocr["text"][:400] if ocr["text"] else ""),
        is_question=is_question,
        likely_code=ocr.get("likely_code", False),
        avg_confidence=ocr.get("avg_confidence"),
        ocr_ms=round((time.monotonic() - started) * 1000, 1),
    )


@router.post("/analyze", response_model=VisualAnalysisResponse)
async def analyze_image(
    file: UploadFile = File(...),
    session_id: int = None,
    db: Session = Depends(get_db),
):
    """OCR an explicitly uploaded image and produce a contextual Bonsai answer."""
    content = await _read_validated_image(file)

    # Ensure inference is reachable BEFORE wasting OCR work.
    provider = get_provider(db=db)
    await ensure_provider_ready(provider)

    profile = get_profile(db)

    # 1. OCR (local, ONNX)
    started = time.monotonic()
    try:
        ocr = await asyncio.to_thread(extract_text, content)
    except RuntimeError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"OCR failed: {e}")
    ocr_ms = (time.monotonic() - started) * 1000

    # 2. Question extraction + detection
    question = extract_question(ocr["text"])
    is_question, _conf = detect_question(question if question else ocr["text"])

    # 3. RAG (never blocks answering on failure)
    rag_used = False
    rag_ms = 0.0
    context = ""
    rag_metrics = {}
    try:
        context, rag_metrics = await asyncio.to_thread(
            retrieve_context_sync, profile.id, question or ocr["text"],
            top_k=settings.rag_top_k,
        )
        rag_used = bool(context)
        rag_ms = rag_metrics.get("embed_ms", 0.0) + rag_metrics.get("search_ms", 0.0)
    except Exception:
        rag_used = False

    # 4. Bonsai answer (short real-time mode)
    system = realtime_system(settings.answer_mode)
    image_note = (
        f"\n\nText extracted from an image the user provided:\n{ocr['text'][:1500]}"
    )
    user_prompt = build_user_prompt(
        question or "Analyze the content in the provided image.",
        profile.name,
        context,
    ) + image_note

    llm_started = time.monotonic()
    try:
        answer = await provider.complete(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user_prompt},
            ],
            stream=False,
            temperature=0.4,
            max_tokens=settings.answer_max_tokens * 2,
            request_id=f"vision:{profile.id}:{int(time.monotonic() * 1000)}",
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Answer generation failed: {e}")
    answer = answer.strip() if isinstance(answer, str) else ""

    latency_ms = int((time.monotonic() - started) * 1000)

    # 5. Keep a transcript segment for history
    if session_id:
        try:
            seg = TranscriptSegment(
                session_id=session_id,
                speaker="interviewer",
                content=f"[IMAGE] {question or ocr['text'][:400]}",
                timestamp_ms=int(time.time() * 1000),
                is_question=is_question,
                confidence=0.9,
            )
            db.add(seg)
            db.commit()
        except Exception:
            db.rollback()

    return VisualAnalysisResponse(
        extracted_text=ocr["text"],
        line_count=ocr["line_count"],
        question=question or ocr["text"][:400],
        is_question=is_question,
        likely_code=ocr["likely_code"],
        answer=answer,
        key_points=[],
        rag_used=rag_used,
        rag_ms=round(rag_ms, 1),
        latency_ms=latency_ms,
        model_used=provider.model_name,
        ocr_ms=round(ocr_ms, 1),
    )