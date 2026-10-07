"""Repository indexing pipeline (Phase 5.2 §9) — turns a GitHub snapshot into
searchable, metadata-preserving code chunks WITHOUT sending the repo to Bonsai.

- Fetch the git tree once (provider caches it) and read only text blobs.
- Exclude generated/dependency/secret paths (never index secrets).
- Chunk semantically per language (fallback: bounded windows).
- Compute embeddings with the SAME local embedder used by normal RAG, so
  project retrieval ranks alongside resume knowledge without a new store.
"""
from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass, field
from typing import Optional

from config import settings
from github_repo.chunker import Chunk, chunk_source, detect_language
from github_repo.config import ProjectSource
from github_repo.provider import GitHubRepositoryProvider
from knowledge.embeddings import embed_batch


class IndexedChunk(Chunk):
    """Chunk plus its (precomputed) embedding vector."""
    embedding: Optional[list[float]] = None


_EXCLUDED_DIRS = {
    "node_modules", ".venv", "venv", "__pycache__", ".git", ".github",
    "dist", "build", ".next", "coverage", ".cache", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "target", "out", ".idea", ".vscode",
    "egg-info", ".eggs", "vendor", "bower_components",
}

_EXCLUDED_FILENAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
    "pipfile.lock", "composer.lock", "gemfile.lock", "cargo.lock",
}

_SECRET_PATTERNS = (
    re.compile(r"\.env(\.local|\.prod|\.dev|\.development|\.production)?$", re.IGNORECASE),
    re.compile(r"\.(pem|key|p12|pfx|keystore|jks)$", re.IGNORECASE),
    re.compile(r"(credentials|secret|secrets|\.pwd|id_rsa|ca-cert|\.kubeconfig)", re.IGNORECASE),
)


def _excluded(path: str, settings_=None) -> bool:
    """True when a tree path should never be fetched/indexed."""
    cfg = settings_ or settings
    parts = [p for p in path.replace("\\", "/").split("/") if p]
    for part in parts[:-1]:
        if part in _EXCLUDED_DIRS:
            return True
    name = parts[-1] if parts else path
    if name in _EXCLUDED_FILENAMES:
        return True
    # search (not match): the patterns are suffix-anchored, so they must be
    # able to match anywhere in the filename ("config/keys.pem" → "keys.pem").
    if any(p.search(name) for p in _SECRET_PATTERNS):
        return True
    if re.search(r"\.(pyc|pyo|class|o|obj|wasm|min\.js)$", name, re.IGNORECASE):
        return True
    if name.endswith(".lock"):
        return True
    return False


def _textlike(path: str) -> bool:
    if re.search(r"\.(py|js|mjs|cjs|ts|tsx|jsx|java|sql|go|rs|c|cpp|h|hpp|cs|"
                 r"rb|php|swift|kt|scala|html|css|scss|json|ya?ml|toml|xml|md|"
                 r"sh|bash|zsh|ps1|bat|dockerfile|ini|cfg|conf|properties|csv|"
                 r"txt|gradle)$", path, re.IGNORECASE):
        return True
    return not re.search(r"\.(png|jpe?g|gif|webp|bmp|ico|ogg|mp3|mp4|wav|pdf|"
                         r"zip|gz|tar|jar|exe|dll|so|dylib|woff2?|ttf|eot|ico)$",
                         path, re.IGNORECASE)


def index_repository(
    project: ProjectSource,
    provider: GitHubRepositoryProvider,
    *,
    force_refresh: bool = False,
    max_files: Optional[int] = None,
    max_bytes: Optional[int] = None,
) -> list[IndexedChunk]:
    """Index one repository into a list of embedded, filtered chunks."""
    cfg = settings
    max_files = max_files or cfg.kiyra_github_max_files
    max_bytes = max_bytes or cfg.kiyra_github_max_file_bytes

    tree = provider.get_repository_tree(project, force_refresh=force_refresh)
    tree = sorted(
        (e for e in tree if e.get("type") == "blob" and e.get("path", "").count("/") < 32),
        key=lambda e: e.get("path") or "",
    )

    chunks: list[Chunk] = []
    fetched = 0
    for entry in tree[:max_files * 3]:
        path = entry.get("path") or ""
        if fetched >= max_files:
            break
        if _excluded(path):
            continue
        if not _textlike(path):
            continue
        size = entry.get("size") or 0
        if size > max_bytes:
            continue
        if size <= 0:
            continue
        raw = provider.read_file(project, path)
        if not raw:
            continue
        try:
            text = raw.decode("utf-8", errors="replace")
        except Exception:
            continue
        if len(text) > max_bytes:
            continue
        lang = detect_language(path)
        fetched += 1
        for c in chunk_source(
            text, path=path, language=lang,
            project_id=project.project_id,
            repository=project.full_name,
        ):
            chunks.append(c)

    # Embed in one batched call using the same embedder the rest of RAG uses.
    embeddings = embed_batch([c.content for c in chunks])
    indexed: list[IndexedChunk] = []
    for c, emb in zip(chunks, embeddings):
        idx = IndexedChunk(
            project_id=c.project_id, repository=c.repository,
            file_path=c.file_path, language=c.language, content=c.content,
            start_line=c.start_line, end_line=c.end_line, symbols=c.symbols,
        )
        idx.embedding = emb if emb else None
        indexed.append(idx)
    return indexed


class RepositoryIndex:
    """Process-wide index cache per project (no separate vector DB)."""

    def __init__(self, settings_=None):
        self._lock = threading.Lock()
        self._indices: dict[str, list[IndexedChunk]] = {}
        self._cfg = settings_ or settings

    def has(self, project_id: str) -> bool:
        with self._lock:
            return project_id in self._indices

    def get(self, project_id: str) -> list[IndexedChunk]:
        with self._lock:
            return list(self._indices.get(project_id, []))

    def set(self, project_id: str, chunks: list[IndexedChunk]) -> None:
        with self._lock:
            self._indices[project_id] = list(chunks)

    def refresh(self, project_id: str,
                project: ProjectSource,
                provider: GitHubRepositoryProvider) -> list[IndexedChunk]:
        chunks = index_repository(project, provider)
        self.set(project_id, chunks)
        return chunks