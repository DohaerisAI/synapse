<p align="center">
  <img src="assets/logo.png" alt="Synapse" width="200" />
</p>

<h1 align="center">Synapse</h1>

<p align="center">
  <strong>An agent runtime that doesn't just respond. It remembers, reasons, and evolves.</strong>
</p>

<p align="center">
  <a href="#quickstart"><img src="https://img.shields.io/badge/quickstart-4%20commands-brightgreen?style=flat-square" /></a>
  <a href="#architecture"><img src="https://img.shields.io/badge/architecture-explicit%20state%20machines-blue?style=flat-square" /></a>
  <a href="#the-evolution-thesis"><img src="https://img.shields.io/badge/vision-self--improving-ff69b4?style=flat-square" /></a>
  <img src="https://img.shields.io/badge/python-3.12+-3776AB?style=flat-square&logo=python&logoColor=white" />
  <img src="https://img.shields.io/badge/license-private-lightgrey?style=flat-square" />
</p>

---

> **Most AI assistants are stateless prompt wrappers.**
> Send a message, get a response, forget everything.
> No memory. No approvals. No safety rails. No real tools.
>
> Synapse is what happens when you stop building chatbots and start building runtimes.

---

## What is Synapse?

Synapse is an async Python agent runtime that treats your AI assistant like a **real system** — with session state machines, capability-gated execution, durable memory, and human-in-the-loop approval flows.

It connects to Telegram (more channels coming), talks to Google Workspace (Gmail, Calendar, Drive, Docs, Sheets), and runs on your machine with a web console and TUI for full operator visibility.

```
You: "What's on my calendar today?"
Synapse: [plans] → [checks capabilities] → [executes GWS query] → [formats response]
         All tracked. All auditable. All approvable.
```

## Why Synapse Exists

We studied the best and took what worked:

| | OpenClaw | NanoClaw | **Synapse** |
|---|---------|----------|-------------|
| Runtime model | Implicit | Minimal | **Explicit state machine** |
| Safety | Basic | None | **Capability broker + approval gates** |
| Memory | In-context | Session-only | **Durable markdown** (session / user / global) |
| Observability | Logs | Logs | **Web console + TUI + heartbeat** |
| Extensibility | Plugin store | Hardcoded | **Plugin SDK** (skills / channels / hooks) |
| Self-improvement | Manual | None | **Integration lifecycle** (propose / test / approve / activate) |

Not a fork. Not a wrapper. A ground-up rethink of what an agent runtime should be.

## Quickstart

```bash
git clone https://github.com/mainadwitiya/synapse.git
cd synapse
python3 -m venv .venv && .venv/bin/pip install -e .
synapse onboard
synapse serve
```

Open **http://127.0.0.1:8000/console** — you're live.

## Architecture

```
                    Telegram / API / Webhook
                            |
                    [ Channel Adapter ]
                     normalize inbound
                            |
                      [ Gateway ]
               session lookup + context build
                            |
                     [ Planner ]
              decompose intent into actions
                            |
                  [ Capability Broker ]
             safe? risky? needs approval?
                     /            \
                 [ Host ]      [ Isolated ]
                Executor        Executor
                     \            /
                  [ State Machine ]
        RECEIVED → PLANNED → EXECUTING → COMPLETED
                            |
               +-----------+-----------+
               |                       |
        [ Memory Store ]        [ SQLite Store ]
        markdown files          runs, events,
        session / user /        approvals,
        global notes            adapter health
```

**Design philosophy:**
- Explicit state machines over implicit conversation flow
- Approval gates over "just do it" autonomy
- Markdown memory over opaque vector stores
- Plugin-first over monolithic features
- Operator observability over black-box execution

## Capabilities

<details>
<summary><strong>Google Workspace</strong> — Gmail, Calendar, Drive, Docs, Sheets</summary>

Natural language interface to your entire workspace. Ask "prep me for my next meeting" and Synapse will check your calendar, pull relevant Drive docs, and compose a brief.

All workspace actions are **approval-gated**. Synapse asks before it sends, deletes, or modifies.

</details>

<details>
<summary><strong>Stateful Sessions</strong> — every conversation is a tracked run</summary>

```
RECEIVED → CONTEXT_BUILT → PLANNED → EXECUTING → RESPONDING → COMPLETED
                                   ↘ WAITING_APPROVAL ↗
                                   ↘ WAITING_INPUT ↗
```

11 states. Explicit transitions. Full audit trail in SQLite.

</details>

<details>
<summary><strong>Durable Memory</strong> — markdown files that persist across sessions</summary>

Three scopes: **session** (conversation context), **user** (preferences, history), **global** (system knowledge). All stored as human-readable `.md` files you can inspect and edit.

</details>

<details>
<summary><strong>Plugin Architecture</strong> — skills, channels, and hooks</summary>

Drop a `manifest.json` + `SKILL.md` into `skills/` and Synapse picks it up automatically. 19 bundled skills ship out of the box.

Three plugin kinds: **skills** (capabilities), **channels** (adapters), **hooks** (lifecycle events).

</details>

<details>
<summary><strong>Operator Dashboard</strong> — web console + TUI</summary>

**Web console** at `/console` — 12 pages: Overview, Runs, Approvals, Auth, GWS, Memory, Workspace, Skills, Integrations, Adapters, Heartbeat, Logs.

**TUI** via `synapse tui` — edit config, approve actions, monitor runs from the terminal.

**Health checks** via `synapse doctor`.

</details>

<details>
<summary><strong>Proactive Heartbeat</strong> — Synapse checks on things for you</summary>

Configurable periodic checks. Synapse can monitor, summarize, and send you updates unprompted — on your schedule, during your active hours.

</details>

## The Evolution Thesis

Here's where it gets interesting.

Synapse already has an integration lifecycle: **propose** → **scaffold** → **test** → **approve** → **activate**. The agent can suggest new integrations, stage them safely, and activate them after human approval.

The next step — already in the roadmap — is **self-awareness**:

```
[ Self-Model ]        "I know what I am and what I can do"
      ↓
[ Diagnosis ]         "I know what I'm bad at"
      ↓
[ Forge ]             "I can write my own plugins to get better"
      ↓
[ Evolution Loop ]    "I continuously improve, with you in the loop"
```

Not AGI hype. Not autonomous chaos. A runtime that identifies its gaps, authors skills to fill them, tests them in a sandbox, and activates them — always with a human gate.

**The agent that builds itself, responsibly.**

## Configuration

```bash
# Required
TELEGRAM_BOT_TOKEN=your-bot-token

# Google Workspace
GWS_ENABLED=1
GWS_BINARY=gws
GWS_ALLOWED_SERVICES=gmail,calendar,drive,docs,sheets

# Optional
TELEGRAM_POLLING_ENABLED=1
AGENT_EXTRA_INSTRUCTIONS="your custom system prompt"
```

Config loads from `.env` → `.env.local` → process environment.

## CLI

```bash
synapse serve       # start the runtime
synapse tui         # operator terminal UI
synapse onboard     # interactive setup
synapse configure   # configuration wizard
synapse doctor      # health check & diagnostics
synapse plugins     # list discovered plugins
```

## Project Structure

```
synapse/
  config/          # typed Pydantic schema + env loader
  gateway/         # 11 decomposed orchestration sub-handlers
  plugins/         # plugin SDK — discovery, loading, registry
  channels/        # channel adapters (Telegram first)
  app.py           # FastAPI with web console
  runtime.py       # lifecycle, heartbeat, background services
  broker.py        # capability decisions + approval gating
  executors.py     # host + isolated execution with SSRF protection
  memory.py        # markdown-first durable storage
  store.py         # SQLite — runs, events, approvals, health
  hooks.py         # lifecycle event hooks
  skills.py        # skill registry + matching
skills/            # 19 bundled skills
tests/             # 90 tests across 16 files
```

## Tests

```bash
.venv/bin/python -m pytest -q
```

---

<p align="center">
  <strong>Built for operators who want to see what their agent is doing.</strong><br/>
  <em>Because "trust me bro" is not a safety model.</em>
</p>
