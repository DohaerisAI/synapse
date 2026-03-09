"""All wizard step functions. Each takes (prompter, env) and returns new env dict."""

from __future__ import annotations

import shutil
import socket
from pathlib import Path
from typing import TYPE_CHECKING

from .validators import (
    api_base_url,
    azure_endpoint,
    ip_or_hostname,
    non_empty,
    port_number,
    positive_int,
    telegram_token,
)

if TYPE_CHECKING:
    from .prompter import WizardPrompter


# -- Agent Identity -----------------------------------------------------------


def step_agent(prompter: WizardPrompter, env: dict[str, str]) -> dict[str, str]:
    """Prompt for agent name and extra instructions."""
    prompter.section(1, "Agent Identity")

    name = prompter.text(
        "Agent name:",
        default=env.get("AGENT_NAME", "Synapse"),
        validate=non_empty,
    )
    instructions = prompter.text(
        "Extra instructions (optional):",
        default=env.get("AGENT_EXTRA_INSTRUCTIONS", ""),
    )
    return {**env, "AGENT_NAME": name, "AGENT_EXTRA_INSTRUCTIONS": instructions}


# -- LLM Provider ------------------------------------------------------------


def step_provider(
    prompter: WizardPrompter,
    env: dict[str, str],
    root: Path,
) -> dict[str, str]:
    """Prompt for LLM provider configuration with grouped options."""
    prompter.section(2, "LLM Provider")

    provider = prompter.select(
        "LLM provider:",
        options=[
            ("Codex CLI (auto-detect)", "codex", "Uses ~/.codex/auth.json — recommended"),
            ("Codex OAuth (browser)", "codex_oauth", "Sign in via ChatGPT in your browser"),
            ("Azure OpenAI", "azure", "Endpoint + API key"),
            ("Custom API endpoint", "custom", "Any OpenAI-compatible API"),
        ],
        default=_detect_default_provider(env),
    )

    if provider == "codex":
        return _provider_codex(prompter, env)
    if provider == "codex_oauth":
        return _provider_codex_oauth(prompter, env, root)
    if provider == "azure":
        return _provider_azure(prompter, env)
    return _provider_custom(prompter, env)


def _provider_codex(
    prompter: WizardPrompter,
    env: dict[str, str],
) -> dict[str, str]:
    """Codex CLI auth file flow with auto-detection."""
    # Auto-detect existing auth
    auth_path = _find_codex_auth(env)
    if auth_path:
        prompter.note(f"Found Codex auth at: {auth_path}", title="Codex")
    else:
        prompter.note(
            "No Codex auth file found. Run `codex login` first, or enter path manually.",
            title="Codex",
        )

    auth_file = prompter.text(
        "Codex auth file path:",
        default=env.get("CODEX_AUTH_FILE", auth_path or ""),
        placeholder="~/.codex/auth.json",
    )

    model = prompter.text(
        "Model:",
        default=env.get("CODEX_MODEL", "gpt-5.4"),
        validate=non_empty,
    )

    transport = prompter.select(
        "Transport:",
        options=[
            ("Responses API", "responses", "OpenAI responses endpoint — recommended"),
            ("CLI passthrough", "cli", "Pipe through Codex CLI binary"),
        ],
        default=env.get("CODEX_TRANSPORT", "responses"),
    )

    # Probe: check if auth file exists and has a token
    resolved_path = auth_file.strip()
    if resolved_path:
        expanded = Path(resolved_path).expanduser()
        if expanded.exists():
            prompter.note("Auth file exists and readable.", title="Codex")
        else:
            prompter.note(
                f"Auth file not found at {expanded}. You can fix this later in .env.local.",
                title="Codex",
            )

    return {
        **env,
        "_PROVIDER_TYPE": "codex",
        "CODEX_MODEL": model,
        "CODEX_AUTH_FILE": auth_file,
        "CODEX_TRANSPORT": transport,
    }


def _provider_codex_oauth(
    prompter: WizardPrompter,
    env: dict[str, str],
    root: Path,
) -> dict[str, str]:
    """Codex OAuth browser flow — opens browser for ChatGPT sign-in."""
    from .oauth import OAuthError, run_codex_oauth

    prompter.note(
        "Opening browser for ChatGPT sign-in... "
        "If the browser doesn't open, copy the URL shown below.",
        title="OAuth",
    )

    try:
        creds = run_codex_oauth(root)
        email = creds.get("email", "unknown")
        prompter.note(f"Authenticated as {email}", title="OAuth")
        prompter.note("Credentials saved to var/auth-profiles.json", title="OAuth")
    except OAuthError as exc:
        prompter.note(f"OAuth failed: {exc}", title="OAuth")
        prompter.note("Falling back to manual Codex CLI setup.", title="OAuth")
        return _provider_codex(prompter, env)

    model = prompter.text(
        "Model:",
        default=env.get("CODEX_MODEL", "gpt-5.4"),
        validate=non_empty,
    )

    transport = prompter.select(
        "Transport:",
        options=[
            ("Responses API", "responses", "OpenAI responses endpoint — recommended"),
            ("CLI passthrough", "cli", "Pipe through Codex CLI binary"),
        ],
        default=env.get("CODEX_TRANSPORT", "responses"),
    )

    return {
        **env,
        "_PROVIDER_TYPE": "codex_oauth",
        "CODEX_MODEL": model,
        "CODEX_AUTH_FILE": "",
        "CODEX_TRANSPORT": transport,
    }


def _provider_azure(
    prompter: WizardPrompter,
    env: dict[str, str],
) -> dict[str, str]:
    """Azure OpenAI flow — endpoint + API key + deployment."""
    endpoint = prompter.text(
        "Azure endpoint URL:",
        default=env.get("AZURE_OPENAI_ENDPOINT", ""),
        placeholder="https://myorg.openai.azure.com",
        validate=azure_endpoint,
    )

    api_key = prompter.password(
        "Azure API key:",
        validate=non_empty,
    )

    model = prompter.text(
        "Model / deployment name:",
        default=env.get("AZURE_OPENAI_MODEL", "gpt-5.2-chat"),
        validate=non_empty,
    )

    deployment = prompter.text(
        "Deployment name (leave blank to use model name):",
        default=env.get("AZURE_OPENAI_DEPLOYMENT", ""),
    )

    api_version = prompter.text(
        "API version:",
        default=env.get("AZURE_OPENAI_API_VERSION", "2024-10-21"),
        validate=non_empty,
    )

    # Probe: try to reach the endpoint
    probe_ok = _probe_azure(endpoint, api_key)
    if probe_ok:
        prompter.note("Azure endpoint is reachable.", title="Azure")
    else:
        prompter.note(
            "Could not reach Azure endpoint (network issue or invalid credentials). "
            "You can fix this later in .env.local.",
            title="Azure",
        )

    return {
        **env,
        "_PROVIDER_TYPE": "azure",
        "AZURE_OPENAI_ENDPOINT": endpoint,
        "AZURE_OPENAI_API_KEY": api_key,
        "AZURE_OPENAI_MODEL": model,
        "AZURE_OPENAI_DEPLOYMENT": deployment or model,
        "AZURE_OPENAI_API_VERSION": api_version,
        # Clear codex fields when using Azure
        "CODEX_MODEL": model,
        "CODEX_AUTH_FILE": "",
        "CODEX_TRANSPORT": "",
    }


def _provider_custom(
    prompter: WizardPrompter,
    env: dict[str, str],
) -> dict[str, str]:
    """Custom API endpoint — any OpenAI-compatible service."""
    base_url = prompter.text(
        "API base URL:",
        default=env.get("CUSTOM_API_BASE_URL", ""),
        placeholder="https://api.example.com/v1",
        validate=api_base_url,
    )

    api_key = prompter.password(
        "API key:",
        validate=non_empty,
    )

    model = prompter.text(
        "Model name:",
        default=env.get("CUSTOM_API_MODEL", ""),
        validate=non_empty,
    )

    transport = prompter.select(
        "Transport:",
        options=[
            ("Responses API", "responses", "OpenAI-style /responses endpoint"),
            ("Chat completions", "chat", "OpenAI-style /chat/completions endpoint"),
        ],
        default=env.get("CODEX_TRANSPORT", "responses"),
    )

    # Probe: try to reach the API
    probe_ok = _probe_custom_api(base_url, api_key)
    if probe_ok:
        prompter.note("API endpoint is reachable.", title="Custom API")
    else:
        prompter.note(
            "Could not reach API endpoint. You can fix this later in .env.local.",
            title="Custom API",
        )

    return {
        **env,
        "_PROVIDER_TYPE": "custom",
        "CUSTOM_API_BASE_URL": base_url,
        "CUSTOM_API_KEY": api_key,
        "CUSTOM_API_MODEL": model,
        "CODEX_MODEL": model,
        "CODEX_AUTH_FILE": "",
        "CODEX_TRANSPORT": transport,
    }


# -- Telegram -----------------------------------------------------------------


def step_telegram(prompter: WizardPrompter, env: dict[str, str]) -> dict[str, str]:
    """Prompt for Telegram bot configuration."""
    prompter.section(3, "Telegram")

    enabled = prompter.confirm(
        "Enable Telegram channel?",
        default=bool(env.get("TELEGRAM_BOT_TOKEN", "").strip()),
    )
    if not enabled:
        return {
            **env,
            "TELEGRAM_BOT_TOKEN": "",
            "TELEGRAM_POLLING_ENABLED": "0",
        }

    token = prompter.password(
        "Telegram bot token:",
        validate=telegram_token,
    )

    # Live probe
    probe_ok = _probe_telegram_token(token)
    if probe_ok:
        prompter.note("Bot token verified successfully.", title="Telegram")
    else:
        prompter.note(
            "Could not verify token (network issue or invalid token). "
            "You can fix it later in .env.local.",
            title="Telegram",
        )

    polling = prompter.confirm("Enable polling?", default=True)
    interval = "2.0"
    if polling:
        interval = prompter.text(
            "Poll interval (seconds):",
            default=env.get("TELEGRAM_POLL_INTERVAL", "2.0"),
        )

    return {
        **env,
        "TELEGRAM_BOT_TOKEN": token,
        "TELEGRAM_POLLING_ENABLED": "1" if polling else "0",
        "TELEGRAM_POLL_INTERVAL": interval,
    }


# -- Google Workspace ---------------------------------------------------------


def step_gws(prompter: WizardPrompter, env: dict[str, str]) -> dict[str, str]:
    """Prompt for Google Workspace configuration."""
    prompter.section(4, "Google Workspace")

    enabled = prompter.confirm(
        "Enable Google Workspace integration?",
        default=env.get("GWS_ENABLED", "0").strip() in {"1", "true"},
    )
    if not enabled:
        return {**env, "GWS_ENABLED": "0"}

    # Check binary
    binary = env.get("GWS_BINARY", "gws")
    gws_path = shutil.which(binary)
    if gws_path:
        prompter.note(f"Found gws at: {gws_path}", title="GWS")
    else:
        prompter.note(
            f"'{binary}' not found in PATH. Install: npm i -g @googleworkspace/cli",
            title="GWS",
        )

    services = prompter.multi_select(
        "Allowed services:",
        options=[
            ("Gmail", "gmail", "Send, read, and manage emails"),
            ("Calendar", "calendar", "Create and manage events"),
            ("Drive", "drive", "Upload, download, and share files"),
            ("Docs", "docs", "Create and edit documents"),
            ("Sheets", "sheets", "Create and edit spreadsheets"),
        ],
        defaults=_parse_services(env.get("GWS_ALLOWED_SERVICES", "gmail,calendar,drive,docs,sheets")),
    )

    extra = prompter.text(
        "GWS planner extra instructions (optional):",
        default=env.get("GWS_PLANNER_EXTRA_INSTRUCTIONS", ""),
    )

    return {
        **env,
        "GWS_ENABLED": "1",
        "GWS_BINARY": binary,
        "GWS_ALLOWED_SERVICES": ",".join(services) if services else "gmail",
        "GWS_PLANNER_EXTRA_INSTRUCTIONS": extra,
    }


# -- Heartbeat ----------------------------------------------------------------


def step_heartbeat(prompter: WizardPrompter, env: dict[str, str]) -> dict[str, str]:
    """Prompt for heartbeat configuration."""
    prompter.section(6, "Heartbeat")

    enabled = prompter.confirm(
        "Enable heartbeat?",
        default=env.get("HEARTBEAT_ENABLED", "0").strip() in {"1", "true"},
    )
    if not enabled:
        return {**env, "HEARTBEAT_ENABLED": "0"}

    every = prompter.text(
        "Heartbeat interval (minutes):",
        default=env.get("HEARTBEAT_EVERY_MINUTES", "10"),
        validate=positive_int,
    )

    target = prompter.select(
        "Heartbeat target:",
        options=[
            ("Last active chat", "last", "Send heartbeat to the most recent conversation"),
            ("All chats", "all", "Broadcast heartbeat to every conversation"),
        ],
        default=env.get("HEARTBEAT_TARGET", "last"),
    )

    ack_mode = prompter.select(
        "Acknowledgment mode:",
        options=[
            ("Silent OK", "silent_ok", "Log success silently, no reply to user"),
            ("Reply", "reply", "Send a visible reply message"),
        ],
        default=env.get("HEARTBEAT_ACK_MODE", "silent_ok"),
    )

    return {
        **env,
        "HEARTBEAT_ENABLED": "1",
        "HEARTBEAT_EVERY_MINUTES": every,
        "HEARTBEAT_TARGET": target,
        "HEARTBEAT_ACK_MODE": ack_mode,
    }


# -- Server -------------------------------------------------------------------


def step_server(prompter: WizardPrompter, env: dict[str, str]) -> dict[str, str]:
    """Prompt for server host/port configuration."""
    prompter.section(7, "Server")

    host = prompter.text(
        "Server host:",
        default=env.get("SERVER_HOST", "127.0.0.1"),
        validate=ip_or_hostname,
    )

    port = prompter.text(
        "Server port:",
        default=env.get("SERVER_PORT", "8000"),
        validate=port_number,
    )

    # Port availability check
    port_int = int(port)
    if _port_available(host, port_int):
        prompter.note(f"Port {port_int} is available.", title="Server")
    else:
        prompter.note(
            f"Port {port_int} is in use. You may need to stop the existing process.",
            title="Server",
        )

    return {**env, "SERVER_HOST": host, "SERVER_PORT": port}


# -- MCP / Financial Services -------------------------------------------------


_MCP_SERVICES = {
    "kite": {
        "server_id": "kite",
        "url": "https://mcp.kite.trade/mcp",
        "auth_type": "oauth",
        "label": "Zerodha Kite",
        "hint": "Equity/MF holdings, positions, margins, GTT orders",
        "needs_token": True,
        "token_prompt": "Kite API key/token:",
    },
    "mfapi": {
        "server_id": "mfapi",
        "url": "https://xpack.ai/mcp/mfapi",
        "auth_type": "none",
        "label": "India MF API",
        "hint": "All Indian MF schemes, daily NAV, full history (free, no auth)",
        "needs_token": False,
    },
    "tradingview": {
        "server_id": "tradingview",
        "url": "https://mcp.tradingviewapi.com/mcp",
        "auth_type": "jwt",
        "label": "TradingView Data",
        "hint": "Prices, quotes, TA scores, calendar, news (needs RapidAPI key)",
        "needs_token": True,
        "token_prompt": "TradingView RapidAPI key:",
    },
}


def step_mcp(prompter: WizardPrompter, env: dict[str, str], root: Path) -> dict[str, str]:
    """Prompt for MCP financial services configuration."""
    prompter.section(5, "Financial Services (MCP)")

    enabled = prompter.confirm(
        "Enable financial services? (portfolio, MF, charts, trading)",
        default=False,
    )
    if not enabled:
        return {**env, "_MCP_ENABLED": "0"}

    selected = prompter.multi_select(
        "Which financial services to connect?",
        options=[
            (svc["label"], key, svc["hint"])
            for key, svc in _MCP_SERVICES.items()
        ],
        defaults=["kite", "mfapi"],
    )

    if not selected:
        prompter.note("No services selected. Skipping MCP setup.", title="MCP")
        return {**env, "_MCP_ENABLED": "0"}

    # Collect auth tokens for services that need them
    tokens: dict[str, str] = {}
    for key in selected:
        svc = _MCP_SERVICES.get(key)
        if svc and svc.get("needs_token"):
            token = prompter.password(svc["token_prompt"])
            tokens[key] = token

    # Generate mcp.yaml
    _write_mcp_yaml(root, selected, tokens)
    prompter.note(f"MCP config written to {root / 'mcp.yaml'}", title="MCP")

    return {**env, "_MCP_ENABLED": "1"}


def _write_mcp_yaml(root: Path, selected: list[str], tokens: dict[str, str]) -> None:
    """Write mcp.yaml from wizard selections."""
    lines = ["enabled: true", "connections:"]
    for key in selected:
        svc = _MCP_SERVICES.get(key)
        if svc is None:
            continue
        lines.append(f"  - server_id: {svc['server_id']}")
        lines.append(f"    url: \"{svc['url']}\"")
        lines.append(f"    auth:")
        lines.append(f"      auth_type: {svc['auth_type']}")
        token = tokens.get(key, "")
        if token:
            lines.append(f"      token: \"{token}\"")
        lines.append(f"    enabled: true")
    (root / "mcp.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")


# -- Helpers ------------------------------------------------------------------


def _find_codex_auth(env: dict[str, str]) -> str:
    """Find existing Codex auth file. Returns path string or empty."""
    override = env.get("CODEX_AUTH_FILE", "").strip()
    if override:
        expanded = Path(override).expanduser()
        if expanded.exists():
            return str(expanded)

    home = Path.home()
    candidates = [
        home / ".codex" / "auth.json",
        home / ".config" / "codex" / "auth.json",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return ""


def _detect_default_provider(env: dict[str, str]) -> str:
    """Detect which provider to default-select based on existing env."""
    if env.get("AZURE_OPENAI_ENDPOINT", "").strip():
        return "azure"
    if env.get("CUSTOM_API_BASE_URL", "").strip():
        return "custom"
    return "codex"


def _probe_telegram_token(token: str) -> bool:
    """Probe Telegram API to verify bot token."""
    try:
        import httpx

        resp = httpx.get(
            f"https://api.telegram.org/bot{token}/getMe",
            timeout=3.0,
        )
        return resp.status_code == 200 and resp.json().get("ok", False)
    except Exception:
        return False


def _probe_azure(endpoint: str, api_key: str) -> bool:
    """Probe Azure OpenAI endpoint."""
    try:
        import httpx

        url = f"{endpoint.rstrip('/')}/openai/models?api-version=2024-10-21"
        resp = httpx.get(url, headers={"api-key": api_key}, timeout=5.0)
        return resp.status_code in {200, 401, 403}  # reachable even if auth fails
    except Exception:
        return False


def _probe_custom_api(base_url: str, api_key: str) -> bool:
    """Probe custom API endpoint."""
    try:
        import httpx

        url = f"{base_url.rstrip('/')}/models"
        resp = httpx.get(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=5.0,
        )
        return resp.status_code in {200, 401, 403}
    except Exception:
        return False


def _port_available(host: str, port: int) -> bool:
    """Check if a port is available for binding."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        return False
    with sock:
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, port))
        except OSError:
            return False
    return True


def _parse_services(raw: str) -> list[str]:
    """Parse comma-separated services string into list."""
    return [s.strip().lower() for s in raw.split(",") if s.strip()]
