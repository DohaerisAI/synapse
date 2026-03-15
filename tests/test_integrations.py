from synapse.integrations import IntegrationRegistry
from synapse.models import IntegrationStatus, NormalizedInboundEvent
from synapse.runtime import build_runtime


def test_integration_registry_scaffolds_and_applies(tmp_path) -> None:
    skills_dir = tmp_path / "skills"
    registry = IntegrationRegistry(
        tmp_path / "integrations",
        skills_dir=skills_dir,
        boot_path=tmp_path / "BOOT.md",
        env={"GITHUB_TOKEN": "token"},
    )
    registry.initialize()

    proposed = registry.propose("github")
    scaffolded = registry.scaffold(proposed.integration_id)
    tested = registry.test(proposed.integration_id)
    applied = registry.apply(proposed.integration_id)

    assert scaffolded.status is IntegrationStatus.SCAFFOLDED
    assert tested.status is IntegrationStatus.TESTED
    assert applied.status is IntegrationStatus.ACTIVE
    assert (skills_dir / "github" / "SKILL.md").exists()
    assert "activate github" in (tmp_path / "BOOT.md").read_text(encoding="utf-8")


async def test_gateway_nl_integration_goes_to_react_loop(tmp_path, monkeypatch) -> None:
    """NL integration requests now go through react loop (not deterministic path)."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    runtime = build_runtime(tmp_path)

    result = await runtime.gateway.ingest(
        NormalizedInboundEvent(
            adapter="telegram",
            channel_id="22",
            user_id="44",
            message_id="1",
            text="add integration github",
        )
    )

    # React loop runs but no model is configured → "[No model available.]"
    assert result.status == "COMPLETED"


def test_runtime_reactivates_approved_integrations_on_restart(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    runtime = build_runtime(tmp_path)
    runtime.integrations.initialize()
    runtime.integrations.propose("github")
    runtime.integrations.scaffold("github")
    runtime.integrations.test("github")
    runtime.integrations.apply("github")

    restarted = build_runtime(tmp_path)
    integration = restarted.integrations.get("github")

    assert integration is not None
    assert integration.status is IntegrationStatus.ACTIVE
    assert "github" in restarted.skills.skills
