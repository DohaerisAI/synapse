# Operator Surfaces

## Web console
MVP pages:
- overview
- runs
- approvals
- auth
- memory
- skills
- adapter health

Current implementation:
- JSON API endpoints exist for health, runs, approvals, auth, memory, and skills.
- HTML pages exist under `/console`, `/console/runs`, `/console/approvals`, `/console/auth`, `/console/memory`, `/console/skills`, and `/console/adapters`.

## TUI
MVP functions:
- gateway health
- adapter status
- pending approvals
- auth status
- active runs
- loaded skills

Current implementation:
- local terminal TUI is available through `python -m agent_runtime tui`
- the TUI is built with Textual and shows Codex auth resolution, adapter health, recent runs, and approvals
- the TUI can edit `.env.local`, reload runtime config, start/stop background services, and approve pending actions
