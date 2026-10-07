"""GitHub-aware project retrieval for Kiyra (Phase 5.2).

Pipeline: question → analyzer.project_related → project selection →
GitHubRepositoryProvider → indexer/chunker (filtered, secret-safe, embedded)
→ hybrid retrieval (semantic + keyword + path) → PROJECT EVIDENCE block
for the ContextBuilder → Bonsai (which never touches GitHub directly).
"""

__all__ = [
    "GitHubRepositoryProvider",
    "GitHubRetriever",
    "RepositoryIndex",
    "index_repository",
    "chunk_source",
    "ProjectSource",
    "ProjectMatch",
    "resolve_project",
    "configured_projects",
    "evidence_block",
    "evidence_note",
    "GitHubConfigError",
]


from github_repo.config import (
    GitHubConfigError,
    ProjectMatch,
    ProjectSource,
    configured_projects,
    project_display_names,
    resolve_project,
)
from github_repo.chunker import Chunk, chunk_source, detect_language
from github_repo.indexer import RepositoryIndex, index_repository
from github_repo.grounding import evidence_block, evidence_note
from github_repo.provider import GitHubRepositoryProvider, create_if_configured
from github_repo.retrieval import GitHubRetriever, ProjectChunkHit