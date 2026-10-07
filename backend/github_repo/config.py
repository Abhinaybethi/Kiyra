"""Project source configuration for GitHub-aware retrieval (Phase 5.2).

Multiple repositories are supported. A project is described by:

- project_id      stable identifier used across retrieval metadata
- project_name    display name ("Bus Booking System")
- github_owner    GitHub owner/org
- github_repo     repository name
- default_branch  default branch to index (default "main")

Configuration precedence:
1. ``KIYRA_GITHUB_PROJECTS_FILE`` — JSON array (multi-project). Example:
   [{"project_id": "bus-booking", "project_name": "Bus Booking System",
     "github_owner": "me", "github_repo": "bus-booking", "default_branch": "main"}]
2. Single project from env: GITHUB_PROJECT_ID / GITHUB_OWNER / GITHUB_REPO /
   GITHUB_PROJECT_NAME / GITHUB_DEFAULT_BRANCH.

Authentication: GITHUB_TOKEN (see provider). Secrets are never logged here.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Optional

from config import settings


@dataclass(frozen=True)
class ProjectSource:
    project_id: str
    project_name: str
    github_owner: str
    github_repo: str
    default_branch: str = "main"

    @property
    def full_name(self) -> str:
        return f"{self.github_owner}/{self.github_repo}"


@dataclass(frozen=True)
class ProjectMatch:
    project: ProjectSource
    confidence: float
    matched_by: str  # "explicit-name" | "active-project" | "single-project"


class GitHubConfigError(RuntimeError):
    """Raised when GitHub access is requested but misconfigured."""


def _token_present() -> bool:
    return bool((settings.kiyra_github_token or "").strip())


def configured_projects() -> list[ProjectSource]:
    """Load all configured project sources (env single + JSON file)."""
    projects: dict[str, ProjectSource] = {}

    def _add(p: ProjectSource) -> None:
        if p.github_owner and p.github_repo:
            projects[p.project_id] = p

    # 1) JSON file (multi-project).
    path = settings.kiyra_github_projects_file
    try:
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            if isinstance(raw, dict):
                raw = [raw]
            for item in raw or []:
                if not isinstance(item, dict):
                    continue
                _add(ProjectSource(
                    project_id=str(item.get("project_id") or "").strip(),
                    project_name=str(item.get("project_name") or item.get("project_id") or "").strip(),
                    github_owner=str(item.get("github_owner") or "").strip(),
                    github_repo=str(item.get("github_repo") or "").strip(),
                    default_branch=str(item.get("default_branch") or "main").strip(),
                ))
    except Exception as e:
        raise GitHubConfigError(
            f"Failed to read GitHub projects config file {path}: {e}"
        ) from e

    # 2) Env single project (merged only if not already present).
    if settings.kiyra_github_owner and settings.kiyra_github_repo:
        pid = settings.kiyra_github_project_id or settings.kiyra_github_repo
        _add(ProjectSource(
            project_id=pid,
            project_name=settings.kiyra_github_project_name or pid,
            github_owner=settings.kiyra_github_owner,
            github_repo=settings.kiyra_github_repo,
            default_branch=settings.kiyra_github_default_branch or "main",
        ))

    return list(projects.values())


def project_display_names() -> list[str]:
    """Human project names for detection (e.g. 'Kiyra', 'Bus Booking System')."""
    out: list[str] = []
    for p in configured_projects():
        if p.project_name and p.project_name not in out:
            out.append(p.project_name)
        if p.project_id and p.project_id not in out:
            out.append(p.project_id)
        if p.github_repo and p.github_repo not in out:
            out.append(p.github_repo)
    return out


def _matches_name(question: str, names: list[str]) -> Optional[str]:
    """Word-bounded, case-insensitive project-name detection in a question."""
    norm = re.sub(r"\s+", " ", (question or "").lower())
    for name in names:
        alias = name.lower()
        if alias and len(alias) >= 3 and f" {alias} " in f" {norm} ":
            return name
    return None


def resolve_project(
    question: str,
    *,
    active_project_id: Optional[str] = None,
    active_topic: str = "",
    conversation_entities: Optional[list[str]] = None,
) -> Optional[ProjectMatch]:
    """Select a project for a question using the documented priority:

    1. Explicit project name in the current question.
    2. Active project from session memory (``active_project_id``).
    3. Project associated with the current conversation/topic/entities.
    4. Single configured project (safe default).
    5. Otherwise None → the caller must NOT guess between multiple projects.

    Returns None when the question has no project signal (generic question) so
    callers only touch GitHub when the analyzer already marked it project-based.
    """
    if not question:
        return None
    projects = configured_projects()
    if not projects:
        return None

    explicit = _matches_name(question, project_display_names())
    if explicit:
        hit = next(
            (p for p in projects if explicit.lower() in (
                p.project_name.lower(), p.project_id.lower(), p.github_repo.lower())),
            None,
        )
        if hit:
            return ProjectMatch(hit, 1.0, "explicit-name")

    if active_project_id:
        hit = next(
            (p for p in projects if p.project_id == active_project_id
             or p.github_repo == active_project_id
             or p.project_name.lower() == str(active_project_id).lower()),
            None,
        )
        if hit:
            return ProjectMatch(hit, 0.9, "active-project")

    for cand in (active_topic, *(conversation_entities or [])):
        if not cand:
            continue
        hit = next(
            (p for p in projects if cand.lower() in (
                p.project_name.lower(), p.project_id.lower(), p.github_repo.lower())),
            None,
        )
        if hit:
            return ProjectMatch(hit, 0.8, "conversation-topic")

    if len(projects) == 1:
        return ProjectMatch(projects[0], 0.7, "single-project")

    return None  # ambiguous — never guess.


def require_configured() -> None:
    """Raise a clear config error when no project is configured (auth included)."""
    if not configured_projects():
        raise GitHubConfigError(
            "No GitHub projects configured. Set GITHUB_OWNER/GITHUB_REPO and a "
            "GITHUB_TOKEN, or point KIYRA_GITHUB_PROJECTS_FILE at a JSON file."
        )
    if not _token_present():
        raise GitHubConfigError(
            "GitHub retrieval requires a GITHUB_TOKEN environment variable "
            "(private repositories and the code-search API are not available "
            "without authentication)."
        )