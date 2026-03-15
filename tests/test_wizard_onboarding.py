"""Full flow tests for the onboarding wizard using MockPrompter."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from synapse.wizard.onboarding import run_onboarding_wizard
from synapse.wizard.prompter import MockPrompter


@pytest.fixture()
def tmp_root(tmp_path: Path) -> Path:
    return tmp_path


def _env_file(root: Path) -> dict[str, str]:
    """Read the .env.local file as a dict."""
    path = root / ".env.local"
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


# -- Helper: build answer lists for common flows ---


def _codex_provider_answers() -> list:
    """Answers for Codex CLI provider sub-flow."""
    return [
        "codex",       # provider selection
        "",            # auth file path (empty = auto)
        "gpt-5.4",     # model
        "responses",   # transport
    ]


def _azure_provider_answers() -> list:
    """Answers for Azure provider sub-flow."""
    return [
        "azure",                              # provider selection
        "https://myorg.openai.azure.com",     # endpoint
        "sk-test-key-123",                    # API key
        "gpt-5.2-chat",                       # model
        "",                                    # deployment (blank = use model)
        "2024-10-21",                          # API version
    ]


def _custom_provider_answers() -> list:
    """Answers for Custom API provider sub-flow."""
    return [
        "custom",                          # provider selection
        "https://api.example.com/v1",      # base URL
        "sk-custom-key",                   # API key
        "my-model",                        # model
        "chat",                            # transport
    ]


class TestFlowSelection:
    def test_prompts_flow_when_not_specified(self, tmp_root: Path):
        """When flow="" (default), wizard prompts for flow selection."""
        answers = [
            # flow selection
            "quickstart",
            # step_agent
            "TestBot", "",
            # step_provider (codex)
            *_codex_provider_answers(),
        ]
        prompter = MockPrompter(answers)

        run_onboarding_wizard(tmp_root, prompter=prompter, process_env={}, flow="")

        env = _env_file(tmp_root)
        assert env.get("AGENT_NAME") == "TestBot"
        assert prompter.remaining == 0

    def test_skips_flow_prompt_when_specified(self, tmp_root: Path):
        """When flow="quickstart", no flow prompt shown."""
        answers = [
            "TestBot", "",
            *_codex_provider_answers(),
        ]
        prompter = MockPrompter(answers)

        run_onboarding_wizard(tmp_root, prompter=prompter, process_env={}, flow="quickstart")

        env = _env_file(tmp_root)
        assert env.get("AGENT_NAME") == "TestBot"
        assert prompter.remaining == 0


class TestQuickstartFlow:
    def test_quickstart_codex(self, tmp_root: Path):
        """QuickStart flow with Codex CLI provider."""
        answers = [
            "TestBot", "",
            *_codex_provider_answers(),
        ]
        prompter = MockPrompter(answers)

        run_onboarding_wizard(tmp_root, prompter=prompter, process_env={}, flow="quickstart")

        env = _env_file(tmp_root)
        assert env.get("AGENT_NAME") == "TestBot"
        assert env.get("CODEX_MODEL") == "gpt-5.4"
        assert env.get("CODEX_TRANSPORT") == "responses"
        assert prompter.remaining == 0

    def test_quickstart_azure(self, tmp_root: Path):
        """QuickStart flow with Azure provider."""
        answers = [
            "MyAgent", "Be helpful",
            *_azure_provider_answers(),
        ]
        prompter = MockPrompter(answers)

        run_onboarding_wizard(tmp_root, prompter=prompter, process_env={}, flow="quickstart")

        env = _env_file(tmp_root)
        assert env.get("AGENT_NAME") == "MyAgent"
        assert env.get("AZURE_OPENAI_ENDPOINT") == "https://myorg.openai.azure.com"
        assert env.get("AZURE_OPENAI_MODEL") == "gpt-5.2-chat"
        assert prompter.remaining == 0

    def test_quickstart_custom_api(self, tmp_root: Path):
        """QuickStart flow with Custom API provider."""
        answers = [
            "Bot", "",
            *_custom_provider_answers(),
        ]
        prompter = MockPrompter(answers)

        run_onboarding_wizard(tmp_root, prompter=prompter, process_env={}, flow="quickstart")

        env = _env_file(tmp_root)
        assert env.get("CUSTOM_API_BASE_URL") == "https://api.example.com/v1"
        assert env.get("CODEX_MODEL") == "my-model"
        assert env.get("CODEX_TRANSPORT") == "chat"
        assert prompter.remaining == 0


class TestAdvancedFlow:
    def test_full_advanced_all_disabled(self, tmp_root: Path):
        """Advanced flow with telegram/gws/mcp/heartbeat all disabled."""
        answers = [
            # step_agent
            "Synapse", "",
            # step_provider (codex)
            *_codex_provider_answers(),
            # step_telegram
            False,           # enable? no
            # step_gws
            False,           # enable? no
            # step_mcp
            False,           # enable? no
            # step_heartbeat
            False,           # enable? no
            # step_server
            "127.0.0.1", "8000",
        ]
        prompter = MockPrompter(answers)

        run_onboarding_wizard(tmp_root, prompter=prompter, process_env={}, flow="advanced")

        env = _env_file(tmp_root)
        assert env.get("AGENT_NAME") == "Synapse"
        assert env.get("TELEGRAM_POLLING_ENABLED") == "0"
        assert env.get("GWS_ENABLED") == "0"
        assert env.get("HEARTBEAT_ENABLED") == "0"
        assert prompter.remaining == 0

    def test_advanced_with_heartbeat(self, tmp_root: Path):
        """Advanced flow with heartbeat enabled."""
        answers = [
            "Bot", "",
            *_codex_provider_answers(),
            False,           # telegram disabled
            False,           # gws disabled
            False,           # mcp disabled
            True,            # heartbeat enabled
            "15",            # interval
            "last",          # target
            "silent_ok",     # ack mode
            "0.0.0.0", "9000",
        ]
        prompter = MockPrompter(answers)

        run_onboarding_wizard(tmp_root, prompter=prompter, process_env={}, flow="advanced")

        env = _env_file(tmp_root)
        assert env.get("HEARTBEAT_ENABLED") == "1"
        assert env.get("HEARTBEAT_EVERY_MINUTES") == "15"
        assert prompter.remaining == 0

    def test_advanced_with_gws(self, tmp_root: Path):
        """Advanced flow with GWS enabled."""
        answers = [
            "Bot", "",
            *_codex_provider_answers(),
            False,                      # telegram disabled
            True,                       # gws enabled
            ["gmail", "calendar"],      # services
            "",                         # gws extra instructions
            False,                      # mcp disabled
            False,                      # heartbeat disabled
            "127.0.0.1", "8000",
        ]
        prompter = MockPrompter(answers)

        run_onboarding_wizard(tmp_root, prompter=prompter, process_env={}, flow="advanced")

        env = _env_file(tmp_root)
        assert env.get("GWS_ENABLED") == "1"
        assert "gmail" in env.get("GWS_ALLOWED_SERVICES", "")
        assert prompter.remaining == 0

    def test_advanced_azure_provider(self, tmp_root: Path):
        """Advanced flow with Azure OpenAI provider."""
        answers = [
            "Bot", "",
            *_azure_provider_answers(),
            False, False, False, False,
            "127.0.0.1", "8000",
        ]
        prompter = MockPrompter(answers)

        run_onboarding_wizard(tmp_root, prompter=prompter, process_env={}, flow="advanced")

        env = _env_file(tmp_root)
        assert env.get("AZURE_OPENAI_ENDPOINT") == "https://myorg.openai.azure.com"
        assert env.get("AZURE_OPENAI_API_KEY") == "sk-test-key-123"
        assert env.get("AZURE_OPENAI_MODEL") == "gpt-5.2-chat"
        assert env.get("AZURE_OPENAI_DEPLOYMENT") == "gpt-5.2-chat"
        assert env.get("AZURE_OPENAI_API_VERSION") == "2024-10-21"
        assert prompter.remaining == 0

    def test_advanced_custom_provider(self, tmp_root: Path):
        """Advanced flow with Custom API provider."""
        answers = [
            "Bot", "",
            *_custom_provider_answers(),
            False, False, False, False,
            "127.0.0.1", "8000",
        ]
        prompter = MockPrompter(answers)

        run_onboarding_wizard(tmp_root, prompter=prompter, process_env={}, flow="advanced")

        env = _env_file(tmp_root)
        assert env.get("CUSTOM_API_BASE_URL") == "https://api.example.com/v1"
        assert env.get("CUSTOM_API_KEY") == "sk-custom-key"
        assert env.get("CUSTOM_API_MODEL") == "my-model"
        assert prompter.remaining == 0


class TestCodexOAuth:
    def test_oauth_success(self, tmp_root: Path):
        """Codex OAuth flow with successful authentication."""
        fake_creds = {
            "access_token": "test-token",
            "refresh_token": "test-refresh",
            "email": "user@example.com",
        }

        answers = [
            "Bot", "",
            "codex_oauth",   # provider selection
            "gpt-5.4",       # model
            "responses",     # transport
        ]
        prompter = MockPrompter(answers)

        with patch("synapse.wizard.oauth.run_codex_oauth", return_value=fake_creds):
            run_onboarding_wizard(
                tmp_root, prompter=prompter, process_env={}, flow="quickstart",
            )

        env = _env_file(tmp_root)
        assert env.get("CODEX_MODEL") == "gpt-5.4"
        assert env.get("CODEX_TRANSPORT") == "responses"
        assert prompter.remaining == 0

    def test_oauth_fallback_on_error(self, tmp_root: Path):
        """When OAuth fails, falls back to manual Codex CLI flow."""
        from synapse.wizard.oauth import OAuthError

        answers = [
            "Bot", "",
            "codex_oauth",   # provider selection → will fail
            # fallback to codex CLI manual flow:
            "",              # auth file
            "gpt-5.4",       # model
            "responses",     # transport
        ]
        prompter = MockPrompter(answers)

        with patch(
            "synapse.wizard.oauth.run_codex_oauth",
            side_effect=OAuthError("browser failed"),
        ):
            run_onboarding_wizard(
                tmp_root, prompter=prompter, process_env={}, flow="quickstart",
            )

        env = _env_file(tmp_root)
        assert env.get("CODEX_MODEL") == "gpt-5.4"
        assert prompter.remaining == 0


class TestSkipFlags:
    def test_skip_telegram(self, tmp_root: Path):
        answers = [
            "Bot", "",
            *_codex_provider_answers(),
            # no telegram prompt
            False,           # gws disabled
            False,           # mcp disabled
            False,           # heartbeat disabled
            "127.0.0.1", "8000",
        ]
        prompter = MockPrompter(answers)

        run_onboarding_wizard(
            tmp_root, prompter=prompter, process_env={}, flow="advanced",
            skip_telegram=True,
        )
        assert prompter.remaining == 0

    def test_skip_gws(self, tmp_root: Path):
        answers = [
            "Bot", "",
            *_codex_provider_answers(),
            False,  # telegram
            # no gws prompt
            False,  # mcp disabled
            False,  # heartbeat
            "127.0.0.1", "8000",
        ]
        prompter = MockPrompter(answers)

        run_onboarding_wizard(
            tmp_root, prompter=prompter, process_env={}, flow="advanced",
            skip_gws=True,
        )
        assert prompter.remaining == 0


class TestExistingConfig:
    def test_keep_existing(self, tmp_root: Path):
        env_path = tmp_root / ".env.local"
        env_path.write_text("AGENT_NAME=OldBot\n")

        answers = ["keep"]
        prompter = MockPrompter(answers)

        run_onboarding_wizard(tmp_root, prompter=prompter, process_env={}, flow="advanced")

        assert "OldBot" in env_path.read_text()
        assert prompter.remaining == 0

    def test_reset_existing(self, tmp_root: Path):
        env_path = tmp_root / ".env.local"
        env_path.write_text("AGENT_NAME=OldBot\n")

        answers = [
            "reset",
            "NewBot", "",
            *_codex_provider_answers(),
            False, False, False, False,
            "127.0.0.1", "8000",
        ]
        prompter = MockPrompter(answers)

        run_onboarding_wizard(tmp_root, prompter=prompter, process_env={}, flow="advanced")

        env = _env_file(tmp_root)
        assert env.get("AGENT_NAME") == "NewBot"

    def test_update_existing(self, tmp_root: Path):
        env_path = tmp_root / ".env.local"
        env_path.write_text("AGENT_NAME=OldBot\nCODEX_MODEL=gpt-4\n")

        answers = [
            "update",
            "OldBot", "",
            *_codex_provider_answers(),
            False, False, False, False,
            "127.0.0.1", "8000",
        ]
        prompter = MockPrompter(answers)

        run_onboarding_wizard(tmp_root, prompter=prompter, process_env={}, flow="advanced")

        env = _env_file(tmp_root)
        assert env.get("AGENT_NAME") == "OldBot"


class TestDirectories:
    def test_ensures_directories(self, tmp_root: Path):
        answers = ["Bot", "", *_codex_provider_answers()]
        prompter = MockPrompter(answers)

        run_onboarding_wizard(tmp_root, prompter=prompter, process_env={}, flow="quickstart")

        for d in ["var", "memory", "skills", "integrations"]:
            assert (tmp_root / d).is_dir()


class TestEnvFileWritten:
    def test_env_file_contains_azure_fields(self, tmp_root: Path):
        """Env file includes Azure fields when Azure provider selected."""
        answers = ["Bot", "", *_azure_provider_answers()]
        prompter = MockPrompter(answers)

        run_onboarding_wizard(tmp_root, prompter=prompter, process_env={}, flow="quickstart")

        content = (tmp_root / ".env.local").read_text()
        assert "AZURE_OPENAI_ENDPOINT=https://myorg.openai.azure.com" in content
        assert "AZURE_OPENAI_API_KEY=sk-test-key-123" in content

    def test_env_file_contains_custom_fields(self, tmp_root: Path):
        """Env file includes Custom API fields when custom provider selected."""
        answers = ["Bot", "", *_custom_provider_answers()]
        prompter = MockPrompter(answers)

        run_onboarding_wizard(tmp_root, prompter=prompter, process_env={}, flow="quickstart")

        content = (tmp_root / ".env.local").read_text()
        assert "CUSTOM_API_BASE_URL=https://api.example.com/v1" in content
        assert "CUSTOM_API_KEY=sk-custom-key" in content
        assert "CUSTOM_API_MODEL=my-model" in content
