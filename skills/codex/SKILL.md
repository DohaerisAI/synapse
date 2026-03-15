---
name: codex
description: "Proposal-only agentic coding with Codex CLI. Use when the user wants code changes, patches, implementation plans, or test commands without directly editing the repository."
metadata:
  openclaw:
    category: "engineering"
    requires:
      bins: ["codex", "git"]
---

# Codex Proposal Mode

Use this skill when the task is a coding change that should stay proposal-only until the user explicitly approves applying it.

## Tools

- `codex_propose`: Generate a proposal bundle in `var/proposals/<proposal_id>/`.
- `codex_run_tests`: Apply the proposal patch in a temporary sandbox and run the proposed test commands there.
- `codex_apply_proposal`: Apply `PATCH.diff` to the working tree only after approval.

## Rules

- Never edit the source repository directly when proposing.
- Always start with `codex_propose` for implementation work unless the user explicitly asks for analysis only.
- Keep repository context tight by passing `files` when the change is narrow.
- Review `PLAN.md`, `PATCH.diff`, `TESTS.md`, and `SUMMARY.md` before applying.
- Only use `codex_apply_proposal` after the user approves the proposal.

## Bundle Layout

Each proposal writes to `var/proposals/<proposal_id>/`:

- `PLAN.md`
- `PATCH.diff`
- `TESTS.md`
- `SUMMARY.md`

`METADATA.json` and test result artifacts may also appear alongside the required files.
