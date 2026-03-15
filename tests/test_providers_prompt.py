from __future__ import annotations

from synapse.providers import CodexCliProvider
from synapse.models import AuthProfile


def test_codex_provider_build_prompt_tolerates_missing_role_and_content(tmp_path):
    profile = AuthProfile(provider="codex", model="gpt-5.4", source="test")
    provider = CodexCliProvider(profile, client=None, workdir=str(tmp_path))
    prompt = provider._build_prompt([{"text": "approved"}], system_prompt="hi")
    assert "Conversation:" in prompt
    assert "approved" in prompt
