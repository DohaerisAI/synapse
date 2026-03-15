# Deployment

## Target
- single Linux VPS first
- Linux laptop supported for local dev

## Runtime shape
- one Python service served through FastAPI/Uvicorn
- SQLite on local disk
- Markdown memory under project directory
- optional Docker for isolated execution

## Adapter notes
- Telegram should run behind webhook URL in production
