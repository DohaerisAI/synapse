# Capability Broker

The capability broker sits between model intent and side effects.

## MVP capability actions
- memory.read
- memory.write
- web.fetch
- telegram.send
- shell.exec
- code.patch.propose
- skills.read
- skills.apply.proposal

## Execution defaults
- host: memory and adapter sends
- docker: shell, web fetch, patch proposals

## Approval defaults
- shell.exec: requires approval unless safe allowlist
- global memory write: requires approval
- patch apply: not allowed in MVP
