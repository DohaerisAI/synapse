# ADR-003: Codex CLI Auth Reuse

## Decision
Reuse local Codex CLI auth state in read-only mode for MVP.

## Why
This is the fastest path to subscription-backed Codex access.

## Deferred
A native OpenAI Codex OAuth implementation may be added later using the same auth-profile abstraction.
