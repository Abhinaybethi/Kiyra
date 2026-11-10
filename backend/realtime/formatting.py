"""Post-processing for answers the candidate reads off the overlay.

The prompts ask for plain speakable text, but small local models still emit
markdown decoration — ``**Thesis:**`` labels, ``### Headings``, ``__bold__``.
The overlay renders the answer verbatim in a Tk ``Text`` widget, so those
markers show up as literal asterisks and hashes on screen.

Prompting the decoration away works but costs answer length (measured: mean
words 185 -> 156 on the same questions), and length is the thing detailed mode
exists to protect. Stripping the markers after generation is lossless: the
words stay, only the syntax goes.

Fenced code blocks are left untouched — ``#`` is a comment character in shell,
Python and YAML, so rewriting lines inside a fence would corrupt real code.
"""
from __future__ import annotations

import re

# ```lang ... ``` — kept verbatim. Non-greedy so adjacent blocks stay separate.
_FENCE = re.compile(
    r"(```[^\n]*\n.*?(?:```|\Z)|~~~[^\n]*\n.*?(?:~~~|\Z))", re.S)

# **bold** / __bold__ -> bold. Applied only outside code fences.
_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*|__(?=\S)(.+?)(?<=\S)__", re.S)

# Any leftover paired/unpaired emphasis run ("**", "__", a lone "***").
_STRAY = re.compile(r"\*{2,}|_{2,}")

# "### Heading" -> "Heading". The trailing space is required so an ordinary
# "#1 reason" keeps its hash instead of being read as a heading.
_HEADING = re.compile(r"^[ \t]*#{1,6}(?:[ \t]+|$)", re.M)

# "* item" -> "- item" so bullets read the same as the "- " ones we ask for.
_STAR_BULLET = re.compile(r"^[ \t]*\*[ \t]+", re.M)

# `identifier()` -> identifier(): the backticks are markup, not spoken words.
_INLINE_CODE = re.compile(r"`([^`\n]+)`")

_BLANK_RUN = re.compile(r"\n{3,}")
_INNER_RUN = re.compile(r"[ \t]{2,}")


def _strip_marks(chunk: str) -> str:
    """Remove inline emphasis and heading markup from prose (no code fences)."""
    chunk = _BOLD.sub(lambda m: m.group(1) or m.group(2) or "", chunk)
    chunk = _INLINE_CODE.sub(r"\1", chunk)
    chunk = _HEADING.sub("", chunk)
    chunk = _STAR_BULLET.sub("- ", chunk)
    chunk = _STRAY.sub("", chunk)
    # Removing a marker mid-sentence leaves a double space; keep the line's
    # leading indentation (bullets) but collapse runs inside the text.
    lines = []
    for line in chunk.split("\n"):
        lead = line[:len(line) - len(line.lstrip(" \t"))]
        lines.append(lead + _INNER_RUN.sub(" ", line[len(lead):]))
    return "\n".join(lines)


def plain_answer(text: str) -> str:
    """Return ``text`` with markdown decoration removed, code blocks intact.

    Idempotent: running it twice gives the same result. Never raises — a
    formatting helper must not be able to lose an answer.
    """
    if not text:
        return text or ""
    try:
        out: list[str] = []
        pos = 0
        for match in _FENCE.finditer(text):
            out.append(_strip_marks(text[pos:match.start()]))
            out.append(match.group(0))
            pos = match.end()
        out.append(_strip_marks(text[pos:]))
        cleaned = _BLANK_RUN.sub("\n\n", "".join(out))
        return cleaned.strip()
    except Exception:  # noqa: BLE001 — formatting must never break a turn
        return text
