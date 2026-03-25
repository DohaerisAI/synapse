from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .models import AuthProfile


class AuthStore:
    def __init__(
        self,
        auth_config_path: Path,
        fallback_config_path: Path,
        *,
        env: dict[str, str] | None = None,
        home: Path | None = None,
    ) -> None:
        self.auth_config_path = auth_config_path
        self.fallback_config_path = fallback_config_path
        self.env = env if env is not None else dict(os.environ)
        self.home = home if home is not None else Path.home()

    def load_profiles(self) -> list[AuthProfile]:
        if not self.auth_config_path.exists():
            return []
        payload = json.loads(self.auth_config_path.read_text(encoding="utf-8"))
        profiles = payload["profiles"] if isinstance(payload, dict) else payload
        return [
            AuthProfile(
                provider=item["provider"],
                model=item["model"],
                source="local_profile",
                settings=dict(item.get("settings", {})),
            )
            for item in profiles
        ]

    def load_codex_cli_profile(self) -> AuthProfile | None:
        model_override = self.env.get("CODEX_MODEL", "").strip()
        transport_override = self.env.get("CODEX_TRANSPORT", "").strip().lower()
        transport = transport_override if transport_override in {"responses", "cli"} else "responses"
        for candidate in self._codex_auth_candidates():
            if not candidate.exists():
                continue
            payload = json.loads(candidate.read_text(encoding="utf-8"))
            tokens = payload.get("tokens", {}) if isinstance(payload, dict) else {}
            access_token = (
                payload.get("access_token")
                or payload.get("token")
                or (tokens.get("access_token") if isinstance(tokens, dict) else None)
            )
            refresh_token = tokens.get("refresh_token") if isinstance(tokens, dict) else None
            account_id = tokens.get("account_id") if isinstance(tokens, dict) else None
            if not access_token:
                continue
            return AuthProfile(
                provider="openai-codex",
                model=model_override or payload.get("model", "gpt-5.3-codex"),
                source="codex_cli",
                settings={
                    "token": access_token,
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                    "account_id": account_id,
                    "auth_path": str(candidate),
                    "transport": transport,
                    "cli_fallback": True,
                    "endpoint": "https://chatgpt.com/backend-api/codex/responses",
                },
            )
        return None

    def load_env_profile(self) -> AuthProfile | None:
        api_key = self.env.get("AZURE_OPENAI_API_KEY")
        endpoint = self.env.get("AZURE_OPENAI_ENDPOINT")
        if not api_key or not endpoint:
            return None
        return AuthProfile(
            provider="azure-openai",
            model=self.env.get("AZURE_OPENAI_MODEL", "gpt-5.2-chat"),
            source="environment",
            settings={
                "api_key": api_key,
                "endpoint": endpoint,
                "deployment": self.env.get("AZURE_OPENAI_DEPLOYMENT", self.env.get("AZURE_OPENAI_MODEL", "gpt-5.2-chat")),
                "api_version": self.env.get("AZURE_OPENAI_API_VERSION", "2024-10-21"),
            },
        )

    def load_custom_profile(self) -> AuthProfile | None:
        base_url = self.env.get("CUSTOM_API_BASE_URL", "").strip()
        api_key = self.env.get("CUSTOM_API_KEY", "").strip()
        model = self.env.get("CUSTOM_API_MODEL", "").strip()
        if not base_url or not api_key:
            return None
        transport = self.env.get("CODEX_TRANSPORT", "chat").strip().lower()
        if transport not in {"chat", "responses"}:
            transport = "chat"
        return AuthProfile(
            provider="custom",
            model=model or "default",
            source="environment",
            settings={
                "base_url": base_url,
                "api_key": api_key,
                "transport": transport,
            },
        )

    def load_fallback_profile(self) -> AuthProfile | None:
        if not self.fallback_config_path.exists():
            return None
        payload = json.loads(self.fallback_config_path.read_text(encoding="utf-8"))
        provider = payload.get("provider")
        model = payload.get("model")
        if not provider or not model:
            return None
        return AuthProfile(
            provider=provider,
            model=model,
            source="config_fallback",
            settings=dict(payload.get("settings", {})),
        )

    def resolve(self) -> AuthProfile | None:
        profiles = self.load_profiles()
        if profiles:
            return profiles[0]
        codex = self.load_codex_cli_profile()
        if codex is not None:
            return codex
        env_profile = self.load_env_profile()
        if env_profile is not None:
            return env_profile
        custom = self.load_custom_profile()
        if custom is not None:
            return custom
        return self.load_fallback_profile()

    def health_view(self) -> dict[str, Any]:
        local_profiles = self.load_profiles()
        codex_candidates = self._codex_auth_candidates()
        codex_profile = self.load_codex_cli_profile()
        env_profile = self.load_env_profile()
        custom_profile = self.load_custom_profile()
        fallback_profile = self.load_fallback_profile()
        resolved = self.resolve()
        return {
            "resolved": None
            if resolved is None
            else {
                "provider": resolved.provider,
                "model": resolved.model,
                "source": resolved.source,
                "transport": resolved.settings.get("transport"),
            },
            "sources": {
                "local_profiles": {
                    "available": bool(local_profiles),
                    "count": len(local_profiles),
                    "path": str(self.auth_config_path),
                },
                "codex_cli": {
                    "available": codex_profile is not None,
                    "candidates": [str(path) for path in codex_candidates],
                },
                "environment": {
                    "available": env_profile is not None,
                    "endpoint_configured": "AZURE_OPENAI_ENDPOINT" in self.env,
                    "api_key_configured": "AZURE_OPENAI_API_KEY" in self.env,
                },
                "custom_api": {
                    "available": custom_profile is not None,
                    "base_url_configured": bool(self.env.get("CUSTOM_API_BASE_URL", "").strip()),
                    "api_key_configured": bool(self.env.get("CUSTOM_API_KEY", "").strip()),
                },
                "config_fallback": {
                    "available": fallback_profile is not None,
                    "path": str(self.fallback_config_path),
                },
            },
        }

    def _codex_auth_candidates(self) -> list[Path]:
        override = self.env.get("CODEX_AUTH_FILE")
        candidates = []
        if override:
            candidates.append(Path(override).expanduser())
        candidates.extend(
            [
                self.home / ".codex" / "auth.json",
                self.home / ".config" / "codex" / "auth.json",
            ]
        )
        return candidates
