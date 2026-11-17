# payroll

S-Corp owner payroll calculator with QuickBooks journal entries. FastAPI + SQLModel, Dockerized, Georgia/single-filing tax rules for tax year 2026.

## Conventions

# Global Agent Conventions

Applies to every repo unless a repo-specific note in `AGENTS.md` overrides it. Kept short on purpose — Codex truncates `AGENTS.md` at 32 KiB, so detail belongs in the language-specific docs, not here.

- Match the existing code style and structure in the repo rather than introducing new patterns.
- Ask before large refactors, schema/migration changes, or anything that touches production data or real money/trading capital.
- Never commit `.env` or other secrets. Keep `.env.example` current when adding new config.
- Run the project's test suite before considering work done; don't leave it broken.
- Prefer small, reviewable commits with clear messages over large batched ones.
- If a change conflicts with something documented in the repo's own README/SPEC, the repo's own docs win — flag the conflict rather than silently picking one.

# Python Conventions

Seeded from existing practice in `payroll` and `baggage_cdk`.

- Python 3.14, dependencies managed with `uv` — never `pip install` or `poetry` directly.
- `pyproject.toml` is the source of truth for dependencies; commit `uv.lock`.
- `ruff` for both linting and formatting (`ruff check`, `ruff format`) — not `black`/`flake8`/`pylint`.
- `pytest` for tests; don't reduce existing coverage.
- `pre-commit` hooks installed and passing before pushing, where the repo has a `.pre-commit-config.yaml`.

---
_Generated 2026-09-17 by sync_agents.py from the SecondBrain vault's ProjectOS/06 Standards/. Edit the source docs there, not this file, unless the change is genuinely repo-specific._

<!-- projectos:start -->
## ProjectOS

ProjectOS is Jason's Obsidian vault. It is the source of truth for this project's status, decisions, and work history.

- Project note: `/Users/jason/Documents/ProjectOS/01 Projects/Payroll App.md`
- Agent rules: `/Users/jason/Documents/ProjectOS/06 Standards/Agent Rules.md`

**Before starting:** read both files.
**When done:** add every decision to the note's "🧠 Decision Log" in the form `YYYY-MM-DD — decision — why`. Then append a work report to its "🤖 Agent Activity Log" with the date, your agent name, a short summary, and the files you touched. If `status` changed, update it in the note's frontmatter, and always set `updated` to today.
**Track your time:** for every work session, append one row to `/Users/jason/Documents/ProjectOS/07 Time Tracking/Time Log.md` with date, start, end (24h ET), minutes, client `-`, project `Payroll App`, your agent name, issue/PR ref or `-`, and a short summary. Also add `Time: N min` to your work report. See Agent Rules §10.
<!-- projectos:end -->
