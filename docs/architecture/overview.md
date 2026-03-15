# Overview

## What we are building
This project is a Python agent runtime that starts as a personal assistant and is intentionally shaped so it can grow into a professional agent system later.

The target is a system that can begin with one user and a couple of messaging adapters, but over time support more structured workflows such as:

- customer support
- marketing operations
- research assistance
- code review and engineering automation
- internal operations assistants

## The core idea
The important idea is that the agent is not the whole app.

Instead, the system is split into clear layers:

1. adapters connect outside channels like Telegram
2. the gateway accepts and normalizes inbound events
3. the session state machine controls each run explicitly
4. the capability broker decides which actions are allowed
5. execution happens on host or Docker depending on the action
6. memory is stored in Markdown and transcripts on disk
7. SQLite records runs, approvals, state changes, and health
8. operators manage the system through a web console and TUI

This makes the system easier to reason about than a free-running agent loop.

## Why we are doing it this way
We want:

- a small runtime that is understandable
- strong observability from day one
- approvals for risky actions
- a clean path from personal assistant to more professional agent use cases
- human-readable memory and docs
- a codebase that can improve itself only through controlled, reviewable proposals

We do not want:

- hidden prompt spaghetti
- uncontrolled self-modification
- unstructured memory stores that are hard to inspect
- a giant framework before the fundamentals work

## MVP shape
The MVP is a single Python service with:

- Telegram ingress
- FastAPI API and minimal operator console
- simple TUI for local operations
- SQLite operational state
- Markdown memory
- Codex CLI auth reuse
- Azure OpenAI fallback
- capability-based execution policy
- event logging and approval flow

## Where OpenClaw and NanoClaw fit
This project is influenced by both, but is not trying to clone either one.

### OpenClaw ideas we are reusing
- gateway as control plane
- adapter model
- auth-profile model
- skills as local extensions
- operator-facing control surfaces
- transport-agnostic request normalization ahead of agent execution

### NanoClaw ideas we are reusing
- smaller codebase bias
- clearer trust boundaries
- optional isolated execution
- avoiding unnecessary platform sprawl
- keeping execution and side-effect policy explicit instead of ambient

### What is different here
This repo is leaning harder on:

- explicit session state machines
- capability brokering instead of broad tool availability
- Markdown-first memory instead of more complex memory systems in MVP
- docs-first implementation discipline
- controlled self-improvement through integration staging, approval, and startup bootstrap

## Development rule
Before implementing a major subsystem, its intent and boundaries should exist in Markdown under `docs/architecture/` or `docs/decisions/`.

The checklists under `docs/checklists/` are the running implementation record.

## Current slice
The current Python slice implements the runtime core, SQLite persistence, memory, local skills, auth/provider resolution, approval flow, and a Telegram webhook-first FastAPI surface.

The current repo is intentionally Telegram-only to keep the runtime lean while the execution, memory, and integration surfaces mature.
