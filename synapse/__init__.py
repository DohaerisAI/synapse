"""Agent runtime MVP package."""

from __future__ import annotations

from .config import RuntimeConfig

__all__ = ["Runtime", "RuntimeConfig", "build_runtime"]


def __getattr__(name: str):
    if name in {"Runtime", "build_runtime"}:
        from .runtime import Runtime, build_runtime

        return {"Runtime": Runtime, "build_runtime": build_runtime}[name]
    raise AttributeError(name)

