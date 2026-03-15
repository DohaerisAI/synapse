# Implementation Checklist

Completed items below reflect actual Python code in this repository.

## Next execution targets
- [ ] Isolated Docker executor instead of host fallback for risky actions
- [x] Telegram live end-to-end testing
- [ ] Self-improvement isolated test run

## Milestone 0 Docs and Bootstrap
- [x] Create docs/architecture files
- [x] Create ADRs
- [x] Create checklist files
- [x] Initialize Python package
- [x] Add FastAPI server
- [x] Add SQLite layer
- [x] Add test harness

## Milestone 1 Gateway
- [x] Normalized inbound schema
- [x] Session key derivation
- [x] Run/event persistence
- [x] Queueing

## Milestone 2 Models/Auth
- [x] Auth store
- [x] Codex CLI auth reader
- [x] Azure OpenAI provider
- [x] Codex CLI-backed provider execution
- [x] Fallback resolver
- [x] Auth health view model

## Milestone 3 Memory
- [x] Transcript store
- [x] Summary store
- [x] User/global memory files

## Milestone 4 Skills
- [x] Skill manifest format
- [x] SKILL.md loading
- [x] Built-in skills
- [x] Session skill injection

## Milestone 5 Telegram
- [x] Webhook path
- [x] Outbound send
- [x] Health checks

## Milestone 5b Google Workspace
- [x] `gws` status/config surface
- [x] Core 5 action builders
- [x] Universal approval gating for `gws.*`
- [x] API/web/TUI visibility

## Milestone 6 Capability/Execution
- [x] Capability broker
- [x] Host executor
- [ ] Docker executor
- [x] Approval flow

## Milestone 7 Web Console
- [x] Overview page
- [x] Runs page
- [x] Approvals page
- [x] Auth page
- [x] Memory page
- [x] Skills page
- [x] Adapter health page

## Milestone 8 TUI
- [x] Health panel
- [x] Approval actions
- [x] Adapter status
- [x] Auth status
- [x] GWS status
- [x] Skills view
- [x] Config editing and reload

## Milestone 8b Setup Flow
- [x] `agent-runtime onboard`
- [x] `agent-runtime configure`
- [x] `agent-runtime doctor`
- [x] `agent-runtime` console script entrypoint

## Milestone 9 Self-improvement
- [x] Patch proposal flow
- [ ] Isolated test run
- [x] Approval gating
