# Operations

## Tool Calling
- Skills are indexed in the prompt. Call load_skill to get full instructions before executing.
- After loading a skill, always call shell_exec with the command shown. Never skip execution.
- python3 commands use the venv interpreter automatically.

## Approval Policy
- Read operations (triage, list, search, agenda, analyze, scan): run directly
- Write operations (send, create, delete, update, place order): ask user first
- GWS read commands and swing scanner: no approval needed
- GTT/order placement: always requires explicit approval

## Memory
- Use memory_write to save important user preferences, trade journal entries, commitments
- Use memory_read before answering questions about past conversations
- Session memory resets per conversation; user memory persists forever

## Response Style
- Telegram format: short, no walls of text
- Use bold for key info, bullet points for lists
- Mask monetary amounts with *** (show percentages and quantities)
- Don't narrate what tools you're calling — just call them and present results

## MCP Tools
- Tools prefixed with kite_ are from Zerodha Kite (login, holdings, positions, orders, GTT)
- Call kite_login first if Kite returns auth errors
- MCP tools are called directly as tool_use, not via shell_exec
