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
.venv/bin/python -m synapse onboard
```

The wizard walks you through everything — LLM provider (Codex CLI, OAuth, Azure, or custom API), Telegram, Google Workspace, MCP financial services, heartbeat, server config. It auto-detects existing credentials, reuses saved tokens, probes endpoints, and writes your `.env.local`.

```bash
.venv/bin/python -m synapse serve    # start the server
.venv/bin/python -m synapse chat     # or interactive terminal chat
```

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
                    Telegram / API / Terminal
                            │
                    [ Channel Adapter ]
                     normalize inbound
                            │
                      [ Gateway ]
                            │
               ┌────────────┴────────────┐
               │                         │
        [ Slash Commands ]        [ ReAct Loop ]
         deterministic path       LLM + native tool calling
         planner → executor       load_skill → shell_exec
               │                         │
               │              [ Operator Layer ]
               │              policy, grounding,
               │              fallback defaults
               │                         │
               │              ┌──────────┼──────────┐
               │              │          │          │
               │        [ Builtins ] [ Skills ]  [ MCP ]
               │         memory,     hot-reload  Kite,
               │         shell, fs,  install     external
               │         web, codex  proposals   servers
               │              │          │          │
               └──────────────┴──────────┴──────────┘
                            │
               ┌────────────┼──────────────────────┐
               │            │            │          │
        [ Memory ]    [ SQLite ]   [ Jobs ]   [ Diagnosis ]
        markdown       runs,        background  gap analysis,
        3 scopes       events,      execution   failure patterns
                       approvals,
                       jobs, usage,
                       proposals
```

### Why these decisions

| Decision | Rationale |
|----------|-----------|
| ReAct loop with native tool calling | Single LLM loop with tool_use. No multi-stage pipeline, no hardcoded routing. The model decides what to call. |
| Skills + shell_exec | Drop a SKILL.md, restart, done. The LLM loads skills on demand and executes via shell. No code changes for new capabilities. |
| Approval at the tool level | `shell_exec("gws gmail +triage")` is safe. `shell_exec("gws gmail send ...")` needs approval. Per-tool, per-command policy. |
| Markdown memory | Three scopes (session, user, global). Human-readable, git-friendly, inspectable. Not opaque vector stores. |
| MCP for external services | Kite (Zerodha) connects via MCP. Stdio transport supports `mcp-remote` bridges. New brokers = new MCP connection. |
| SQLite + WAL | Durable event log for audit trails. Every run, every approval, every failure — queryable. |

---

## What It Can Do Today

**Google Workspace** — Gmail (send, search, triage), Calendar (agenda, create), Drive (search, upload), Docs (create, write), Sheets (read, append). The LLM loads GWS skills and calls the `gws` CLI directly.

**Swing Trading** — Scan Nifty 50/500/FnO stocks for setups (inside candle, NR7, volume dry-up, engulfing), full single-stock TA, TKM position sizing. Uses `tradingview_ta` — free, no API key. First-class `swing_analyze` and `swing_scan` builtins — no skill load needed.

**Zerodha Kite (MCP)** — Holdings, positions, margins, order history, GTT placement. Connected via MCP protocol with OAuth login.

**Telegram** — Full channel adapter with polling, streaming responses with live message editing, attachment handling.

**Memory** — Durable markdown files across three scopes (session, personal, global). The agent remembers context across conversations.

**Terminal Chat** — Claude Code-style REPL with streaming, tool call tracing, slash commands.

**Proactive Heartbeat** — Configurable periodic checks during active hours.

**Background Jobs** — Long-running tools (`shell_exec`, `codex_propose`, `codex_run_tests`) run in a background thread, persisted in SQLite, with direct delivery follow-up on completion. Set `background: true` in tool params.

**Skill Hot Reload** — Add, edit, or remove skills in `skills/` and they register without process restart. `POST /api/skills/reload` for manual trigger.

**Skill Install** — Install skill bundles from `.zip` or directory with staging, checksum validation, provenance tracking, and atomic promotion into the live skills tree.

**Codex Proposals** — `codex_propose` generates PLAN.md/PATCH.diff/TESTS.md bundles under `var/proposals/`. `codex_apply_proposal` applies with an approval gate. `codex_run_tests` runs tests in a sandboxed temp dir.

**FS / Repo Tools** — `fs_read`, `fs_write`, `fs_edit` (repo-root enforced), `patch_apply` (approval-gated), `repo_grep`, `repo_open`, `repo_status`, `repo_diffstat`, `repo_diff`.

**Usage Tracking** — Per-provider token and cost tracking. `GET /api/usage/summary`, `/usage` command in Telegram/chat.

**Operator Tooling** — Web console, terminal TUI, `synapse doctor`, structured JSON logging.

**Self-Awareness** — Introspection APIs, diagnosis engine, self-model injection.

### What It Explicitly Cannot Do (Yet)

- Auto-apply code patches without approval (proposal + explicit approval required)
- Docker sandboxing opt-in (`EXECUTION_ISOLATED_ENABLED=1`), off by default
- Channels beyond Telegram (Slack, Discord planned)
- Self-author plugins autonomously (can propose, cannot auto-codegen)

These show up in `GET /api/self` under `limitations`.

---

## Setup Wizard

`synapse onboard` is an interactive terminal wizard with:

- **4 LLM providers** — Codex CLI (auto-detects auth), Codex OAuth (browser flow), Azure OpenAI, or any OpenAI-compatible API
- **MCP financial services** — Zerodha Kite, TradingView, with token reuse and stdio transport support
- **Live probes** — verifies credentials, checks endpoints, validates bot tokens before saving
- **Token reuse** — existing Telegram and Kite tokens are preserved, not re-prompted
- **Systemd integration** — `--install-daemon` sets up a user service

```bash
synapse onboard                      # full interactive setup
synapse onboard --flow quickstart    # just name + provider, defaults for rest
synapse onboard --install-daemon     # also install systemd service
synapse doctor                       # verify everything works
```

---

## CLI

```bash
synapse serve                # start the runtime server
synapse chat                 # interactive terminal chat (REPL)
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

# Background jobs
JOB_MAX_CONCURRENCY=1

# Execution sandbox (optional, off by default)
EXECUTION_ISOLATED_ENABLED=0
EXECUTION_DOCKER_IMAGE=python:3.12-slim
EXECUTION_TIMEOUT_SECONDS=60
EXECUTION_MAX_OUTPUT_BYTES=65536
SKILL_AUTO_INSTALL_DEPS=0

# Usage pricing (optional)
PRICING_JSON=/path/to/pricing.json
```

MCP connections are configured in `mcp.yaml` (gitignored, contains tokens):

```yaml
enabled: true
connections:
  - server_id: kite
    url: "https://mcp.kite.trade/mcp"
    auth:
      auth_type: oauth
      token: "your-kite-token"
    enabled: true
  - server_id: upstox
    url: "https://mcp.upstox.com/mcp"
    transport: stdio
    command: "npx mcp-remote"
    auth:
      auth_type: oauth
    enabled: false
```

---

## Project Structure

```
synapse/
  config/          typed Pydantic schema + env loader
  gateway/         orchestration (core, planner, state, context, ingest)
  channels/        channel adapters (Telegram)
  tools/           tool registry, builtins, MCP tools
  mcp/             MCP client (HTTP + stdio transports)
  plugins/         plugin SDK — discovery, loading, registry
  wizard/          interactive setup wizard
  streaming/       response streaming with live editing
  app.py           FastAPI with web console + jobs/usage/skills endpoints
  runtime.py       lifecycle, heartbeat, background services
  react_loop.py    ReAct agent loop with native tool calling
  operator.py      operator policy layer — defaults, grounding, fallbacks
  jobs.py          background job substrate (threaded, SQLite-persisted)
  skill_runtime.py skill hot reload, package install, venv/docker execution
  codex_tools.py   proposal generation, apply, sandboxed test execution
  usage.py         per-provider token and cost tracking
  repl.py          terminal chat with streaming
  executors.py     host execution for slash commands
  memory.py        markdown-first durable storage
  store.py         SQLite — runs, events, approvals, jobs, usage, proposals
  self_model.py    typed self-awareness schemas
  introspection.py runtime capability discovery
  diagnosis.py     failure analysis + gap detection
  approvals.py     tool-level approval gates with persistent allowlist
  hooks.py         lifecycle event hooks
  skills.py        skill registry + hot reload + metadata parsing
skills/            bundled skills (GWS, swing-trader, codex, assistant)
tests/             ~480 tests across 42 files
```

---

## Tests

```bash
.venv/bin/python -m pytest -q     # ~480 tests
```

---

<p align="center">
  <strong>The agent that knows what it can't do — and works on it.</strong>
</p>
