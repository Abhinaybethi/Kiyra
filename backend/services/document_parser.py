"""Document parsing service — PDF, DOCX, TXT with security checks."""
from __future__ import annotations

import io
import os
import re
from pathlib import Path

from config import settings


ALLOWED_EXTENSIONS = {".pdf", ".docx", ".txt"}
MAX_SIZE = settings.max_upload_bytes


def validate_file(filename: str, content: bytes) -> str:
    """Returns cleaned filename or raises ValueError."""
    path = Path(filename)
    ext = path.suffix.lower()

    if ext not in ALLOWED_EXTENSIONS:
        raise ValueError(f"Unsupported file type: {ext}. Allowed: {', '.join(ALLOWED_EXTENSIONS)}")

    if len(content) > MAX_SIZE:
        raise ValueError(f"File too large: {len(content)} bytes. Max: {MAX_SIZE} bytes")

    # Path traversal protection
    safe_name = re.sub(r"[^a-zA-Z0-9._\-]", "_", path.name)
    return safe_name


def extract_text(filename: str, content: bytes) -> str:
    """Extract plain text from PDF, DOCX, or TXT."""
    ext = Path(filename).suffix.lower()

    if ext == ".txt":
        return content.decode("utf-8", errors="replace").strip()

    elif ext == ".pdf":
        return _extract_pdf(content)

    elif ext == ".docx":
        return _extract_docx(content)

    raise ValueError(f"Unsupported format: {ext}")


def _extract_pdf(content: bytes) -> str:
    from pdfminer.high_level import extract_text as pdf_extract
    from pdfminer.layout import LAParams

    try:
        text = pdf_extract(io.BytesIO(content), laparams=LAParams())
        return _clean_text(text or "")
    except Exception as e:
        raise ValueError(f"Failed to parse PDF: {e}")


def _extract_docx(content: bytes) -> str:
    try:
        from docx import Document
        doc = Document(io.BytesIO(content))
        paragraphs = [para.text for para in doc.paragraphs if para.text.strip()]
        return _clean_text("\n".join(paragraphs))
    except Exception as e:
        raise ValueError(f"Failed to parse DOCX: {e}")


def _clean_text(text: str) -> str:
    # Collapse excessive whitespace
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


async def save_upload(filename: str, content: bytes) -> str:
    """Save file to uploads dir and return path."""
    import aiofiles
    upload_dir = Path(settings.upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)
    safe_name = validate_file(filename, content)
    file_path = upload_dir / safe_name

    async with aiofiles.open(file_path, "wb") as f:
        await f.write(content)

    return str(file_path)
