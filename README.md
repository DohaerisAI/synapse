<p align="center">
  <img src="assets/logo.png" alt="Synapse" width="200" />
</p>

<h1 align="center">Synapse</h1>

<p align="center">
  <strong>A first-principles agent runtime that knows what it is, knows what it can't do, and gets better.</strong>
</p>

<p align="center">
  <a href="#quickstart"><img src="https://img.shields.io/badge/quickstart-3%20commands-brightgreen?style=flat-square" /></a>
  <a href="#self-awareness"><img src="https://img.shields.io/badge/self--aware-introspection%20%2B%20diagnosis-blue?style=flat-square" /></a>
  <a href="#the-thesis"><img src="https://img.shields.io/badge/thesis-self--improving%20agents-ff69b4?style=flat-square" /></a>
  <img src="https://img.shields.io/badge/python-3.12+-3776AB?style=flat-square&logo=python&logoColor=white" />
  <img src="https://img.shields.io/badge/license-private-lightgrey?style=flat-square" />
</p>

---

## The Thesis

Every agent framework today solves the same problem: _how do I get an LLM to call tools?_

We're solving a different one: **how do you build an agent that understands itself well enough to improve itself — with a human still in control?**

That's it. That's the whole idea. Everything in Synapse exists to answer that question.

```
[ Self-Model ]         I know what I am, what I can do, what I can't
       ↓
[ Diagnosis ]          I analyze my failures and find patterns
       ↓
[ Propose ]            I suggest new skills to fill the gaps
       ↓
[ Human Gate ]         You decide what gets activated
       ↓
[ Evolve ]             I'm better than I was yesterday
```

Not AGI hype. Not "autonomous agents." A runtime that identifies its own gaps, proposes fixes, and lets you decide.

---

## Quickstart

```bash
git clone https://github.com/DohaerisAI/synapse.git
cd synapse
python3 -m venv .venv && .venv/bin/pip install -e .
```

Then run the setup wizard:

```bash
synapse onboard
```

The wizard walks you through everything — LLM provider (Codex CLI, OAuth, Azure, or custom API), Telegram, Google Workspace, heartbeat, server config. It auto-detects existing credentials, probes endpoints, and writes your `.env.local`.

```bash
synapse serve
```

Open your console at the configured address. You're live.

---

## Self-Awareness

This is what makes Synapse different. The agent carries a structured model of itself.

**Self-Model** — typed Pydantic schemas describing identity, architecture, capabilities, and limitations. Not a prompt. A data structure the agent can query.

**Introspection** — discovers its own state at runtime: what capabilities are registered, what plugins are loaded, what skills are available, what it explicitly cannot do.

**Diagnosis** — analyzes historical runs, finds `action.unsupported` patterns, groups failures by action family, calculates frequency, and suggests improvements. The agent literally tells you what it's bad at.

```
GET /api/self          → full self-model snapshot
GET /api/diagnosis     → gap analysis + improvement suggestions
```

The self-model is also injected into every LLM context via `SELF.md` — so the agent reasons with accurate knowledge of its own capabilities, not hallucinated ones.

---

## Architecture

```
                    Telegram / API / Webhook
                            │
                    [ Channel Adapter ]
                     normalize inbound
                            │
                      [ Gateway ]
               session state machine (11 states)
                            │
                     [ Planner ]
              LLM-driven intent decomposition
                            │
                  [ Capability Broker ]
             safe? risky? needs approval?
                     ╱            ╲
                 [ Host ]      [ Isolated ]
                Executor        Executor
                     ╲            ╱
                  [ State Machine ]
        RECEIVED → PLANNED → EXECUTING → COMPLETED
                            │
               ┌────────────┼────────────┐
               │            │            │
        [ Memory ]    [ SQLite ]   [ Diagnosis ]
        markdown       runs,        gap analysis,
        3 scopes       events,      failure patterns
                       approvals
```

### Why these decisions

| Decision | Rationale |
|----------|-----------|
| Explicit state machines | Implicit flow is unauditable. 11 states give full visibility into what the agent is doing and why. |
| Capability Broker | Not everything should be auto-approved. "Read memory" is safe. "Send email" needs a human. Nuanced policy, not blanket rules. |
| Markdown memory | Three scopes (session, user, global). Human-readable, git-friendly, inspectable. Not opaque vector stores. |
| LLM-driven routing | ActionPlanner asks the model to classify intents. No hardcoded `if "calendar" in message` keyword matching. |
| Plugin SDK | Skills, channels, and hooks — drop a manifest + SKILL.md and it's discovered automatically. |
| SQLite + WAL | Durable event log for audit trails. Every run, every approval, every failure — queryable. |

---

## What It Can Do Today

**Google Workspace** — Gmail (send, search, triage), Calendar (agenda, create), Drive (search, upload), Docs (create, write), Sheets (read, append). All workspace actions are approval-gated.

**Telegram** — Full channel adapter with polling, streaming responses with live message editing, attachment handling.

**Memory** — Durable markdown files across three scopes. The agent remembers context across sessions, users, and globally.

**20 Bundled Skills** — GWS workflows (meeting prep, email triage), personal assistant patterns, grounded web research, memory stewardship, channel operations.

**Proactive Heartbeat** — Configurable periodic checks. The agent monitors things and reaches out on schedule, during your active hours.

**Operator Tooling** — Web console (12 pages), terminal TUI, health checks via `synapse doctor`, structured JSON logging.

**Self-Awareness** — Introspection APIs, diagnosis engine, self-model injection. The agent knows its own capabilities and limitations as data, not vibes.

### What It Explicitly Cannot Do (Yet)

We track limitations as first-class data, not footnotes:

- Auto-apply code patches (disabled in MVP — `code.patch.apply` returns `allowed=False`)
- Real Docker sandboxing (isolated executor runs on host for now)
- Channels beyond Telegram (Slack, Discord, email are planned)
- Self-author plugins autonomously (can propose, cannot auto-codegen yet)

These show up in `GET /api/self` under `limitations`. The agent knows about them and won't hallucinate capabilities it doesn't have.

---

## Setup Wizard

`synapse onboard` is an interactive terminal wizard with:

- **4 LLM providers** — Codex CLI (auto-detects `~/.codex/auth.json`), Codex OAuth (browser flow), Azure OpenAI, or any OpenAI-compatible API
- **Live probes** — verifies credentials, checks endpoints, validates bot tokens before saving
- **Step-by-step flow** — Agent identity → Provider → Telegram → GWS → Heartbeat → Server
- **Systemd integration** — `--install-daemon` sets up a user service that starts on boot

```bash
synapse onboard                      # full interactive setup
synapse onboard --flow quickstart    # just name + provider, defaults for rest
synapse onboard --install-daemon     # also install systemd service
synapse doctor                       # verify everything works
synapse doctor --json                # machine-readable health check
```

---

## CLI

```bash
synapse serve                # start the runtime server
synapse tui                  # operator terminal dashboard
synapse onboard              # interactive setup wizard
synapse configure            # re-run wizard sections
synapse doctor               # health check
synapse doctor --json        # health check (JSON)
synapse plugins              # list discovered plugins
synapse service install      # install systemd user service
synapse service status       # check service status
synapse service uninstall    # remove systemd service
```

---

## Configuration

Config loads in order: `.env` → `.env.local` → process environment. Later values override earlier ones.

`synapse onboard` writes `.env.local` for you. If you prefer manual setup:

```bash
# LLM Provider (pick one)
CODEX_MODEL=gpt-5.4
CODEX_AUTH_FILE=~/.codex/auth.json
CODEX_TRANSPORT=responses

# Or Azure
AZURE_OPENAI_ENDPOINT=https://myorg.openai.azure.com
AZURE_OPENAI_API_KEY=sk-...
AZURE_OPENAI_MODEL=gpt-5.2-chat

# Or Custom API
CUSTOM_API_BASE_URL=https://api.example.com/v1
CUSTOM_API_KEY=sk-...
CUSTOM_API_MODEL=my-model

# Telegram
TELEGRAM_BOT_TOKEN=your-bot-token
TELEGRAM_POLLING_ENABLED=1

# Google Workspace
GWS_ENABLED=1
GWS_ALLOWED_SERVICES=gmail,calendar,drive,docs,sheets

# Server
SERVER_HOST=127.0.0.1
SERVER_PORT=8000

# Heartbeat (optional)
HEARTBEAT_ENABLED=1
HEARTBEAT_EVERY_MINUTES=10
```

---

## Project Structure

```
synapse/
  config/          typed Pydantic schema + env loader
  gateway/         13 decomposed orchestration modules
  channels/        channel adapters (Telegram)
  plugins/         plugin SDK — discovery, loading, registry
  wizard/          interactive setup wizard (8 modules)
  streaming/       response streaming with live editing
  app.py           FastAPI with 12-page web console
  runtime.py       lifecycle, heartbeat, background services
  broker.py        capability decisions + approval gates
  executors.py     host + isolated execution
  memory.py        markdown-first durable storage
  store.py         SQLite — runs, events, approvals
  self_model.py    typed self-awareness schemas
  introspection.py runtime capability discovery
  diagnosis.py     failure analysis + gap detection
  hooks.py         lifecycle event hooks
  skills.py        skill registry + matching
skills/            20 bundled skills
tests/             246 tests across 26 files
```

---

## Tests

```bash
.venv/bin/python -m pytest -q     # 246 tests
```

---

<p align="center">
  <strong>The agent that knows what it can't do — and works on it.</strong>
</p>
