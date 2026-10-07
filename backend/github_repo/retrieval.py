"""Hybrid repository retrieval (Phase 5.2 §12).

Combines:
  A. semantic retrieval  (cosine on the local embedder — same one as RAG)
  B. keyword / identifier search (exact symbols, tech names like jwt/auth)
  C. file/path relevance   (auth/, middleware/, models/, routes/, …)

Scoring is blended (defaults 0.45 / 0.35 / 0.20) and the top-K (configurable,
default 6) is returned as metadata-preserving hits.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

import numpy as np

from config import settings
from github_repo.config import ProjectSource
from github_repo.indexer import IndexedChunk, RepositoryIndex
from github_repo.provider import GitHubRepositoryProvider
from knowledge.embeddings import embed_text

# Intent → keywords + path fragments that help with exact identifiers/filenames.
_INTENT_TERMS: dict[str, dict[str, list[str]]] = {
    "project_explanation": {
        "phrases": ["project", "app", "application", "system", "feature",
                    "flow", "usage", "works", "overview", "describe"],
        "paths": ["src", "app", "readme"],
    },
    "project_implementation": {
        "phrases": ["implement", "implementation", "setup", "create",
                    "build", "flow", "handles", "does it work", "how",
                    "configure", "logic", "pipeline"],
        "paths": ["src", "backend", "services", "core", "impl"],
    },
    "project_authentication_security": {
        "phrases": ["authentication", "auth", "login", "jwt", "token",
                    "password", "signin", "sign-in", "authorization",
                    "session", "cookie", "oauth", "bcrypt"],
        "paths": ["auth", "middleware", "security", "guard", "login"],
    },
    "project_database": {
        "phrases": ["database", "db", "schema", "model", "migration",
                    "query", "sequelize", "orm", "sql", "prisma", "knex"],
        "paths": ["models", "database", "db", "migrations", "schema",
                  "seeders", "prisma"],
    },
    "project_api": {
        "phrases": ["api", "rest", "endpoint", "route", "router",
                    "http", "request", "response", "controller", "handler"],
        "paths": ["routes", "router", "controllers", "api", "endpoints",
                  "handlers", "graphql"],
    },
    "project_frontend": {
        "phrases": ["frontend", "front-end", "ui", "component", "react",
                    "vue", "angular", "page", "store", "hook", "state"],
        "paths": ["frontend", "components", "pages", "src/views", "hooks",
                  "store", "actions", "reducers"],
    },
    "project_backend": {
        "phrases": ["backend", "back-end", "server", "service", "api",
                    "middleware", "express", "fastify", "flask", "django",
                    "spring", "nestjs", "worker", "socket"],
        "paths": ["backend", "server", "services", "middleware", "src/api"],
    },
    "project_architecture": {
        "phrases": ["architecture", "structure", "design", "architecture",
                    "layers", "monolith", "microservice", "module", "pattern"],
        "paths": ["src", "core", "architecture", "containers", "lib"],
    },
    "project_technology_choice": {
        "phrases": ["sequelize", "express", "react", "mysql", "postgres",
                    "jwt", "bcrypt", "redis", "docker", "typescript", "prisma"],
        "paths": ["package.json", "requirements.txt", "pyproject.toml",
                  "dockerfile", "docker-compose", ".env.example"],
    },
    "project_debugging": {
        "phrases": ["bug", "fix", "error", "crash", "exception", "failure",
                    "not working", "broken", "stack trace"],
        "paths": ["tests", "logs", "__tests__", "spec"],
    },
    "project_code": {
        "phrases": ["code", "implementation", "function", "class", "model"],
        "paths": [],
    },
}


@dataclass
class ProjectChunkHit:
    content: str
    metadata: dict
    score: float

    def to_metadata(self) -> dict:
        m = dict(self.metadata)
        m["score"] = round(float(self.score), 4)
        return m


class GitHubRetriever:
    """Retrieval entry point used by the session (single instance, lazy)."""

    def __init__(self, provider: GitHubRepositoryProvider,
                 index: Optional[RepositoryIndex] = None,
                 settings_=None):
        self.provider = provider
        self.index = index or RepositoryIndex()
        self._cfg = settings_ or settings

    # ── Index readiness ──────────────────────────────────────────────────

    def is_indexed(self, project: ProjectSource) -> bool:
        return self.index.has(project.project_id)

    def ensure_indexed(self, project: ProjectSource,
                       *, force_refresh: bool = False) -> None:
        """Populate the in-memory index (first question pays fetch cost)."""
        if self.is_indexed(project) and not force_refresh:
            return
        self.index.refresh(project.project_id, project, self.provider)

    # ── Hybrid ranking ───────────────────────────────────────────────────

    @staticmethod
    def _keyword_tokens(query: str) -> set[str]:
        words = re.findall(r"[a-z0-9_]{2,}", query.lower())
        stop = {
            "the", "and", "for", "with", "from", "into", "does", "did",
            "what", "why", "how", "when", "which", "where", "who", "my",
            "your", "this", "that", "have", "has", "was", "implement",
            "implementation", "explain", "show", "tell", "me", "about", "in",
            "on", "of", "to", "is", "are",
        }
        return {w for w in words if w not in stop and len(w) >= 3}

    def _intent_kw(self, intent: str) -> tuple[list[str], list[str]]:
        spec = _INTENT_TERMS.get(intent, {})
        return spec.get("phrases", []), spec.get("paths", [])

    @staticmethod
    def _cosine(a: list[float], b: list[float]) -> float:
        if not a or not b:
            return 0.0
        try:
            va = np.asarray(a, dtype=np.float64)
            vb = np.asarray(b, dtype=np.float64)
            if va.shape != vb.shape:
                return 0.0
            denom = float(np.linalg.norm(va) * np.linalg.norm(vb))
            if denom == 0.0:
                return 0.0
            return float(np.clip(va @ vb / denom, 0.0, 1.0))
        except Exception:
            return 0.0

    def retrieve(
        self,
        project: ProjectSource,
        query: str,
        *,
        intent: str = "",
        top_k: Optional[int] = None,
        min_score: float = 0.0,
    ) -> list[ProjectChunkHit]:
        """Hybrid top-K retrieval over the project's indexed chunks."""
        top_k = top_k or self._cfg.kiyra_github_top_k
        if not self.is_indexed(project):
            # Cold index: build it here (callers decide whether that is safe
            # inside the real-time budget; see ensure_indexed/warm).
            self.ensure_indexed(project)
        chunks = self.index.get(project.project_id)
        if not chunks:
            return []

        q_embedding = embed_text(query)
        q_words = self._keyword_tokens(query)
        phrases, paths = self._intent_kw(intent)
        intent_words = set(phrases)

        scored: list[tuple[float, int]] = []
        for ci, c in enumerate(chunks):
            sem = self._cosine(q_embedding, c.embedding or [])

            text_low = c.content.lower()
            keep = q_words & set(re.findall(r"[a-z0-9_]{2,}", text_low))
            kw = len(keep) / max(len(q_words), 1)

            # Intent keyword hits in content OR symbols.
            sym_low = " ".join(c.symbols).lower()
            intent_hits = [p for p in intent_words if p in text_low or p in sym_low]
            intent_kw = len(intent_hits) / max(len(intent_words), 1)

            path_parts = c.file_path.lower().split("/")
            path_hits = [p for p in paths if any(p in part for part in path_parts)]
            path_score = len(path_hits) / max(len(paths), 1)
            # Exact filename match against a q-word is a strong path signal.
            fname = path_parts[-1] if path_parts else ""
            if fname and (fname.replace(".", " ") in q_words
                          or fname in {w + ".js" for w in q_words}):
                path_score = max(path_score, 0.9)

            blend = (0.45 * sem + 0.35 * max(kw, intent_kw)
                     + 0.20 * path_score)
            if c.symbols:
                blend += 0.03  # symbol-bearing chunks are generally more useful
            scored.append((blend, ci))

        scored.sort(key=lambda x: (-x[0], x[1]))
        hits: list[ProjectChunkHit] = []
        for blend, ci in scored:
            if blend < min_score:
                continue
            c = chunks[ci]
            meta = c.to_metadata()
            meta["score"] = round(float(blend), 4)
            hits.append(ProjectChunkHit(content=c.content, metadata=meta,
                                        score=float(blend)))
            if len(hits) >= top_k:
                break
        return self._post_process(hits, top_k)

    # ── Deduplicate / diversify / compress (Phase 5.4 §8) ──────────────────

    # Two chunks are near-duplicates above this Jaccard overlap of their
    # significant tokens. Chunker overlaps are small, so this only fires on
    # genuinely repetitive evidence.
    _DEDUP_OVERLAP = 0.82
    # Below this many significant tokens a chunk carries too little signal to
    # compare safely — short chunks would all look identical ("def", "return")
    # and real evidence would be dropped.
    _DEDUP_MIN_TOKENS = 8
    # Never let one file monopolise the evidence budget.
    _MAX_PER_FILE = 2
    # Hard cap on the characters of one chunk handed to the model.
    _CHUNK_CHAR_BUDGET = 900

    @classmethod
    def _post_process(cls, hits: list[ProjectChunkHit],
                      top_k: int) -> list[ProjectChunkHit]:
        """Rank, deduplicate, cap per-file repetition, then compress.

        Sorting happens here so the guarantee "the best duplicate survives"
        holds no matter what order the caller passed. Score order is otherwise
        preserved; this only spends the context budget on *distinct* evidence.
        """
        kept: list[ProjectChunkHit] = []
        seen_tokens: list[set[str]] = []
        per_file: dict[str, int] = {}

        for hit in sorted(hits, key=lambda h: -float(h.score)):
            tokens = cls._content_tokens(hit.content)
            comparable = len(tokens) >= cls._DEDUP_MIN_TOKENS
            # Near-duplicate of something already selected?
            if comparable and any(
                    cls._jaccard(tokens, prev) >= cls._DEDUP_OVERLAP
                    for prev in seen_tokens):
                continue
            # Too many chunks from one file already?
            path = str(hit.metadata.get("file_path") or "")
            if per_file.get(path, 0) >= cls._MAX_PER_FILE:
                continue

            kept.append(hit)
            seen_tokens.append(tokens)
            per_file[path] = per_file.get(path, 0) + 1
            if len(kept) >= top_k:
                break

        for hit in kept:
            hit.content = cls._compress(hit.content)
        return kept

    @staticmethod
    def _content_tokens(text: str) -> set[str]:
        return set(re.findall(r"[a-z0-9_]{3,}", (text or "").lower()))

    @staticmethod
    def _jaccard(a: set[str], b: set[str]) -> float:
        if not a or not b:
            return 0.0
        inter = len(a & b)
        if not inter:
            return 0.0
        return inter / float(len(a | b))

    @classmethod
    def _compress(cls, content: str) -> str:
        """Trim one chunk to the per-chunk budget on a line boundary.

        The head of a chunk carries its signature (the function/file the chunk
        was cut around), so truncation keeps the most informative part.
        """
        text = (content or "").strip()
        if len(text) <= cls._CHUNK_CHAR_BUDGET:
            return text
        head = text[:cls._CHUNK_CHAR_BUDGET]
        cut = head.rfind("\n")
        if cut > cls._CHUNK_CHAR_BUDGET // 2:
            head = head[:cut]
        return head.rstrip() + "\n..."