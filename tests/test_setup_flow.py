from synapse.setup_flow import configure, doctor_snapshot, onboard


def test_onboard_writes_env_defaults(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    output = onboard(tmp_path, {})
    env_path = tmp_path / ".env.local"
    assert env_path.exists()
    contents = env_path.read_text(encoding="utf-8")
    assert "GWS_ENABLED=0" in contents
    assert "GWS_ALLOWED_SERVICES=gmail,calendar,drive,docs,sheets" in contents
    assert "Agent Runtime Onboard" in output


def test_configure_lists_tracked_fields(tmp_path) -> None:
    output = configure(tmp_path, {"AGENT_NAME": "AD"})
    assert "Tracked fields:" in output
    assert "AGENT_NAME=AD" in output


def test_doctor_snapshot_reports_gws(tmp_path) -> None:
    snapshot = doctor_snapshot(tmp_path, {})
    assert "gws" in snapshot
    assert "installed" in snapshot["gws"]
