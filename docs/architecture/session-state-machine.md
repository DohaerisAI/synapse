# Session State Machine

## Run states
- RECEIVED
- CONTEXT_BUILT
- PLANNED
- WAITING_APPROVAL
- EXECUTING
- VERIFYING
- RESPONDING
- COMPLETED
- FAILED
- CANCELLED

## Rules
- only one active run per session key
- inbound follow-ups while active are queued
- every transition is persisted
- failures produce a terminal error event and user-safe reply when possible
