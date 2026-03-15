# Model Auth

## MVP providers
- `openai-codex/gpt-5.4` via Codex OAuth auth reuse and direct Codex responses transport
- `codex-cli/gpt-5.4` via local Codex CLI fallback
- `azure-openai/gpt-5.2-chat` via API key

## Python implementation notes
- provider resolution should be pure Python and independent of transport adapters
- local auth profiles should be loaded from the runtime state directory first
- Codex auth reuse remains read-only and should only read existing CLI state files
- direct `openai-codex` requests should use the locally stored OAuth bearer token and Codex backend transport
- `codex-cli` remains the shell-out fallback path when direct Codex transport fails or is forced via `CODEX_TRANSPORT=cli`
- outbound model clients should be isolated behind a provider interface so HTTP stack changes do not leak into the gateway

## Auth order
1. local auth profiles
2. Codex CLI auth reuse
3. environment variables
4. config fallback

## Deferred
- native OAuth login flow inside this runtime
- refresh-token renewal instead of bearer-token reuse plus CLI fallback
