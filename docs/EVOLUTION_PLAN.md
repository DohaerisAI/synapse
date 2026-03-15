# Synapse Evolution Plan: Self-Aware & Self-Improving Runtime

## Overview

Transform Synapse from a human-operated agent runtime into a self-aware, self-improving system. Inspired by OpenClaw's philosophy but next-gen: Synapse knows what it is, diagnoses its own gaps, authors its own plugins, and continuously evolves.

---

## Current State Assessment

### What We Have (Strong Foundation)
- Plugin system (discovery, loading, registry) with channel/skill/hook kinds
- Integration lifecycle: PROPOSED -> SCAFFOLDED -> TESTED -> APPROVED -> ACTIVE
- Capability broker with approval gating
- Hook system for lifecycle events
- Memory store (session/user/global markdown)
- Code patch proposals (disabled in MVP)
- Skill registry with keyword matching
- Health checks via `synapse doctor`

### What's Missing
- No **self-model** -- Synapse doesn't know its own architecture/capabilities at runtime
- No **introspection engine** -- can't inspect its own code, measure performance, identify gaps
- No **autonomous improvement loop** -- can't decide "I need a new skill for X" and build it
- No **self-plugin authoring** -- can't write plugin code, manifests, and register them
- Code patch apply is **hard-disabled** -- no path to self-modification even with approval

---

## Phase 1: Self-Model & Identity

**File:** `synapse/self_model.py`
**Complexity:** MEDIUM | **Risk:** LOW

Create a runtime self-model that Synapse can query about itself.

### Components

```
SelfModel
  identity: Identity           # name, version, purpose, personality
  architecture: Architecture   # components, their roles, how they connect
  capabilities: list[Capability]  # what I can do right now
  limitations: list[Limitation]   # what I can't do / known gaps
  health: HealthSnapshot       # current health metrics
  performance: PerformanceMetrics  # response times, success rates, error rates
```

### Deliverables
- `synapse/self_model.py` -- SelfModel, Identity, Architecture, HealthSnapshot
- `synapse/introspection.py` -- runtime module/plugin/handler scanning
- `SELF.md` template in workspace
- API endpoint: `GET /api/self` -- returns full self-model as JSON
- New capability family: `self.*` (self.describe, self.health, self.capabilities, self.gaps)

---

## Phase 2: Self-Diagnosis & Gap Detection

**File:** `synapse/diagnosis.py`
**Complexity:** MEDIUM | **Risk:** LOW

Synapse analyzes its own performance and identifies improvement opportunities.

### Components

```
DiagnosisEngine
  analyze_runs(window_hours=24) -> DiagnosisReport
  detect_gaps() -> list[Gap]
  suggest_improvements() -> list[Improvement]
```

### Deliverables
- `synapse/diagnosis.py` -- DiagnosisEngine, Gap, Improvement, DiagnosisReport
- New hook event: `diagnosis.completed`
- Heartbeat integration: auto-diagnosis every N cycles
- Console page: `/console/diagnosis` showing gaps and suggestions

---

## Phase 3: Self-Plugin Authoring (The Forge)

**File:** `synapse/forge.py`
**Complexity:** HIGH | **Risk:** MEDIUM

Synapse can write its own plugins -- the core self-improvement mechanism.

### Components

```
PluginForge
  design_skill(gap: Gap) -> SkillBlueprint
  generate_skill(blueprint: SkillBlueprint) -> StagedPlugin
  test_skill(staged: StagedPlugin) -> TestResult
  activate_skill(staged: StagedPlugin, approved: bool) -> None
```

### Workflow
1. **Design** -- LLM generates a skill blueprint from a detected gap
2. **Generate** -- writes files to `staging/` directory (not active yet)
3. **Test** -- validates manifest, dry-runs the skill against sample inputs
4. **Gate** -- requires approval (configurable: always/risky-only/never)
5. **Activate** -- copies to `skills/`, registers in SkillRegistry

### Safety Gates
- `self_improvement.auto_approve` config (default: `false`)
- All self-authored plugins go through capability broker
- Staged plugins are sandboxed until approved
- Audit trail in SQLite

### Deliverables
- `synapse/forge.py` -- PluginForge, SkillBlueprint, StagedPlugin
- `synapse/sandbox.py` -- isolated execution environment for testing
- New capability family: `forge.*`
- New hook events: `plugin.designed`, `plugin.staged`, `plugin.self_created`
- API endpoints: `POST /api/forge/design`, `POST /api/forge/activate`

---

## Phase 4: Self-Configuration & Tuning

**File:** `synapse/tuner.py`
**Complexity:** MEDIUM | **Risk:** MEDIUM

Synapse adjusts its own configuration based on observed performance.

### Components

```
SelfTuner
  analyze_config() -> list[ConfigSuggestion]
  apply_suggestion(suggestion: ConfigSuggestion) -> None
```

### Auto-tuning Targets
- max_agent_loop_turns
- heartbeat interval
- poll interval
- planner instructions

### Guardrails
- Hard whitelist of tunable config keys (no auth/security keys)
- Evidence-based: only suggests changes backed by run data
- Rollback: keeps config history, can revert
- Min/max bounds on all tunable values

### Deliverables
- `synapse/tuner.py` -- SelfTuner, ConfigSuggestion, ConfigHistory
- Tunable config whitelist in schema
- Config history in SQLite
- API endpoint: `GET /api/tuner/suggestions`

---

## Phase 5: Autonomous Improvement Loop

**File:** `synapse/evolution.py`
**Complexity:** HIGH | **Risk:** HIGH

The full loop -- Synapse continuously improves itself.

```
Heartbeat -> Diagnose -> Detect Gaps -> Design Skill -> Stage -> Test -> [Approve] -> Activate -> Re-Diagnose
```

### Modes
- `observe` -- diagnose only, no action
- `suggest` -- diagnose + design blueprints
- `evolve` -- full autonomous loop

### Controls
- Cadence: configurable (e.g., once per hour, once per day)
- Budget: max improvements per cycle (default: 1)
- Kill switch: `evolution.enabled` config flag + `synapse evolve stop` CLI

### Deliverables
- `synapse/evolution.py` -- EvolutionLoop with modes and budget
- CLI: `synapse evolve [start|stop|status]`
- New config: `EvolutionConfig`
- Console page: `/console/evolution`
- Hook event: `evolution.cycle_completed`

---

## Phase 6: Code Self-Modification (Guarded)

**Complexity:** HIGH | **Risk:** HIGH

Un-gate `code.patch.apply` with proper safety.

### Safety Mechanisms
- Docker-isolated execution for code patches
- Git branch per patch -- never modifies main directly
- Test suite must pass before merge
- Human approval required by default
- Scope limits: `skills/`, `integrations/`, `plugins/` freely; `synapse/` core requires explicit approval

### Deliverables
- Update `broker.py` to conditionally allow `code.patch.apply`
- `synapse/patcher.py` -- GitPatcher (branch, apply, test, merge workflow)
- Scope-based approval rules in capability broker

---

## Dependency Graph

```
Phase 1 (Self-Model) ---+---> Phase 2 (Diagnosis)
                        |         |
                        |         +---> Phase 3 (Forge)
                        |         |         |
                        |         +---> Phase 4 (Tuner)
                        |         |         |
                        |         +----+----+
                        |              |
                        |              v
                        |       Phase 5 (Evolution Loop)
                        |              |
                        |              v
                        +-------> Phase 6 (Code Self-Mod)
```

## Risk Matrix

| Risk | Severity | Mitigation |
|------|----------|------------|
| Runaway self-modification | CRITICAL | Kill switch, budget limits, approval gates, scope limits |
| Low-quality self-authored skills | HIGH | Test validation, dry-run, human review option |
| Config corruption via tuner | HIGH | Hard whitelist, rollback history, min/max bounds |
| Infinite improvement loops | MEDIUM | Max iterations per cycle, cooldown periods |
| Context pollution (self-model too large) | MEDIUM | Lazy loading, summary-only in prompts |
| Breaking core code via patches | HIGH | Docker isolation, git branching, test gates |

## New Files Summary

| File | Purpose | LOC Est |
|------|---------|---------|
| `synapse/self_model.py` | Runtime self-model & identity | ~200 |
| `synapse/introspection.py` | Module/plugin/handler scanning | ~150 |
| `synapse/diagnosis.py` | Gap detection & run analysis | ~250 |
| `synapse/forge.py` | Self-plugin authoring | ~300 |
| `synapse/sandbox.py` | Isolated skill testing | ~100 |
| `synapse/tuner.py` | Self-configuration tuning | ~200 |
| `synapse/evolution.py` | Autonomous improvement loop | ~200 |
| `synapse/patcher.py` | Git-based code patching | ~150 |

**Total: ~1,600 LOC across 8 new files**
