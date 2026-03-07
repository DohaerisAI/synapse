# Agent Runtime MVP

A Python agent runtime for a personal assistant first, designed so it can later become a professional agent platform for workflows like customer support, marketing operations, research, and internal tooling.

## Overall idea
This project is not just a chatbot wrapper. It is a small agent platform with explicit system boundaries:

- adapters receive messages from external channels like Telegram
- a gateway normalizes inbound events and routes them into sessions
- a session state machine controls how each run moves from intake to response
- a capability broker decides what the agent is allowed to do
- execution can happen on the host or in Docker depending on risk
- Markdown memory stores durable notes and summaries in a human-readable form
- SQLite stores operational state such as runs, approvals, events, and adapter health
- operators manage the system through a web console and a simple TUI

## MVP goals
- Telegram based input
- Codex CLI auth reuse for OpenAI Codex access
- Azure OpenAI API support for `gpt-5.2-chat`
- approval-gated execution for risky actions
- controlled self-improvement via integration registry, staging, approval, and startup bootstrap
- docs and checklists kept current as implementation progresses

## Why this shape
OpenClaw and NanoClaw both have useful ideas, but this project is aiming for a clearer runtime model:

- OpenClaw contributes the gateway, adapter, auth-profile, and skills ideas
- NanoClaw contributes the emphasis on smaller, understandable runtime boundaries
- this repo adds explicit state machines, capability brokerage, Markdown-first memory, and operator-first observability in a Python-first service

## Source of truth
Start here, then see:

- `docs/architecture/overview.md`
- `docs/checklists/implementation-checklist.md`
- `docs/checklists/test-checklist.md`

## Current implementation
- Python package under `agent_runtime/`
- FastAPI app with inbound, health, approvals, runs, and skills endpoints
- Telegram webhook normalization plus optional outbound send when `TELEGRAM_BOT_TOKEN` is configured
- Google Workspace bridge via `gws` for Gmail, Calendar, Drive, Docs, and Sheets
- SQLite operational state for runs, run events, queued events, approvals, and adapter health
- Markdown/JSONL memory under `memory/`
- local skill loading from `skills/<skill-id>/`
- integration registry under `integrations/` with propose/scaffold/test/apply flow
- `BOOT.md` startup activation for approved integrations
- auth resolution order: local profiles, Codex CLI auth reuse, environment variables, config fallback
- minimal web console pages under `/console`
- rich Textual TUI via `agent-runtime tui`
- setup flow via `agent-runtime onboard`, `agent-runtime configure`, and `agent-runtime doctor`
- live Telegram account verification is in place

## Local run
```bash
python3 -m venv .venv
.venv/bin/pip install -e .
agent-runtime onboard
agent-runtime serve
```

## Runtime env
- `TELEGRAM_BOT_TOKEN`: Telegram bot token for outbound replies
- `TELEGRAM_POLLING_ENABLED=1`: enables local Telegram polling instead of relying only on webhooks
- `TELEGRAM_POLL_INTERVAL`: backoff interval after polling errors
- `AGENT_EXTRA_INSTRUCTIONS`: extra runtime instructions appended to the main assistant prompt
- `GWS_ENABLED=1`: enables the Google Workspace bridge
- `GWS_BINARY=gws`: path or command name for the Google Workspace CLI
- `GWS_ALLOWED_SERVICES=gmail,calendar,drive,docs,sheets`: service allowlist
- `GWS_PLANNER_EXTRA_INSTRUCTIONS`: extra instructions appended to the GWS skill-planning prompt
- `CODEX_AUTH_FILE`: optional override for Codex auth file discovery

Start from `.env.example` and export the values you actually want to use.

## Operator use
- Web console: `http://127.0.0.1:8000/console`
- TUI: `agent-runtime tui`

When Telegram polling is enabled, the runtime can receive Telegram messages locally without a public webhook URL.
The TUI can also edit `.env.local`, reload the runtime, and approve pending actions.
All Google Workspace actions are approval-gated, including reads. Use `/gws status`, `/gws gmail search ...`, `/gws calendar agenda`, `/gws drive search ...`, `/gws docs write ...`, and `/gws sheets read|append ...`.
Natural Workspace prompts are also supported now for common flows such as `my last mail`, `what's on my calendar today`, `prep me for my next meeting`, and `search my drive for budget`. If the agent replies that a Workspace action is waiting for approval, replying `yes`, `approve`, or `go ahead` in the same chat will approve it.

## Tests
```bash
.venv/bin/python -m pytest -q
```
