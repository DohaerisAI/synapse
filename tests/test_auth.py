import json

from synapse.auth import AuthStore
from synapse.models import AuthProfile
from synapse.providers import CodexCliProvider, ModelRouter, OpenAICodexResponsesProvider


def test_auth_store_uses_local_profiles_first(tmp_path) -> None:
    auth_path = tmp_path / "auth-profiles.json"
    auth_path.write_text(
        json.dumps(
            {
                "profiles": [
                    {
                        "provider": "azure-openai",
                        "model": "gpt-5.2-chat",
                        "settings": {"api_key": "local-key", "endpoint": "https://example.test"},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    store = AuthStore(auth_path, tmp_path / "config.json", env={})
    profile = store.resolve()
    assert profile is not None
    assert profile.source == "local_profile"
    assert profile.settings["api_key"] == "local-key"


def test_auth_store_falls_back_to_codex_then_env(tmp_path) -> None:
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "auth.json").write_text(
        json.dumps({"access_token": "codex-token", "model": "gpt-5.3-codex"}),
        encoding="utf-8",
    )
    store = AuthStore(
        tmp_path / "missing.json",
        tmp_path / "config.json",
        env={
            "AZURE_OPENAI_API_KEY": "env-key",
            "AZURE_OPENAI_ENDPOINT": "https://azure.test",
        },
        home=home,
    )
    profile = store.resolve()
    assert profile is not None
    assert profile.provider == "openai-codex"
    assert profile.source == "codex_cli"

    (home / ".codex" / "auth.json").unlink()
    profile = store.resolve()
    assert profile is not None
    assert profile.provider == "azure-openai"
    assert profile.source == "environment"


def test_auth_store_reads_nested_codex_token_shape(tmp_path) -> None:
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "auth.json").write_text(
        json.dumps(
            {
                "auth_mode": "oauth-personal",
                "OPENAI_API_KEY": None,
                "tokens": {
                    "access_token": "nested-token",
                    "refresh_token": "refresh",
                    "id_token": "id",
                    "account_id": "acct",
                },
            }
        ),
        encoding="utf-8",
    )
    store = AuthStore(tmp_path / "missing.json", tmp_path / "config.json", env={}, home=home)
    profile = store.resolve()
    assert profile is not None
    assert profile.provider == "openai-codex"
    assert profile.settings["token"] == "nested-token"
    assert profile.settings["refresh_token"] == "refresh"
    assert profile.settings["account_id"] == "acct"
    assert profile.settings["transport"] == "responses"


def test_auth_store_allows_codex_model_override(tmp_path) -> None:
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "auth.json").write_text(
        json.dumps({"access_token": "codex-token", "model": "gpt-5.3-codex"}),
        encoding="utf-8",
    )
    store = AuthStore(
        tmp_path / "missing.json",
        tmp_path / "config.json",
        env={"CODEX_MODEL": "gpt-5.4"},
        home=home,
    )

    profile = store.resolve()

    assert profile is not None
    assert profile.provider == "openai-codex"
    assert profile.model == "gpt-5.4"


def test_auth_store_allows_codex_transport_override(tmp_path) -> None:
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "auth.json").write_text(
        json.dumps({"access_token": "codex-token", "model": "gpt-5.3-codex"}),
        encoding="utf-8",
    )
    store = AuthStore(
        tmp_path / "missing.json",
        tmp_path / "config.json",
        env={"CODEX_TRANSPORT": "cli"},
        home=home,
    )

    profile = store.resolve()

    assert profile is not None
    assert profile.provider == "openai-codex"
    assert profile.settings["transport"] == "cli"


async def test_codex_cli_provider_uses_cli_exec(monkeypatch, tmp_path) -> None:
    calls = {}

    async def fake_create_subprocess_exec(*command, stdout=None, stderr=None):
        calls["command"] = list(command)
        output_path = list(command)[list(command).index("-o") + 1]
        with open(output_path, "w", encoding="utf-8") as handle:
            handle.write("ok")

        class FakeProcess:
            returncode = 0
            async def communicate(self):
                return b"", b""
        return FakeProcess()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_create_subprocess_exec)
    provider = CodexCliProvider(
        AuthProfile(provider="openai-codex", model="gpt-5.3-codex", source="codex_cli", settings={"token": "unused"}),
        workdir=str(tmp_path),
    )

    result = await provider.generate([{"role": "user", "content": "say ok"}], system_prompt="be exact")

    assert result == "ok"
    assert calls["command"][:2] == ["codex", "exec"]


async def test_openai_codex_responses_provider_builds_direct_request() -> None:
    calls = {}

    class DummyResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        async def aiter_lines(self):
            yield "event: response.completed"
            yield 'data: {"response": {"output_text": "ok from responses"}}'
            yield ""

    class DummyClient:
        def stream(self, method, url, headers=None, json=None):  # noqa: A002
            calls["method"] = method
            calls["url"] = url
            calls["headers"] = headers
            calls["json"] = json

            class AsyncCM:
                async def __aenter__(self):
                    return DummyResponse()
                async def __aexit__(self, *args):
                    pass
            return AsyncCM()

    provider = OpenAICodexResponsesProvider(
        AuthProfile(
            provider="openai-codex",
            model="gpt-5.4",
            source="codex_cli",
            settings={
                "access_token": "access-token",
                "account_id": "acct_123",
                "endpoint": "https://chatgpt.com/backend-api/codex/responses",
            },
        ),
        client=DummyClient(),
    )

    result = await provider.generate([{"role": "user", "content": "say ok"}], system_prompt="be exact")

    assert result.text == "ok from responses"
    assert calls["url"] == "https://chatgpt.com/backend-api/codex/responses"
    assert calls["headers"]["Authorization"] == "Bearer access-token"
    assert calls["headers"]["ChatGPT-Account-Id"] == "acct_123"
    assert calls["json"]["model"] == "gpt-5.4"
    assert calls["json"]["store"] is False
    assert calls["json"]["stream"] is True
    assert calls["json"]["instructions"] == "be exact"


async def test_openai_codex_responses_provider_includes_inline_image_parts() -> None:
    calls = {}

    class DummyResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        async def aiter_lines(self):
            yield "event: response.completed"
            yield 'data: {"response": {"output_text": "saw image"}}'
            yield ""

    class DummyClient:
        def stream(self, method, url, headers=None, json=None):  # noqa: A002
            calls["json"] = json

            class AsyncCM:
                async def __aenter__(self):
                    return DummyResponse()
                async def __aexit__(self, *args):
                    pass
            return AsyncCM()

    provider = OpenAICodexResponsesProvider(
        AuthProfile(
            provider="openai-codex",
            model="gpt-5.4",
            source="codex_cli",
            settings={"access_token": "access-token"},
        ),
        client=DummyClient(),
    )

    result = await provider.generate(
        [
            {
                "role": "user",
                "content": "what is in this image?",
                "attachments": [
                    {"kind": "photo", "inline_data_url": "data:image/png;base64,abc123"},
                ],
            }
        ]
    )

    assert result.text == "saw image"
    assert calls["json"]["input"][0]["content"][1] == {
        "type": "input_image",
        "image_url": "data:image/png;base64,abc123",
    }


async def test_openai_codex_responses_provider_parses_stream_deltas() -> None:
    class DummyResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        async def aiter_lines(self):
            yield "event: response.output_text.delta"
            yield 'data: {"delta": "hello"}'
            yield ""
            yield "event: response.output_text.delta"
            yield 'data: {"delta": " world"}'
            yield ""

    class DummyClient:
        def stream(self, method, url, headers=None, json=None):  # noqa: A002
            class AsyncCM:
                async def __aenter__(self):
                    return DummyResponse()
                async def __aexit__(self, *args):
                    pass
            return AsyncCM()

    provider = OpenAICodexResponsesProvider(
        AuthProfile(
            provider="openai-codex",
            model="gpt-5.4",
            source="codex_cli",
            settings={"access_token": "access-token"},
        ),
        client=DummyClient(),
    )

    result = await provider.generate([{"role": "user", "content": "say hello"}])

    assert result.text == "hello world"


async def test_model_router_falls_back_to_codex_cli_when_direct_transport_fails(monkeypatch, tmp_path) -> None:
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "profiles": [
                    {
                        "provider": "openai-codex",
                        "model": "gpt-5.4",
                        "settings": {
                            "access_token": "access-token",
                            "transport": "responses",
                            "cli_fallback": True,
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    store = AuthStore(auth_path, tmp_path / "config.json", env={})
    router = ModelRouter(store, workdir=str(tmp_path))

    async def fake_stream(*args, **kwargs):
        raise RuntimeError("responses failed")

    class FailingClient:
        def stream(self, *args, **kwargs):
            class AsyncCM:
                async def __aenter__(self):
                    raise RuntimeError("responses failed")
                async def __aexit__(self, *args):
                    pass
            return AsyncCM()

    router.client = FailingClient()

    async def fake_create_subprocess_exec(*command, stdout=None, stderr=None):
        output_path = list(command)[list(command).index("-o") + 1]
        with open(output_path, "w", encoding="utf-8") as handle:
            handle.write("ok from cli")

        class FakeProcess:
            returncode = 0
            async def communicate(self):
                return b"", b""
        return FakeProcess()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_create_subprocess_exec)

    result = await router.generate([{"role": "user", "content": "say ok"}], system_prompt="be exact")

    assert result == "ok from cli"
