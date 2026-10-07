"""Isolated GitHub REST client — the ONLY module that talks to GitHub.

Abstraction surface (Phase 5.2 §7):
- get_repository_tree()      → snapshot of paths + types + sizes
- read_file(path)            → raw file content (bytes)
- search_code(query)         → best-effort GitHub code search (token only)
- get_file_metadata(path)    → size / path / type for one file
- get_relevant_files(query)  → hybrid retrieval entry point (see retrieval.py)

Security rules:
- Token is read from settings (GITHUB_TOKEN) and NEVER logged.
- On config/auth failure a clear GitHubConfigError is raised — never a guess.
- 404 auth failures are surfaced as config errors, not fabricated content.
"""
from __future__ import annotations

import asyncio
import re
import threading
from typing import Optional

import httpx

from config import settings
from github_repo.config import (
    GitHubConfigError,
    ProjectSource,
    configured_projects,
)

_ACCEPT_RAW = "application/vnd.github.raw+json"
_API = "https://api.github.com"


def _headers(token: Optional[str] = None) -> dict[str, str]:
    _token = token or settings.kiyra_github_token
    headers = {
        "Accept": _ACCEPT_RAW,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if _token:
        headers["Authorization"] = f"Bearer {_token}"
    return headers


def _path_is_binary(path: str) -> bool:
    return re.search(
        r"\.(png|jpe?g|gif|webp|bmp|ico|ogg|mp3|mp4|pdf|zip|gz|tar|jar|"
        r"exe|dll|so|dylib|woff2?|ttf|eot|pyc|class)$", path, re.IGNORECASE,
    ) is not None


logger = None  # optional future hook; we print only safe, non-secret lines


class GitHubRepositoryProvider:
    """One provider can serve every configured repository."""

    def __init__(self, settings_=None):
        self._cfg = settings_ or settings
        self._client = httpx.Client(timeout=self._cfg.kiyra_github_http_timeout)
        self._lock = threading.Lock()
        # repo snapshot cache: key = f"{owner}/{repo}@{branch}"
        self._snapshots: dict[str, dict] = {}

    # ── Auth / configuration ─────────────────────────────────────────────

    def _token(self) -> Optional[str]:
        return self._cfg.kiyra_github_token or None

    def _require_auth(self) -> None:
        if not self._cfg.kiyra_github_token:
            raise GitHubConfigError(
                "GitHub retrieval needs a GITHUB_TOKEN environment variable. "
                "Set it before asking project questions."
            )

    def _repo_cache_key(self, project: ProjectSource) -> str:
        return f"{project.github_owner}/{project.github_repo}@{project.default_branch}"

    # ── Tree / files ─────────────────────────────────────────────────────

    def get_repository_tree(
        self, project: ProjectSource, *, force_refresh: bool = False,
        sha: Optional[str] = None,
    ) -> list[dict]:
        """Recursive git tree for the project's default branch (cached)."""
        key = self._repo_cache_key(project)
        if not force_refresh:
            cached = self._snapshot_cached(key)
            if cached is not None:
                return cached
        url = f"{_API}/repos/{project.github_owner}/{project.github_repo}/git/trees/{sha or project.default_branch}?recursive=1"
        resp = self._get(url)
        self._handle_api_error(resp, project)
        data = resp.json()
        entries = data.get("tree", [])
        self._set_snapshot(key, entries)
        return entries

    def _snapshot_cached(self, key: str) -> Optional[list[dict]]:
        with self._lock:
            snap = self._snapshots.get(key)
        if snap is None:
            return None
        import time as _t
        ttl = self._cfg.kiyra_github_snapshot_ttl_s
        if ttl > 0 and (_t.monotonic() - snap["at"]) > ttl:
            self._invalidate_snapshot(key)
            return None
        return snap["entries"]

    def _set_snapshot(self, key: str, entries: list[dict]) -> None:
        import time as _t
        with self._lock:
            self._snapshots[key] = {"entries": entries, "at": _t.monotonic()}

    def _invalidate_snapshot(self, key: str) -> None:
        with self._lock:
            self._snapshots.pop(key, None)

    def _get(self, url: str) -> httpx.Response:
        try:
            resp = self._client.get(url, headers=_headers(self._token()))
            return resp
        except httpx.HTTPError as e:
            raise GitHubConfigError(f"GitHub request failed: {e}") from e

    def _handle_api_error(self, resp: httpx.Response, project: ProjectSource) -> None:
        if resp.status_code in (200, 206):
            return
        detail = resp.text[:200] if resp.text else ""
        if resp.status_code in (401, 403):
            raise GitHubConfigError(
                "GitHub authentication failed (401/403). Check GITHUB_TOKEN "
                "scope (repo read) for %s." % project.full_name
            )
        if resp.status_code == 404:
            raise GitHubConfigError(
                "Repository %s not found or not accessible with the current "
                "token." % project.full_name
            )
        raise GitHubConfigError(
            "GitHub API error %s for %s: %s"
            % (resp.status_code, project.full_name, detail or "unknown")
        )

    def read_file(self, project: ProjectSource, path: str) -> bytes:
        """Read one raw file. Never returns secrets-beyond-file-content concern:
        the indexer decides what to keep — this just fetches."""
        if _path_is_binary(path):
            return b""
        url = f"{_API}/repos/{project.github_owner}/{project.github_repo}/contents/{path}"
        resp = self._get(url)
        self._handle_api_error(resp, project)
        return resp.content

    def get_file_metadata(self, project: ProjectSource, path: str) -> Optional[dict]:
        """Metadata for one path (from the cached tree, no extra request)."""
        for e in self.get_repository_tree(project):
            if e.get("path") == path:
                return {
                    "path": path,
                    "type": e.get("type"),
                    "size": e.get("size"),
                    "sha": e.get("sha"),
                }
        return None

    def get_commit_history(self, project: ProjectSource, path: str, per_page: int = 5) -> list[dict]:
        """Recent commits touching a path (optional, best-effort)."""
        url = (
            f"{_API}/repos/{project.github_owner}/{project.github_repo}/commits"
            f"?path={path}&per_page={per_page}"
        )
        resp = self._get(url)
        if resp.status_code != 200:
            return []
        commits = resp.json()
        return [
            {"sha": c.get("sha", "")[:12], "message":
             (c.get("commit") or {}).get("message", "")[:200]}
            for c in commits if isinstance(c, dict)
        ]

    def search_code(self, query: str) -> list[dict]:
        """GitHub code search (requires a token; best-effort passthrough)."""
        self._require_auth()
        url = f"{_API}/search/code?q={query}"
        resp = self._get(url)
        if resp.status_code != 200:
            return []
        data = resp.json()
        return [
            {"path": (i.get("path") or ""), "repo":
             (((i.get("repository") or {}).get("full_name")) or "")}
            for i in data.get("items", [])
        ]

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass


def create_if_configured() -> Optional[GitHubRepositoryProvider]:
    """Return a provider only when at least one project is configured. Raises
    GitHubConfigError when projects exist but the token is missing and the
    caller asked to use them; None when GitHub is simply not configured."""
    projects = configured_projects()
    if not projects:
        return None
    if not settings.kiyra_github_token:
        raise GitHubConfigError(
            "GitHub retrieval requires a GITHUB_TOKEN environment variable "
            "(unauthenticated repos/code search are unavailable)."
        )
    return GitHubRepositoryProvider()