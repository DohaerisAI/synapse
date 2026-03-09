from __future__ import annotations

from pathlib import Path

import pytest

from synapse.self_model import (
    Architecture,
    ComponentInfo,
    HealthSnapshot,
    Identity,
    Limitation,
    PerformanceMetrics,
    SelfModel,
)


def test_identity_from_defaults():
    identity = Identity(
        name="Synapse",
        version="0.1.0",
        purpose="Agent runtime",
    )
    assert identity.name == "Synapse"
    assert identity.version == "0.1.0"
    assert identity.purpose == "Agent runtime"
    assert identity.personality == ""


def test_identity_with_personality():
    identity = Identity(
        name="Synapse",
        version="0.1.0",
        purpose="Agent runtime",
        personality="Helpful and concise",
    )
    assert identity.personality == "Helpful and concise"


def test_component_info():
    comp = ComponentInfo(
        name="Gateway",
        role="Central orchestration engine",
        module="synapse.gateway",
    )
    assert comp.name == "Gateway"
    assert comp.module == "synapse.gateway"
    assert comp.sub_components == []


def test_component_info_with_sub_components():
    comp = ComponentInfo(
        name="Gateway",
        role="orchestration",
        module="synapse.gateway",
        sub_components=["planner", "executor", "renderer"],
    )
    assert len(comp.sub_components) == 3


def test_architecture():
    arch = Architecture(
        components=[
            ComponentInfo(name="Gateway", role="orchestration", module="synapse.gateway"),
            ComponentInfo(name="Store", role="persistence", module="synapse.store"),
        ]
    )
    assert len(arch.components) == 2
    assert arch.component_names() == ["Gateway", "Store"]


def test_architecture_empty():
    arch = Architecture(components=[])
    assert arch.component_names() == []


def test_limitation():
    lim = Limitation(
        area="self-modification",
        description="Cannot apply code patches automatically",
    )
    assert lim.area == "self-modification"


def test_health_snapshot():
    snap = HealthSnapshot(
        runs_by_state={"COMPLETED": 10, "FAILED": 2},
        pending_approvals=1,
        pending_inputs=0,
        queued_events=0,
        adapters_healthy=1,
        adapters_total=1,
    )
    assert snap.total_runs == 12
    assert snap.failure_rate == pytest.approx(2 / 12)


def test_health_snapshot_zero_runs():
    snap = HealthSnapshot(
        runs_by_state={},
        pending_approvals=0,
        pending_inputs=0,
        queued_events=0,
        adapters_healthy=0,
        adapters_total=0,
    )
    assert snap.total_runs == 0
    assert snap.failure_rate == 0.0


def test_performance_metrics_defaults():
    metrics = PerformanceMetrics()
    assert metrics.total_runs == 0
    assert metrics.completed_runs == 0
    assert metrics.failed_runs == 0
    assert metrics.avg_duration_seconds is None
    assert metrics.success_rate == 0.0


def test_performance_metrics_with_data():
    metrics = PerformanceMetrics(
        total_runs=100,
        completed_runs=90,
        failed_runs=10,
        avg_duration_seconds=2.5,
    )
    assert metrics.success_rate == 0.9


def test_self_model_to_dict():
    model = SelfModel(
        identity=Identity(name="Synapse", version="0.1.0", purpose="runtime"),
        architecture=Architecture(components=[]),
        capabilities=[],
        limitations=[],
        health=HealthSnapshot(
            runs_by_state={},
            pending_approvals=0,
            pending_inputs=0,
            queued_events=0,
            adapters_healthy=0,
            adapters_total=0,
        ),
        performance=PerformanceMetrics(),
    )
    data = model.to_dict()
    assert data["identity"]["name"] == "Synapse"
    assert isinstance(data["architecture"], dict)
    assert isinstance(data["capabilities"], list)
    assert isinstance(data["health"], dict)


def test_self_model_summary():
    model = SelfModel(
        identity=Identity(name="Synapse", version="0.1.0", purpose="runtime"),
        architecture=Architecture(
            components=[
                ComponentInfo(name="Gateway", role="orchestration", module="synapse.gateway"),
            ]
        ),
        capabilities=["gws.gmail.send", "memory.read"],
        limitations=[
            Limitation(area="code", description="no auto-apply"),
        ],
        health=HealthSnapshot(
            runs_by_state={"COMPLETED": 5},
            pending_approvals=0,
            pending_inputs=0,
            queued_events=0,
            adapters_healthy=1,
            adapters_total=1,
        ),
        performance=PerformanceMetrics(total_runs=5, completed_runs=5),
    )
    summary = model.summary()
    assert "Synapse" in summary
    assert "0.1.0" in summary
    assert "2 capabilities" in summary
    assert "1 limitation" in summary
