from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class Identity(BaseModel):
    name: str
    version: str
    purpose: str
    personality: str = ""


class ComponentInfo(BaseModel):
    name: str
    role: str
    module: str
    sub_components: list[str] = Field(default_factory=list)


class Architecture(BaseModel):
    components: list[ComponentInfo] = Field(default_factory=list)

    def component_names(self) -> list[str]:
        return [c.name for c in self.components]


class Limitation(BaseModel):
    area: str
    description: str


class HealthSnapshot(BaseModel):
    runs_by_state: dict[str, int]
    pending_approvals: int
    pending_inputs: int
    queued_events: int
    adapters_healthy: int
    adapters_total: int

    @property
    def total_runs(self) -> int:
        return sum(self.runs_by_state.values())

    @property
    def failure_rate(self) -> float:
        total = self.total_runs
        if total == 0:
            return 0.0
        failed = self.runs_by_state.get("FAILED", 0)
        return failed / total


class PerformanceMetrics(BaseModel):
    total_runs: int = 0
    completed_runs: int = 0
    failed_runs: int = 0
    avg_duration_seconds: float | None = None

    @property
    def success_rate(self) -> float:
        if self.total_runs == 0:
            return 0.0
        return self.completed_runs / self.total_runs


class SelfModel(BaseModel):
    identity: Identity
    architecture: Architecture
    capabilities: list[str] = Field(default_factory=list)
    limitations: list[Limitation] = Field(default_factory=list)
    health: HealthSnapshot
    performance: PerformanceMetrics

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()

    def summary(self) -> str:
        cap_count = len(self.capabilities)
        lim_count = len(self.limitations)
        comp_count = len(self.architecture.components)
        lines = [
            f"{self.identity.name} v{self.identity.version}",
            f"{self.identity.purpose}",
            f"{comp_count} components, {cap_count} capabilities, {lim_count} limitation{'s' if lim_count != 1 else ''}",
            f"Health: {self.health.total_runs} runs, {self.health.failure_rate:.0%} failure rate",
        ]
        return " | ".join(lines)
