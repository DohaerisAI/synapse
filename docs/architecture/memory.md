# Memory

## Storage
- Markdown files for durable memory
- JSONL transcripts for append-only session history

## Layout
- `memory/global/`
- `memory/users/`
- `memory/sessions/<sessionKey>/summary.md`
- `memory/sessions/<sessionKey>/notes.md`
- `memory/sessions/<sessionKey>/transcript.jsonl`

## Rules
- session summaries update after completed runs
- global memory writes require approval in MVP
