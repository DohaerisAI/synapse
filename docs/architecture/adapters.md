# Adapters

## Supported MVP adapters
- Telegram: webhook first, with optional outbound send through the Bot API.

## Adapter contract
Adapters must:
- normalize inbound events into `NormalizedInboundEvent`
- expose connection and auth health
- send outbound text replies
- avoid direct model interaction

## Health model
Every adapter reports:
- status
- auth required
- last inbound time
- last outbound time
- last error

## Current implementation status
- Telegram inbound normalization is implemented.
- Telegram outbound send is implemented when the bot token is configured.
- Telegram polling is available for local bring-up without a public webhook.
