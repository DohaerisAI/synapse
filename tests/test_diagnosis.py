from __future__ import annotations

from pathlib import Path

import pytest

from synapse.diagnosis import (
    DiagnosisEngine,
    DiagnosisReport,
    Gap,
    Improvement,
)
from synapse.models import NormalizedInboundEvent, RunState
from synapse.store import SQLiteStore


@pytest.fixture()
def store(tmp_path: Path) -> SQLiteStore:
    db = SQLiteStore(tmp_path / "test.sqlite3")
    db.initialize()
    return db


def _make_event(**overrides) -> NormalizedInboundEvent:
    defaults = {
        "adapter": "telegram",
        "channel_id": "ch1",
        "user_id": "u1",
        "message_id": "m1",
        "text": "hello",
    }
    return NormalizedInboundEvent(**{**defaults, **overrides})


def test_gap_model():
    gap = Gap(
        category="skill",
        description="No skill for Slack integration",
        frequency=5,
        example_intents=["connect to slack", "send slack message"],
    )
    assert gap.category == "skill"
    assert gap.frequency == 5
    assert len(gap.example_intents) == 2


def test_improvement_model():
    imp = Improvement(
        gap=Gap(category="skill", description="missing X", frequency=3),
        suggestion="Create a skill for X",
        priority="high",
    )
    assert imp.priority == "high"
    assert imp.gap.frequency == 3


def test_diagnosis_report_empty():
    report = DiagnosisReport(
        total_runs=0,
        completed_runs=0,
        failed_runs=0,
        gaps=[],
        improvements=[],
        run_states={},
    )
    assert report.health_score == 1.0


def test_diagnosis_report_with_failures():
    report = DiagnosisReport(
        total_runs=10,
        completed_runs=7,
        failed_runs=3,
        gaps=[Gap(category="skill", description="missing X", frequency=1)],
        improvements=[],
        run_states={"COMPLETED": 7, "FAILED": 3},
    )
    assert report.health_score == pytest.approx(0.7)


def test_diagnosis_engine_empty_store(store: SQLiteStore):
    engine = DiagnosisEngine(store=store)
    report = engine.analyze_runs()
    assert report.total_runs == 0
    assert report.health_score == 1.0
    assert report.gaps == []


def test_diagnosis_engine_with_completed_runs(store: SQLiteStore):
    for i in range(5):
        event = _make_event(message_id=f"m{i}")
        run = store.create_run(f"session_{i}", event)
        store.set_run_state(run.run_id, RunState.COMPLETED)

    engine = DiagnosisEngine(store=store)
    report = engine.analyze_runs()
    assert report.total_runs == 5
    assert report.completed_runs == 5
    assert report.failed_runs == 0
    assert report.health_score == 1.0


def test_diagnosis_engine_with_failed_runs(store: SQLiteStore):
    for i in range(4):
        event = _make_event(message_id=f"m{i}")
        run = store.create_run(f"session_{i}", event)
        store.set_run_state(run.run_id, RunState.COMPLETED)

    event = _make_event(message_id="mfail")
    run = store.create_run("session_fail", event)
    store.set_run_state(run.run_id, RunState.FAILED)

    engine = DiagnosisEngine(store=store)
    report = engine.analyze_runs()
    assert report.total_runs == 5
    assert report.failed_runs == 1
    assert report.health_score == pytest.approx(0.8)


def test_diagnosis_engine_detect_gaps_from_events(store: SQLiteStore):
    event = _make_event(message_id="m1")
    run = store.create_run("s1", event)
    store.append_run_event(
        run.run_id, "s1", "action.unsupported",
        {"action": "slack.send", "reason": "no capability"},
    )
    store.set_run_state(run.run_id, RunState.FAILED)

    engine = DiagnosisEngine(store=store)
    report = engine.analyze_runs()
    assert len(report.gaps) >= 1
    assert any("slack" in g.description.lower() for g in report.gaps)


def test_diagnosis_report_to_dict():
    report = DiagnosisReport(
        total_runs=10,
        completed_runs=8,
        failed_runs=2,
        gaps=[Gap(category="skill", description="missing X", frequency=1)],
        improvements=[
            Improvement(
                gap=Gap(category="skill", description="missing X", frequency=1),
                suggestion="Add skill X",
                priority="medium",
            )
        ],
        run_states={"COMPLETED": 8, "FAILED": 2},
    )
    data = report.to_dict()
    assert data["total_runs"] == 10
    assert len(data["gaps"]) == 1
    assert len(data["improvements"]) == 1
    assert data["health_score"] == pytest.approx(0.8)
