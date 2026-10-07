"""Unit tests for the Phase-5.2 GitHub-aware project retrieval package.

Covered: project resolution priority, chunking semantics, index filtering
(never index secrets/lockfiles), provider snapshot caching (no double fetch),
hybrid retrieval ranking, evidence formatting, and the disabled-path safety.
"""
from __future__ import annotations

import types

import pytest

os_env_import_guard = None  # placeholder — real environments will be handled below.


def _make_settings(**overrides):
    base = dict(
        kiyra_github_enabled=True,
        kiyra_github_token="ghp_test",
        kiyra_github_owner="",
        kiyra_github_repo="",
        kiyra_github_project_id="",
        kiyra_github_project_name="",
        kiyra_github_default_branch="main",
        kiyra_github_projects_file="",
        kiyra_github_top_k=6,
        kiyra_github_snapshot_ttl_s=600.0,
        kiyra_github_http_timeout=15.0,
        kiyra_github_max_file_bytes=300_000,
        kiyra_github_max_files=400,
        kiyra_github_real_timeout=3.0,
        knowledge_chunk_size=512,
        knowledge_chunk_overlap=50,
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


from github_repo.chunker import chunk_source, detect_language
from github_repo.config import (
    GitHubConfigError,
    ProjectSource,
    resolve_project,
)
from github_repo.grounding import evidence_block, evidence_note
from github_repo.indexer import _excluded, _textlike
from github_repo.provider import GitHubRepositoryProvider, create_if_configured
from github_repo.retrieval import GitHubRetriever, _INTENT_TERMS


# ── Project resolution ──────────────────────────────────────────────────


def test_resolve_project_explicit_name(monkeypatch):
    projects = [ProjectSource("p1", "ShopSense", "me", "shopsense"),
                ProjectSource("p2", "Bus Booking", "me", "bus-booking")]
    monkeypatch.setattr("github_repo.config.configured_projects",
                        lambda: projects)
    monkeypatch.setattr("github_repo.config.project_display_names",
                        lambda: ["ShopSense", "shopsense", "Bus Booking", "bus-booking"])
    match = resolve_project("Why did I build ShopSense this way?")
    assert match is not None and match.matched_by == "explicit-name"
    assert match.project.project_id == "p1"


def test_resolve_project_active_project(monkeypatch):
    projects = [ProjectSource("p1", "ShopSense", "me", "shopsense"),
                ProjectSource("p2", "Bus Booking", "me", "bus-booking")]
    monkeypatch.setattr("github_repo.config.configured_projects",
                        lambda: projects)
    monkeypatch.setattr("github_repo.config.project_display_names", lambda: [])
    match = resolve_project(
        "Explain the database schema", active_project_id="p2")
    assert match is not None and match.matched_by == "active-project"
    assert match.project.project_id == "p2"


def test_resolve_project_conversation_topic(monkeypatch):
    projects = [ProjectSource("p1", "ShopSense", "me", "shopsense"),
                ProjectSource("p2", "Bus Booking", "me", "bus-booking")]
    monkeypatch.setattr("github_repo.config.configured_projects",
                        lambda: projects)
    monkeypatch.setattr("github_repo.config.project_display_names", lambda: [])
    match = resolve_project(
        "How did you handle the payments part?",
        conversation_entities=["ShopSense", "express", "jwt"])
    assert match is not None and match.matched_by == "conversation-topic"
    assert match.project.project_id == "p1"


def test_resolve_project_single_project_fallback(monkeypatch):
    projects = [ProjectSource("p1", "OnlyOne", "me", "only-one")]
    monkeypatch.setattr("github_repo.config.configured_projects",
                        lambda: projects)
    monkeypatch.setattr("github_repo.config.project_display_names", lambda: [])
    match = resolve_project("Why did you choose postgres for auth?")
    assert match is not None and match.matched_by == "single-project"


def test_resolve_project_ambiguous_returns_none(monkeypatch):
    projects = [ProjectSource("p1", "ShopSense", "me", "shopsense"),
                ProjectSource("p2", "Bus Booking", "me", "bus-booking")]
    monkeypatch.setattr("github_repo.config.configured_projects",
                        lambda: projects)
    monkeypatch.setattr("github_repo.config.project_display_names", lambda: [])
    assert resolve_project("Why did you choose postgres for auth?") is None


def test_create_if_configured_none_without_projects():
    assert create_if_configured() is None


def test_require_auth_error_message():
    with pytest.raises(GitHubConfigError):
        GitHubRepositoryProvider(settings_=_make_settings(kiyra_github_token=""))._require_auth()


# ── Provider snapshot caching ───────────────────────────────────────────


class _FakeResp:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def get(self, url, headers=None):
        self.calls += 1
        return _FakeResp(self.payload)

    def close(self):
        pass


def test_provider_tree_is_cached(monkeypatch):
    payload = {"truncated": False, "tree": [
        {"path": "src/app.py", "type": "blob", "size": 12},
        {"path": "node_modules/x", "type": "blob", "size": 3},
    ]}
    fake = _FakeClient(payload)
    prov = GitHubRepositoryProvider(
        settings_=_make_settings())
    monkeypatch.setattr(prov, "_client", fake)
    project = ProjectSource("p1", "Proj", "me", "repo")
    first = prov.get_repository_tree(project)
    second = prov.get_repository_tree(project)
    assert fake.calls == 1          # second served entirely from cache
    assert first == second
    assert first[0]["path"] == "src/app.py"


def test_provider_read_file(monkeypatch):
    prov = GitHubRepositoryProvider(settings_=_make_settings())
    fake = _FakeClient({})
    fake.get = lambda url, headers=None: types.SimpleNamespace(
        status_code=200, content=b"print('hi')", text="")
    monkeypatch.setattr(prov, "_client", fake)
    project = ProjectSource("p1", "Proj", "me", "repo")
    assert prov.read_file(project, "src/app.py") == b"print('hi')"


def test_provider_404_surfaces_config_error(monkeypatch):
    prov = GitHubRepositoryProvider(settings_=_make_settings())
    fake = _FakeClient({})
    fake.get = lambda url, headers=None: types.SimpleNamespace(
        status_code=404, content=b"", text="Not Found")
    monkeypatch.setattr(prov, "_client", fake)
    project = ProjectSource("p1", "Proj", "me", "missing-repo")
    with pytest.raises(GitHubConfigError, match="not found"):
        prov.get_repository_tree(project)


# ── Chunking ─────────────────────────────────────────────────────────────


def test_detect_language():
    assert detect_language("x.py") == "python"
    assert detect_language("x.ts") == "typescript"
    assert detect_language("x.java") == "java"
    assert detect_language("migration.sql") == "sql"
    assert detect_language("README.md") == ""


def test_python_semantic_chunks():
    src = """import os

class User:
    def __init__(self, name):
        self.name = name

def authenticate(token):
    return token

def helper():
    pass
"""
    chunks = list(chunk_source(
        src, path="auth.py", language="python",
        project_id="p1", repository="me/repo"))
    assert len(chunks) == 3
    assert chunks[0].symbols == ["User"]
    assert "class User" in chunks[0].content
    assert chunks[1].symbols == ["authenticate"]
    assert "def authenticate(token):" in chunks[1].content
    assert "return token" in chunks[1].content


def test_sql_schema_chunk():
    src = "CREATE TABLE users (id INT PRIMARY KEY);\n\nINSERT INTO users VALUES (1);"
    chunks = list(chunk_source(
        src, path="schema.sql", language="sql",
        project_id="p1", repository="me/repo"))
    assert chunks and chunks[0].symbols == ["users"]


def test_bounded_fallback_for_unknown_language():
    lines = "\n".join(f"line {i}" for i in range(60))
    chunks = list(chunk_source(
        lines, path="data.txt", language="",
        project_id="p1", repository="me/repo",
        size=20, overlap=2))
    assert len(chunks) > 1
    assert chunks[0].start_line >= 1


def test_js_route_chunk():
    src = """const express = require('express');
const router = express.Router();
router.get('/users', (req, res) => { res.json([]); });
module.exports = router;
"""
    chunks = list(chunk_source(
        src, path="routes.js", language="javascript",
        project_id="p1", repository="me/repo"))
    assert chunks
    assert any("router.get" in c.content for c in chunks)


# ── Index filtering (secrets/lockfiles/binary) ───────────────────────────


def test_excluded_paths():
    for bad in ["node_modules/foo/index.js",
                ".venv/lib/x.py",
                "a/__pycache__/m.pyc",
                ".git/config",
                "dist/bundle.js",
                "build/app",
                ".next/server.js",
                "coverage/lcov.info",
                "package-lock.json",
                "yarn.lock",
                "config/.env",
                ".env.local",
                "keys/secret.pem",
                "secrets/id_rsa",
                ".env.production",
                # suffix-anchored secret patterns (regression: must not use
                # re.match, or these files slip into the index)
                "deploy/id_rsa",
                "config/app.key",
                "infra/credentials.json",
                "certs/server.p12",
                "deploy/app.keystore",
                "src/services/secrets.py"]:
        assert _excluded(bad), f"{bad} should be excluded"
    for good in ["src/app.py", "backend/routes.js",
                 "README.md", "src/auth/login.tsx"]:
        assert not _excluded(good), f"{good} should NOT be excluded"


def test_textlike():
    assert _textlike("src/app.py")
    assert _textlike("models.csv")
    assert not _textlike("img/logo.png")
    assert not _textlike("assets/font.woff2")


# ── Hybrid retrieval ─────────────────────────────────────────────────────


def _indexed_chunk(project_id, path, lang, text, symbols=None):
    from github_repo.indexer import IndexedChunk
    from knowledge.embeddings import embed_batch
    c = IndexedChunk(
        project_id=project_id, repository="me/repo",
        file_path=path, language=lang, content=text,
        start_line=1, end_line=len(text.splitlines()),
        symbols=list(symbols or []))
    c.embedding = embed_batch([text])[0]
    return c


def test_retriever_hybrid_ranking(monkeypatch):
    project = ProjectSource("p1", "Proj", "me", "repo")
    chunks = [
        _indexed_chunk("p1", "backend/middleware/auth.js", "javascript",
                       "const jwt = require('jsonwebtoken');\n"
                       "module.exports = function auth(req,res,next){\n"
                       "  const token = req.headers.authorization;\n"
                       "  res.locals.user = verify(token, SECRET);\n"
                       "  next();\n};", ["auth"]),
        _indexed_chunk("p1", "src/components/Login.tsx", "typescript",
                       "export function Login(){ return <form>...</form> }",
                       ["Login"]),
        _indexed_chunk("p1", "backend/models/User.js", "javascript",
                       "const User = new Model('user', {name:String});",
                       ["User"]),
    ]
    from github_repo.indexer import RepositoryIndex
    index = RepositoryIndex(settings_=_make_settings())
    index.set("p1", chunks)

    retriever = GitHubRetriever(
        provider=object(),  # unused below; index is pre-seeded
        index=index, settings_=_make_settings())

    hits = retriever.retrieve(project, "how does the jwt auth middleware verify tokens",
                              intent="project_authentication_security")
    assert hits, "should return evidence"
    top = hits[0]
    assert top.metadata["path"] == "backend/middleware/auth.js"
    assert "jwt" in top.content.lower()
    assert top.metadata["source"] == "github"
    assert top.metadata["project_id"] == "p1"
    assert len(hits) <= 6


def test_retriever_no_intent_still_returns_semantic_hits(monkeypatch):
    project = ProjectSource("p1", "Proj", "me", "repo")
    chunks = [_indexed_chunk("p1", "backend/middleware/auth.js", "javascript",
                             "const jwt = require('jsonwebtoken'); function auth(req,res,next){ next(); }",
                             ["auth"])]
    from github_repo.indexer import RepositoryIndex
    index = RepositoryIndex(settings_=_make_settings())
    index.set("p1", chunks)
    retriever = GitHubRetriever(provider=object(), index=index,
                                settings_=_make_settings())
    hits = retriever.retrieve(project, "auth middleware")
    assert hits and hits[0].metadata["path"] == "backend/middleware/auth.js"


def test_intent_terms_cover_all_project_intents():
    intents = {
        "project_explanation", "project_architecture", "project_implementation",
        "project_code", "project_debugging", "project_technology_choice",
        "project_database", "project_api", "project_frontend",
        "project_backend",
    }
    assert intents <= set(_INTENT_TERMS)
    assert "project_authentication_security" in _INTENT_TERMS


# ── End-to-end: tree → filtered index → hybrid retrieval ─────────────────


class FakeRepoProvider:
    """Serves a tiny synthetic repository; no network."""

    FILES = {
        "backend/middleware/auth.js":
            "const jwt = require('jsonwebtoken');\n"
            "function auth(req, res, next) {\n"
            "  const t = req.headers.authorization;\n"
            "  res.locals.user = jwt.verify(t, SECRET);\n"
            "  next();\n}\n"
            "module.exports = auth;\n",
        "backend/models/user.js":
            "const User = sequelize.define('user', { name: DataTypes.STRING });\n",
        "src/components/Login.tsx":
            "export function Login() { return <form><input /></form>; }\n",
        # Never indexed:
        "node_modules/lodash/index.js": "module.exports = { SECRET: 'x' };",
        "package-lock.json": '{"lockfileVersion": 3}',
        ".env": "SECRET_KEY=super-secret-value\nDB_PASSWORD=hunter2\n",
        "config/keys.pem": "-----BEGIN PRIVATE KEY-----\nabc\n",
        "README.md": "# ShopSense\nA store.\n",
    }

    def __init__(self):
        self.read_paths: list[str] = []

    def get_repository_tree(self, project, force_refresh=False, sha=None):
        return [{"path": p, "type": "blob", "size": len(c.encode())}
                for p, c in self.FILES.items()]

    def read_file(self, project, path):
        self.read_paths.append(path)
        return self.FILES[path].encode("utf-8")


def test_index_repository_filters_secrets_and_dependencies():
    from github_repo.indexer import index_repository
    project = ProjectSource("p1", "ShopSense", "me", "shopsense")
    provider = FakeRepoProvider()
    chunks = index_repository(project, provider)

    indexed_paths = {c.file_path for c in chunks}
    assert "backend/middleware/auth.js" in indexed_paths
    assert "backend/models/user.js" in indexed_paths
    # Secrets / dependencies / lockfiles are never fetched or indexed.
    assert "node_modules/lodash/index.js" not in indexed_paths
    assert "package-lock.json" not in indexed_paths
    assert ".env" not in indexed_paths
    assert "config/keys.pem" not in indexed_paths
    assert not any("hunter2" in c.content or "BEGIN PRIVATE KEY" in c.content
                   for c in chunks)
    assert ".env" not in provider.read_paths
    assert "config/keys.pem" not in provider.read_paths
    # Every chunk keeps full provenance + an embedding.
    for c in chunks:
        assert c.project_id == "p1"
        assert c.repository == "me/shopsense"
        assert c.language in ("javascript", "typescript", "")
        assert c.end_line >= c.start_line >= 1
        assert c.embedding and len(c.embedding) == 384


def test_retrieval_over_indexed_repository():
    from github_repo.indexer import RepositoryIndex, index_repository
    project = ProjectSource("p1", "ShopSense", "me", "shopsense")
    retriever = GitHubRetriever(
        provider=FakeRepoProvider(), index=RepositoryIndex(settings_=_make_settings()),
        settings_=_make_settings())
    assert retriever.is_indexed(project) is False
    hits = retriever.retrieve(
        project, "how do you verify the jwt token in the auth middleware",
        intent="project_authentication_security")
    assert hits
    top = hits[0]
    assert top.metadata["path"] == "backend/middleware/auth.js"
    assert top.metadata["source"] == "github"
    assert top.metadata["repo"] == "me/shopsense"
    assert top.metadata["project_id"] == "p1"
    assert 0.0 < top.metadata["score"] <= 1.0
    # Index is cached after the first retrieval.
    assert retriever.is_indexed(project) is True


# ── Evidence formatting ──────────────────────────────────────────────────


def test_evidence_block_contains_rules_and_paths():
    from github_repo.retrieval import ProjectChunkHit
    hits = [ProjectChunkHit(
        content="def auth(): pass",
        metadata={"path": "backend/auth.py", "language": "python",
                  "project_id": "p1", "repo": "me/repo",
                  "start_line": 3, "end_line": 4},
        score=0.91)]
    text = evidence_block("ShopSense", hits, "project_authentication_security")
    assert "PROJECT GROUNDING MODE" in text
    assert "backend/auth.py" in text
    assert "def auth(): pass" in text
    assert "ShopSense" in text


def test_evidence_note():
    from github_repo.retrieval import ProjectChunkHit
    hits = [ProjectChunkHit(content="a",
                            metadata={"path": "x.py"},
                            score=1.0) for _ in range(4)]
    note = evidence_note(hits)
    assert note.startswith("github:") and "4chunks" in note and "1files" in note
    assert evidence_note([]) == "github-none"