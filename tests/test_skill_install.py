import json
import zipfile
from pathlib import Path

import httpx
import pytest

from synapse.app import create_app
from synapse.runtime import build_runtime


def _write_bundle_dir(
    bundle_dir: Path,
    *,
    skill_id: str,
    description: str,
    tool_name: str,
    instruction: str,
) -> Path:
    bundle_dir.mkdir(parents=True, exist_ok=True)
    (bundle_dir / "manifest.json").write_text(
        json.dumps(
            {
                "id": skill_id,
                "name": skill_id.title(),
                "description": description,
                "tools": [
                    {
                        "name": tool_name,
                        "description": f"{tool_name} tool",
                        "parameters": {"type": "object", "properties": {}},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (bundle_dir / "SKILL.md").write_text(instruction, encoding="utf-8")
    return bundle_dir


def _zip_bundle(bundle_dir: Path, zip_path: Path) -> Path:
    with zipfile.ZipFile(zip_path, "w") as archive:
        for path in sorted(bundle_dir.rglob("*")):
            archive.write(path, path.relative_to(bundle_dir))
    return zip_path


def test_runtime_install_skill_from_directory_registers_skill_and_tool(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    runtime = build_runtime(tmp_path)
    bundle_dir = _write_bundle_dir(
        tmp_path / "incoming" / "market-scan",
        skill_id="market-scan",
        description="scans the market",
        tool_name="scan_watchlist",
        instruction="# Market Scan\n\nScan the watchlist.",
    )

    try:
        summary = runtime.install_skill(str(bundle_dir))

        assert summary["skill_id"] == "market-scan"
        assert runtime.skills.get("market-scan") is not None
        assert runtime.gateway.tool_registry.get("skill.market-scan.scan_watchlist") is not None

        registry_path = tmp_path / "var" / "skills" / "registry" / "market-scan.json"
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        assert registry["source"]["type"] == "directory"
        assert registry["checksum"] == summary["checksum"]
    finally:
        runtime.shutdown()


@pytest.mark.anyio
async def test_api_install_skill_from_zip_registers_skill_and_tool(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    bundle_dir = _write_bundle_dir(
        tmp_path / "bundles" / "ops-skill",
        skill_id="ops-skill",
        description="handles ops flows",
        tool_name="triage_incident",
        instruction="# Ops\n\nHandle incidents.",
    )
    zip_path = _zip_bundle(bundle_dir, tmp_path / "ops-skill.zip")
    runtime = build_runtime(tmp_path)
    app = create_app(runtime)
    transport = httpx.ASGITransport(app=app)

    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/api/skills/install", json={"source": str(zip_path)})

        assert response.status_code == 200
        payload = response.json()
        assert payload["skill_id"] == "ops-skill"
        assert payload["source"]["type"] == "zip"
        assert payload["reload"]["skills"] == ["ops-skill"]
        assert runtime.skills.get("ops-skill") is not None
        assert runtime.gateway.tool_registry.get("skill.ops-skill.triage_incident") is not None
    finally:
        runtime.shutdown()


def test_install_invalid_bundle_leaves_skills_dir_unchanged(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    runtime = build_runtime(tmp_path)
    invalid_bundle = tmp_path / "incoming" / "broken-skill"
    invalid_bundle.mkdir(parents=True, exist_ok=True)
    (invalid_bundle / "manifest.json").write_text(
        json.dumps({"id": "broken-skill", "name": "Broken", "description": "broken"}),
        encoding="utf-8",
    )

    try:
        with pytest.raises(ValueError):
            runtime.install_skill(str(invalid_bundle))

        assert not (tmp_path / "skills").exists()
        assert runtime.skills.get("broken-skill") is None
    finally:
        runtime.shutdown()


def test_install_replaces_existing_skill_and_keeps_backup(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    _write_bundle_dir(
        tmp_path / "skills" / "finance",
        skill_id="finance",
        description="first version",
        tool_name="analyze_portfolio",
        instruction="# Finance\n\nAnalyze positions.",
    )
    runtime = build_runtime(tmp_path)
    replacement = _write_bundle_dir(
        tmp_path / "incoming" / "finance-v2",
        skill_id="finance",
        description="second version",
        tool_name="rebalance_portfolio",
        instruction="# Finance\n\nRebalance positions.",
    )

    try:
        summary = runtime.install_skill(str(replacement))

        assert summary["backup_path"] is not None
        backups = list((tmp_path / "var" / "skills" / "backups").glob("*-finance"))
        assert len(backups) == 1
        backup_manifest = json.loads((backups[0] / "manifest.json").read_text(encoding="utf-8"))
        assert backup_manifest["description"] == "first version"

        skill = runtime.skills.get("finance")
        assert skill is not None
        assert skill.description == "second version"
        assert runtime.gateway.tool_registry.get("skill.finance.analyze_portfolio") is None
        assert runtime.gateway.tool_registry.get("skill.finance.rebalance_portfolio") is not None
    finally:
        runtime.shutdown()


def test_install_rejects_zip_path_traversal(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    runtime = build_runtime(tmp_path)
    zip_path = tmp_path / "bad-skill.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("manifest.json", json.dumps({"id": "bad-skill", "name": "Bad", "description": "bad"}))
        archive.writestr("SKILL.md", "# Bad\n")
        archive.writestr("../escape.txt", "escape")

    try:
        with pytest.raises(ValueError):
            runtime.install_skill(str(zip_path))

        assert not (tmp_path / "skills").exists()
        assert runtime.skills.get("bad-skill") is None
    finally:
        runtime.shutdown()
