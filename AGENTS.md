# AGENTS.md

Kiyra — local-first AI interview assistance platform.
Backend: FastAPI (Python 3.13, `uv`) · Frontend: Next.js 14 · Desktop: Windows Tkinter overlay.

## Commands

- `make backend` — uvicorn on :8000
- `make frontend` — Next.js on :3000
- `make test` — `cd backend && uv run pytest tests/ -v` (340 tests; must stay green)
- `make live-check` — live E2E harness (needs running backend + Bonsai + Whisper + Windows SAPI)
- `make migrate` / `make migration name=...` — Alembic
- `make seed` — demo data

Run backend tests from `backend/`: `uv run pytest tests/ -q`. The `integration`
marker is registered in `backend/pyproject.toml`.

## Constraints worth remembering

- **One Tk root per test session.** Creating a second `tkinter.Tk()` after a
  previous one was destroyed intermittently raises
  `_tkinter.TclError: invalid command name "tcl_findLibrary"` on CPython 3.14 /
  Windows. All Tkinter tests use the session-scoped `tk_root` fixture in
  `backend/tests/conftest.py` and destroy only their own child widgets — never
  the root. Do not reintroduce `tk.Tk()` in test fixtures.
- `backend/tests/integration/live_*.py` are manual live harnesses, not pytest
  tests (filenames deliberately omit the `test_` prefix).

## gstack

Invoke gstack skills with the `gstack-` prefix (`/gstack-review`, not `/review`).

- **Web browsing**: always `/gstack-browse`. Never `mcp__claude-in-chrome__*`
  or raw fetch for interactive pages. `/gstack-scrape` for structured extraction.
- **Plan before building**: `/gstack-office-hours` → `/gstack-autoplan`
  (CEO → design → DX → eng, eng last).
- **Review before shipping**: `/gstack-review` on the branch diff.
- **QA**: `/gstack-qa` for backend API/WebSocket fixtures; `/gstack-browse` for
  the Next.js UI; `/gstack-qa-only` for report-only. Bugs: `/gstack-investigate`
  first — no fix without a root cause.
- **Ship**: `/gstack-ship` (tests, VERSION, CHANGELOG, PR); `/gstack-land-and-deploy` to merge.
- **Safety**: `/gstack-guard`, `/gstack-freeze`, `/gstack-careful`.
- **Memory/docs**: `/gstack-learn`, `/gstack-document-release`, `/gstack-health`, `/gstack-retro`.

Available: /gstack-office-hours, /gstack-plan-ceo-review, /gstack-plan-eng-review,
/gstack-plan-design-review, /gstack-design-consultation, /gstack-design-shotgun,
/gstack-design-html, /gstack-review, /gstack-deslop-shared-libs, /gstack-test-audit,
/gstack-ship, /gstack-land-and-deploy, /gstack-canary, /gstack-benchmark, /gstack-browse,
/gstack-qa, /gstack-qa-only, /gstack-design-review, /gstack-scrape,
/gstack-setup-browser-cookies, /gstack-setup-deploy, /gstack-retro, /gstack-investigate,
/gstack-document-release, /gstack-document-generate, /gstack-codex, /gstack-claude-code,
/gstack-cso, /gstack-autoplan, /gstack-plan-devex-review, /gstack-devex-review,
/gstack-careful, /gstack-freeze, /gstack-guard, /gstack-unfreeze, /gstack-gstack-upgrade,
/gstack-learn, /gstack-spec, /gstack-diagram, /gstack-make-pdf.

> Safety skills are advisory-only on the opencode host (gstack's Claude Code
> hooks enforce them; opencode does not).
