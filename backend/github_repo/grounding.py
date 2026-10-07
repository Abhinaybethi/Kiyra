"""Project evidence formatting + Bonsai grounding instructions (Phase 5.2
§14-16). Turns retrieval hits into a compact, metadata-anchored prompt block.
Never logs or forwards the whole repository — only the retrieved top-K.
"""
from __future__ import annotations

from config import settings
from github_repo.retrieval import ProjectChunkHit

# Intent → a one-line directive that orients Bonsai on what to emphasize.
_INTENT_PROMPTS: dict[str, str] = {
    "project_explanation": (
        "Explain how {name} implements it: direct answer → actual flow → "
        "technologies → important files/components → short example."),
    "project_architecture": (
        "Describe the architecture/structure of {name} grounded in the "
        "evidence: layers, key modules, and how components interact."),
    "project_implementation": (
        "Explain the concrete implementation flow in {name}, citing the "
        "relevant files and symbols from the evidence."),
    "project_code": (
        "PROJECT CODE MODE: show the actual code for this part of {name}. "
        "Prefer the repository implementation, keep relevant structure and "
        "naming, use a fenced code block, and name the file path before the "
        "code. Show only the relevant part — never the whole repository."),
    "project_debugging": (
        "Investigate the failure in {name} with the evidence: locate where it "
        "can go wrong, explain the cause, and give the fix."),
    "project_technology_choice": (
        "Explain WHY {name} uses the given technology, based only on code "
        "evidence; explain the trade-off against what the code actually shows."),
    "project_database": (
        "Explain the database/schema/data flow of {name} from the evidence: "
        "models, migrations, queries, and how data changes hands."),
    "project_api": (
        "Walk through the relevant API endpoints/routes of {name} from the "
        "evidence: request → handler/controller → response, naming files."),
    "project_frontend": (
        "Explain the relevant frontend/UI flow of {name} from the evidence, "
        "naming components/hooks/pages and how they connect."),
    "project_backend": (
        "Explain the relevant backend flow of {name} from the evidence, "
        "naming services/middleware/routes and how requests are handled."),
}

_GROUNDING_RULES = (
    "PROJECT GROUNDING MODE\n"
    "The repository evidence below is the candidate's ACTUAL project.\n"
    "Rules:\n"
    "- Do not invent implementation details, files, or technologies.\n"
    "- Only claim a technology/pattern if the evidence supports it.\n"
    "- Answer in first person when appropriate: this is an interview answer "
    "about the candidate's own project.\n"
    "- If the evidence is insufficient, say explicitly that the available "
    "project code does not show that detail instead of substituting generic "
    "knowledge.\n"
    "- A generic technical explanation may be added AFTER clearly grounding "
    "the project-specific claim.\n")


def evidence_block(
    project_name: str,
    hits: list[ProjectChunkHit],
    intent: str = "",
    *,
    max_chars: int = None,
) -> str:
    """Build the PROJECT EVIDENCE & TASK section for the user prompt."""
    cap = max_chars or settings.kiyra_github_top_k * 1200
    directive = _INTENT_PROMPTS.get(intent, _INTENT_PROMPTS["project_explanation"])
    lines = [_GROUNDING_RULES.strip(),
             "Relevant project: %s." % (project_name or "the candidate's project"),
             "Guidance: " + directive.format(name=project_name or "the project")]
    used = sum(len(x) for x in lines) + 4
    for h in hits:
        path = h.metadata.get("path", "?")
        lang = h.metadata.get("language", "")
        block = (
            f"--- {path} (lines {h.metadata.get('start_line', '?')}-"
            f"{h.metadata.get('end_line', '?')}) ---\n"
            f"{h.content}"
        )
        if used + len(block) > cap and len(lines) > 4:
            break
        lines.append(block)
        used += len(block) + 1
    return "\n\n".join(lines)


def evidence_note(hits: list[ProjectChunkHit]) -> str:
    """Short rag_note tail: how many files/projects were retrieved."""
    if not hits:
        return "github-none"
    n_files = len({h.metadata.get("path") for h in hits})
    return f"github:{len(hits)}chunks/{n_files}files"