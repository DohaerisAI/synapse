# Unified Agent Loop

This runtime follows an OpenClaw-style split between skills, tools, and execution:

- the runtime exposes a real capability registry in code
- the main prompt includes only a compact capability summary and a compact skill index
- full `SKILL.md` bodies are loaded only when the agent asks for them
- the model decides when to chat, inspect, or act
- approvals are policy-based and apply only to outward or destructive actions

## Core Pattern

Each session runs through one serialized loop:

1. build compact context
2. run a model turn
3. accept one of:
   - final reply
   - missing-input request
   - tool calls
4. execute allowed tool calls
5. feed tool results back into the next model turn
6. stop only on final reply, approval wait, input wait, or failure

The gateway should not contain handwritten domain heuristics for Gmail, Calendar, Drive, Docs, Sheets, or future integrations.

## Skills vs Capabilities

Skills and capabilities are intentionally different:

- capabilities are executable runtime actions registered in code
- skills are operating instructions that help the agent decide what to do

The prompt should never rely on a handwritten per-domain tool list inside the gateway. Tool summaries must be generated from the capability registry.

## Current Design Rules

- keep the system prompt compact
- keep the capability summary registry-driven
- keep the skill index compact
- load `SKILL.md` only when needed
- do not promise external action unless the run has actually entered execution or created a real pause for input/approval
- preserve current task as loop context, not as a keyword router

## Why This Exists

This architecture keeps future integrations scalable:

- adding a new integration should mainly mean registering capabilities and adding skills
- the main prompt should not need handwritten per-tool rewrites
- the agent should learn to use skills, playbooks, and capabilities together instead of being driven by gateway keyword branches
