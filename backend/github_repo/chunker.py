"""Code chunking for the repository index (Phase 5.2 §10).

Prefer semantic chunks (class / function / route / model / controller /
service / query); fall back to bounded line windows for anything unparsed.
Every chunk preserves project/repo/path/language + line range metadata.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from config import settings as _settings

# Anchor regexes per language family: (compile, expression-to-match, kind).
def _py_anchors() -> list[tuple[re.Pattern, str]]:
    return [
        (re.compile(r"^(?:@[\w.]+(?:\([^)]*\))?\s+)*def\s+\w+|^def\s+\w+"), "function"),
        (re.compile(r"^class\s+\w+"), "class"),
    ]


def _js_anchors() -> list[tuple[re.Pattern, str]]:
    return [
        (re.compile(r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+\w+"), "function"),
        (re.compile(r"^(?:export\s+)?class\s+\w+"), "class"),
        (re.compile(r"^const\s+\w+\s*=\s*(?:async\s*)?(?:\([^)]*\)|[\w]+)\s*=>"), "function"),
        (re.compile(r"^(?:app|router|server|api)\.(?:get|post|put|patch|delete|use)\("), "route"),
        (re.compile(r"^router\.(?:get|post|put|patch|delete|use)\("), "route"),
        (re.compile(r"^module\.exports|^export\s+default\b"), "export"),
    ]


def _java_anchors() -> list[tuple[re.Pattern, str]]:
    return [
        (re.compile(r"^@(?:RestController|Controller|Service|Repository|Component|Entity)\b"), "annotation"),
        (re.compile(r"^@(?:Get|Post|Put|Delete|Patch)Mapping\b"), "mapping"),
        (re.compile(r"^public\s+(?:class|interface|enum|record\s|static|\/)\s+\w+", re.IGNORECASE), "class"),
        (re.compile(r"^\s{0,4}(?:public|private|protected)\s+[\w<>\[\],\s]+\w+\s*\([^)]*\)\s*\{?"), "method"),
    ]


def _sql_anchors() -> list[tuple[re.Pattern, str]]:
    return [
        (re.compile(r"CREATE\s+TABLE\b|CREATE\s+INDEX\b|CREATE\s*(OR\s+REPLACE\s+)?VIEW\b", re.IGNORECASE), "schema"),
        (re.compile(r"INSERT\s+INTO\b|UPDATE\b|DELETE\s+FROM\b|SELECT\b", re.IGNORECASE), "query"),
        (re.compile(r"^--\s*\{?[A-Za-z_:.-]+\}?\s*$", re.IGNORECASE), "migration"),
    ]


_LANGUAGE_ANCHORS: dict[str, list[tuple[re.Pattern, str]]] = {
    "python": _py_anchors(),
    "javascript": _js_anchors(),
    "typescript": _js_anchors(),
    "java": _java_anchors(),
    "sql": _sql_anchors(),
}


def detect_language(path: str) -> str:
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return {
        "py": "python",
        "js": "javascript",
        "mjs": "javascript",
        "cjs": "javascript",
        "ts": "typescript",
        "tsx": "typescript",
        "jsx": "javascript",
        "java": "java",
        "sql": "sql",
    }.get(ext, "")


@dataclass
class Chunk:
    project_id: str
    repository: str
    file_path: str
    language: str
    content: str
    start_line: int = 1
    end_line: int = 1
    symbols: list[str] = field(default_factory=list)

    def to_metadata(self) -> dict:
        return {
            "source": "github",
            "project_id": self.project_id,
            "repo": self.repository,
            "path": self.file_path,
            "language": self.language,
            "score": 0.0,
        }


def _bounded_chunks(content: str, start_line: int = 1,
                    size: Optional[int] = None,
                    overlap: Optional[int] = None) -> list[tuple[str, int, int]]:
    """Bounded line-window fallback chunks (no semantic anchors found)."""
    size = size or _settings.knowledge_chunk_size or 512
    size = min(int(size), 1200)
    overlap = overlap or _settings.knowledge_chunk_overlap or 50
    overlap = min(int(overlap), 120)
    lines = content.splitlines(keepends=True)
    out: list[tuple[str, int, int]] = []
    i = 0
    step = max(1, size - overlap)
    while i < len(lines):
        block_lines = lines[i:i + size]
        block = "".join(block_lines)
        if not block.strip():
            break
        out.append((block, start_line + i, start_line + i + len(block_lines) - 1))
        i += step
    if not out and content.strip():
        out.append((content, start_line, start_line + len(lines) - 1))
    return out


def chunk_source(
    text: str,
    *,
    path: str,
    language: str,
    project_id: str,
    repository: str,
    # line offset of the file within the stream (whole-file index = 1)
    base_line: int = 1,
    # bounded-fallback window tuning (ignored when semantic anchors are found)
    size: Optional[int] = None,
    overlap: Optional[int] = None,
) -> list[Chunk]:
    """Chunk one source file into semantic (or bounded) chunks with metadata."""
    lines = text.splitlines()
    anchors = _LANGUAGE_ANCHORS.get(language, [])
    if not anchors:
        for block, s, e in _bounded_chunks(text, start_line=base_line,
                                            size=size, overlap=overlap):
            yield Chunk(
                project_id=project_id, repository=repository, file_path=path,
                language=language, content=block.strip("\n"),
                start_line=s, end_line=e, symbols=[],
            )
        return

    # Collect anchor line numbers.
    marks: list[tuple[int, str]] = []
    for idx, raw in enumerate(lines):
        for pat, kind in anchors:
            if pat.search(raw):
                marks.append((idx, kind))
                break
    if not marks:
        for block, s, e in _bounded_chunks(text, start_line=base_line,
                                            size=size, overlap=overlap):
            yield Chunk(
                project_id=project_id, repository=repository, file_path=path,
                language=language, content=block.strip("\n"),
                start_line=s, end_line=e, symbols=[],
            )
        return

    marks.sort(key=lambda m: m[0])
    # Physical-character start line of the whole file for title association.
    for i, (idx, kind) in enumerate(marks):
        start = idx
        end = (marks[i + 1][0] - 1) if i + 1 < len(marks) else len(lines) - 1
        block_lines = lines[start:end + 1]
        if not any(b.strip() for b in block_lines):
            continue
        symbol = _symbol_for(lines[idx], kind)
        yield Chunk(
            project_id=project_id, repository=repository, file_path=path,
            language=language, content="\n".join(block_lines).strip("\n"),
            start_line=base_line + start, end_line=base_line + end,
            symbols=[symbol] if symbol else [],
        )


def _symbol_for(line: str, kind: str) -> str:
    """Best-effort symbol name from an anchor line."""
    m = re.search(r"\b(?:def|class|function|const|var|let)\s+([\w$]+)", line)
    if m:
        return m.group(1)
    m = re.search(r"\b(?:app|router|server|api)\.(get|post|put|patch|delete|use)\(([^)]*)\)", line)
    if m and m.group(2):
        return f"{m.group(1).upper()} {m.group(2).strip()[:40]}"
    m = re.search(r"@(?:Get|Post|Put|Delete|Patch)Mapping\(\s*[\"']([^\"']+)", line)
    if m:
        return m.group(1)
    m = re.search(r"CREATE\s+TABLE\s+([\w.]+)", line, re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(r"(?:public|private|protected)\s+[\w<>\[\],\s]+\s*(\w+)\s*\([^)]*\)", line)
    if m:
        return m.group(1)
    return ""