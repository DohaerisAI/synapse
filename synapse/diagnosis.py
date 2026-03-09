from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .models import RunState
from .store import SQLiteStore


class Gap(BaseModel):
    category: str
    description: str
    frequency: int = 1
    example_intents: list[str] = Field(default_factory=list)


class Improvement(BaseModel):
    gap: Gap
    suggestion: str
    priority: str = "medium"


class DiagnosisReport(BaseModel):
    total_runs: int
    completed_runs: int
    failed_runs: int
    gaps: list[Gap] = Field(default_factory=list)
    improvements: list[Improvement] = Field(default_factory=list)
    run_states: dict[str, int] = Field(default_factory=dict)

    @property
    def health_score(self) -> float:
        if self.total_runs == 0:
            return 1.0
        return self.completed_runs / self.total_runs

    def to_dict(self) -> dict[str, Any]:
        data = self.model_dump()
        data["health_score"] = self.health_score
        return data


class DiagnosisEngine:
    def __init__(self, *, store: SQLiteStore) -> None:
        self._store = store

    def analyze_runs(self, *, window_hours: int = 24) -> DiagnosisReport:
        runs = self._store.list_runs(limit=500)

        state_counts: dict[str, int] = {}
        for run in runs:
            state_counts[run.state.value] = state_counts.get(run.state.value, 0) + 1

        total = len(runs)
        completed = state_counts.get(RunState.COMPLETED.value, 0)
        failed = state_counts.get(RunState.FAILED.value, 0)

        gaps = self._detect_gaps_from_events(runs)
        improvements = self._suggest_improvements(gaps)

        return DiagnosisReport(
            total_runs=total,
            completed_runs=completed,
            failed_runs=failed,
            gaps=gaps,
            improvements=improvements,
            run_states=state_counts,
        )

    def _detect_gaps_from_events(self, runs: list) -> list[Gap]:
        gap_map: dict[str, Gap] = {}

        for run in runs:
            if run.state != RunState.FAILED:
                continue
            events = self._store.list_run_events(run.run_id)
            for event in events:
                if event["event_type"] == "action.unsupported":
                    payload = event["payload"]
                    action = payload.get("action", "unknown") if isinstance(payload, dict) else "unknown"
                    reason = payload.get("reason", "") if isinstance(payload, dict) else ""
                    key = action
                    if key in gap_map:
                        gap_map[key] = Gap(
                            category="capability",
                            description=f"Unsupported action: {action} ({reason})",
                            frequency=gap_map[key].frequency + 1,
                            example_intents=gap_map[key].example_intents,
                        )
                    else:
                        gap_map[key] = Gap(
                            category="capability",
                            description=f"Unsupported action: {action} ({reason})",
                            frequency=1,
                        )

        return list(gap_map.values())

    def _suggest_improvements(self, gaps: list[Gap]) -> list[Improvement]:
        improvements = []
        for gap in gaps:
            priority = "high" if gap.frequency >= 5 else "medium" if gap.frequency >= 2 else "low"
            improvements.append(
                Improvement(
                    gap=gap,
                    suggestion=f"Create a skill or capability to handle: {gap.description}",
                    priority=priority,
                )
            )
        return improvements
