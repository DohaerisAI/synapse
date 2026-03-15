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
- proposal-only self-improvement workflow
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
- SQLite operational state for runs, run events, queued events, approvals, and adapter health
- Markdown/JSONL memory under `memory/`
- local skill loading from `skills/<skill-id>/`
- auth resolution order: local profiles, Codex CLI auth reuse, environment variables, config fallback
- minimal web console pages under `/console`
- rich Textual TUI via `python -m agent_runtime tui`
- live Telegram account verification is in place

## Bring-up
- set `TELEGRAM_BOT_TOKEN` for Telegram outbound replies
- set `TELEGRAM_POLLING_ENABLED=1` for local Telegram polling if you do not want to expose a webhook yet
- run `.venv/bin/python -m agent_runtime tui` to inspect Codex auth and runtime status
- run `.venv/bin/python -m uvicorn agent_runtime.app:create_app --factory` to serve webhooks and operator pages
- use `.env.example` as the starting point for local environment values
