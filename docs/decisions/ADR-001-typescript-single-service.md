# ADR-001: Python Single Service

## Decision
Use a single Python service for the MVP.

## Why
The MVP is gateway-heavy, adapter-heavy, and operationally simpler as one service. Python keeps the runtime small, readable, and easy to operate while still supporting SQLite, local filesystem memory, simple HTTP ingress, and optional isolated execution.
