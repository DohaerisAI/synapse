"""Codex OAuth browser flow: PKCE authorization code flow via ChatGPT."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

# OpenAI Codex OAuth configuration (from pi-ai library)
AUTH_URL = "https://auth.openai.com/oauth/authorize"
TOKEN_URL = "https://auth.openai.com/oauth/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
REDIRECT_URI = "http://localhost:1455/auth/callback"
SCOPES = "openid profile email offline_access"
CALLBACK_PORT = 1455
CALLBACK_TIMEOUT = 120  # seconds


class OAuthError(Exception):
    """Raised when OAuth flow fails."""


def run_codex_oauth(root: Path) -> dict[str, Any]:
    """Run full OAuth browser flow. Returns credential dict.

    Flow:
    1. Generate PKCE verifier + challenge
    2. Start local HTTP server on :1455
    3. Open browser to auth URL
    4. Wait for callback with auth code
    5. Exchange code for tokens
    6. Save to auth-profiles.json
    """
    # PKCE
    verifier = _generate_pkce_verifier()
    challenge = _generate_pkce_challenge(verifier)
    state = secrets.token_hex(16)

    # Build auth URL
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
        "originator": "synapse",
    }
    auth_url = f"{AUTH_URL}?{urlencode(params)}"

    # Start callback server
    callback = _CallbackHandler()
    server = HTTPServer(("127.0.0.1", CALLBACK_PORT), callback.make_handler(state))
    server.timeout = CALLBACK_TIMEOUT
    server_thread = threading.Thread(target=server.handle_request, daemon=True)
    server_thread.start()

    # Always show the URL so users can copy it (especially useful for SSH/headless)
    print(f"\n  Auth URL (copy if browser doesn't open):\n  {auth_url}\n")

    # Try to open browser automatically
    try:
        webbrowser.open(auth_url)
    except Exception:
        pass  # URL already printed above — user can copy it manually

    # Wait for callback
    server_thread.join(timeout=CALLBACK_TIMEOUT)
    server.server_close()

    if callback.error:
        raise OAuthError(f"OAuth callback error: {callback.error}")
    if not callback.code:
        raise OAuthError("OAuth timed out — no callback received within 2 minutes.")

    # Exchange code for tokens
    tokens = _exchange_code(callback.code, verifier)

    # Extract account info from JWT (best effort)
    email = _extract_email(tokens.get("access_token", ""))

    # Save credentials
    creds = {
        "provider": "openai-codex",
        "model": "gpt-5.4",
        "source": "oauth",
        "settings": {
            "token": tokens["access_token"],
            "access_token": tokens["access_token"],
            "refresh_token": tokens.get("refresh_token", ""),
            "email": email,
            "transport": "responses",
            "endpoint": "https://chatgpt.com/backend-api/codex/responses",
        },
    }
    _save_profile(root, creds)

    return {
        "access_token": tokens["access_token"],
        "refresh_token": tokens.get("refresh_token", ""),
        "email": email,
    }


def _generate_pkce_verifier() -> str:
    """Generate a random PKCE code verifier (32 bytes, base64url-encoded)."""
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")


def _generate_pkce_challenge(verifier: str) -> str:
    """Generate PKCE S256 code challenge from verifier."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _exchange_code(code: str, verifier: str) -> dict[str, Any]:
    """Exchange authorization code for tokens."""
    resp = httpx.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": REDIRECT_URI,
        },
        timeout=10.0,
    )
    if resp.status_code != 200:
        raise OAuthError(f"Token exchange failed (HTTP {resp.status_code}): {resp.text}")
    return resp.json()


def _extract_email(access_token: str) -> str:
    """Best-effort extract email from JWT access token."""
    try:
        parts = access_token.split(".")
        if len(parts) < 2:
            return ""
        # Add padding
        payload_b64 = parts[1] + "=" * (4 - len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return payload.get("email", payload.get("sub", ""))
    except Exception:
        return ""


def _save_profile(root: Path, profile: dict[str, Any]) -> None:
    """Save OAuth profile to var/auth-profiles.json."""
    profiles_path = root / "var" / "auth-profiles.json"
    profiles_path.parent.mkdir(parents=True, exist_ok=True)

    existing: list[dict] = []
    if profiles_path.exists():
        try:
            data = json.loads(profiles_path.read_text(encoding="utf-8"))
            existing = data.get("profiles", data) if isinstance(data, dict) else data
        except (json.JSONDecodeError, KeyError):
            existing = []

    # Replace existing openai-codex profile or append
    updated = [p for p in existing if p.get("provider") != "openai-codex"]
    updated.append(profile)

    profiles_path.write_text(
        json.dumps({"profiles": updated}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


class _CallbackHandler:
    """Captures the OAuth callback code from the browser redirect."""

    def __init__(self) -> None:
        self.code: str = ""
        self.error: str = ""

    def make_handler(self, expected_state: str):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                params = parse_qs(parsed.query)

                if parsed.path != "/auth/callback":
                    self.send_response(404)
                    self.end_headers()
                    return

                # Validate state
                received_state = params.get("state", [""])[0]
                if received_state != expected_state:
                    outer.error = "State mismatch — possible CSRF"
                    self._respond("Authentication failed: state mismatch.")
                    return

                if "error" in params:
                    outer.error = params["error"][0]
                    self._respond(f"Authentication failed: {outer.error}")
                    return

                code = params.get("code", [""])[0]
                if not code:
                    outer.error = "No authorization code received"
                    self._respond("Authentication failed: no code.")
                    return

                outer.code = code
                self._respond(
                    "Authenticated! You can close this tab and return to your terminal."
                )

            def _respond(self, message: str) -> None:
                html = f"<html><body><h2>{message}</h2></body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(html.encode())

            def log_message(self, format: str, *args: Any) -> None:
                pass  # suppress HTTP server logs

        return Handler
