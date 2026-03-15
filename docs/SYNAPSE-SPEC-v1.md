# Synapse Engineering Specification v1.0

**Document type:** Engineering Specification
**Repository:** `DohaerisAI/synapse`
**Python:** ≥ 3.12
**Status:** Draft
**Date:** 2026-03-09

---

## 0. Terminology

| Term | Definition |
|------|-----------|
| **Capability** | A registered action string (e.g. `finance.holdings.read`) in `CapabilityRegistry` |
| **Skill** | A directory under `skills/` containing `manifest.json` + `SKILL.md` |
| **MCP** | Model Context Protocol — a standardized server exposing tools to LLM agents |
| **Broker** | `CapabilityBroker` — decides `allowed`, `requires_approval`, `executor` for every action |
| **Self-model** | `SelfModel` Pydantic schema — typed representation of identity, architecture, capabilities, limitations, health |
| **Diagnosis** | `DiagnosisEngine` — analyzes `run_events` for `action.unsupported` patterns, outputs `DiagnosisReport` |
| **Skill codegen** | The pipeline where the agent generates new skill code, tests it, and proposes it for human approval |
| **Human gate** | Any point where execution pauses for explicit user approval before proceeding |

---

## 1. Scope

This spec covers three systems to be added to the existing Synapse runtime:

1. **SPEC-CODEGEN** — Skill code generation pipeline
2. **SPEC-EVOLVE** — Diagnosis → Propose → Approve → Deploy evolution loop
3. **SPEC-FINANCE** — Financial advisor domain plugin (equities, mutual funds, portfolio analysis, swing trading)

Each system is defined by: data models, interfaces, state transitions, file layouts, capability registrations, broker policies, and acceptance criteria.

---

## 2. SPEC-CODEGEN — Skill Code Generation

### 2.1. Purpose

Enable Synapse to generate new skill code at runtime when it detects a capability gap, test the generated code in a sandbox, and deploy it only after human approval.

### 2.2. Existing code touched

| File | Change type |
|------|-------------|
| `synapse/models.py` | Add `SkillProposal`, `SkillProposalStatus` |
| `synapse/capabilities.py` | Add `skill.generate`, `skill.test`, `skill.propose`, `skill.deploy` actions to `DEFAULT_CAPABILITY_REGISTRY` |
| `synapse/broker.py` | Add broker rules for `skill.*` action family |
| `synapse/store.py` | Add `skill_proposals` table |
| `synapse/skills.py` | Add `SkillGenerator` class, extend `SkillRegistry.deploy()` |
| `synapse/hooks.py` | Existing — fire `skill.proposed`, `skill.deployed` events |
| `synapse/introspection.py` | Extend `discover_limitations()` to reflect codegen status |
| `synapse/self_model.py` | No schema change needed — capabilities list auto-updates |

### 2.3. Data models

Add to `synapse/models.py`:

```python
class SkillProposalStatus(StrEnum):
    DRAFT = "DRAFT"              # LLM generated, not yet tested
    TESTING = "TESTING"          # Sandbox tests running
    TEST_PASSED = "TEST_PASSED"  # All tests green
    TEST_FAILED = "TEST_FAILED"  # Tests failed, needs regen or discard
    PROPOSED = "PROPOSED"        # Awaiting human approval
    APPROVED = "APPROVED"        # Human approved, deploying
    DEPLOYED = "DEPLOYED"        # Live in skill registry
    REJECTED = "REJECTED"        # Human rejected

class SkillProposal(BaseModel):
    proposal_id: str
    trigger_run_id: str | None = None        # Run that triggered the gap
    trigger_gap: str | None = None           # Gap description from diagnosis
    skill_id: str                            # Proposed skill ID
    skill_name: str
    skill_description: str
    capabilities_provided: list[str]         # What actions this skill enables
    capabilities_required: list[str]         # What MCPs/tools it depends on
    risk_level: str                          # "safe" | "risky" | "approval-required"
    generated_code: str                      # The Python skill code
    generated_manifest: str                  # manifest.json content
    generated_skill_md: str                  # SKILL.md content
    generated_tests: str                     # Test code
    test_results: str = ""                   # stdout/stderr from test run
    test_passed: bool = False
    status: SkillProposalStatus
    model_used: str = ""                     # Which LLM generated this
    created_at: str
    updated_at: str
```

### 2.4. Store schema

Add to `SQLiteStore.initialize()`:

```sql
CREATE TABLE IF NOT EXISTS skill_proposals (
    proposal_id TEXT PRIMARY KEY,
    trigger_run_id TEXT,
    trigger_gap TEXT,
    skill_id TEXT NOT NULL,
    skill_name TEXT NOT NULL,
    skill_description TEXT NOT NULL,
    capabilities_provided TEXT NOT NULL,  -- JSON array
    capabilities_required TEXT NOT NULL,  -- JSON array
    risk_level TEXT NOT NULL,
    generated_code TEXT NOT NULL,
    generated_manifest TEXT NOT NULL,
    generated_skill_md TEXT NOT NULL,
    generated_tests TEXT NOT NULL,
    test_results TEXT NOT NULL DEFAULT '',
    test_passed INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    model_used TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_skill_proposals_status ON skill_proposals(status);
```

### 2.5. Capability registrations

Add to `DEFAULT_CAPABILITY_REGISTRY` in `synapse/capabilities.py`:

```python
CapabilityDefinition("skill.generate", "skill", "Generate a new skill from a gap description.", "{gap_description, capabilities_required?}"),
CapabilityDefinition("skill.test", "skill", "Run sandbox tests on a generated skill.", "{proposal_id}"),
CapabilityDefinition("skill.propose", "skill", "Submit a tested skill for human approval.", "{proposal_id}"),
CapabilityDefinition("skill.deploy", "skill", "Deploy an approved skill to the registry.", "{proposal_id}"),
CapabilityDefinition("skill.list_proposals", "skill", "List skill proposals by status.", "{status?}"),
```

### 2.6. Broker policy

Add to `CapabilityBroker.decide()` in `synapse/broker.py`:

```python
if action == "skill.generate":
    return CapabilityDecision(allowed=True, requires_approval=False, executor="docker", reason="skill generation runs in sandbox")
if action == "skill.test":
    return CapabilityDecision(allowed=True, requires_approval=False, executor="docker", reason="skill tests run in sandbox")
if action == "skill.propose":
    return CapabilityDecision(allowed=True, requires_approval=False, executor="host", reason="proposing a skill is a read-safe notification")
if action == "skill.deploy":
    return CapabilityDecision(allowed=True, requires_approval=True, executor="host", reason="deploying a skill modifies the runtime — requires approval")
if action == "skill.list_proposals":
    return CapabilityDecision(allowed=True, requires_approval=False, executor="host", reason="listing proposals is read-only")
```

### 2.7. File layout for generated skills

```
skills/generated/{skill_id}/
├── manifest.json              # Standard Synapse skill manifest
├── SKILL.md                   # Instruction markdown
├── skill.py                   # Executable skill code
├── tests/
│   └── test_skill.py          # Auto-generated tests
└── .generation_context.json   # Provenance metadata (not loaded by SkillRegistry)
```

`.generation_context.json` schema:
```json
{
  "proposal_id": "string",
  "trigger_run_id": "string | null",
  "trigger_gap": "string | null",
  "model_used": "string",
  "generated_at": "ISO 8601",
  "approved_at": "ISO 8601",
  "approved_by": "user_id"
}
```

### 2.8. SkillGenerator class

New file: `synapse/skill_generator.py`

```python
class SkillGenerator:
    def __init__(self, *, store: SQLiteStore, skill_registry: SkillRegistry,
                 capability_registry: CapabilityRegistry, model_router: ModelRouter,
                 sandbox_root: Path, skills_root: Path) -> None: ...

    async def generate(self, gap_description: str,
                       capabilities_required: list[str] | None = None,
                       trigger_run_id: str | None = None) -> SkillProposal: ...

    async def test(self, proposal_id: str) -> SkillProposal: ...

    async def deploy(self, proposal_id: str) -> SkillDefinition: ...
```

**`generate()` contract:**
1. Query `CapabilityRegistry` and `SkillRegistry` for existing capabilities — avoid duplicates.
2. Call LLM with a structured prompt containing: gap description, available capabilities, existing skill index, manifest schema, SKILL.md conventions.
3. LLM returns: `skill.py`, `manifest.json`, `SKILL.md`, `test_skill.py` as structured output.
4. Write all files to `sandbox_root/{proposal_id}/`.
5. Create `SkillProposal` record with status `DRAFT`.
6. Return the proposal.

**`test()` contract:**
1. Set proposal status to `TESTING`.
2. Execute `pytest sandbox_root/{proposal_id}/tests/` in a subprocess with timeout (30s).
3. Capture stdout/stderr into `test_results`.
4. Set `test_passed` and status to `TEST_PASSED` or `TEST_FAILED`.
5. If `TEST_PASSED`, auto-advance status to `PROPOSED`.
6. Fire `HookEventType.SKILL_PROPOSED` (new hook event — add to `hooks.py`).

**`deploy()` contract:**
1. Verify status is `APPROVED` (set by approval resolution flow).
2. Copy files from `sandbox_root/{proposal_id}/` to `skills_root/generated/{skill_id}/`.
3. Write `.generation_context.json`.
4. Call `SkillRegistry.load()` to pick up the new skill.
5. Set proposal status to `DEPLOYED`.
6. Fire `HookEventType.SKILL_DEPLOYED` (new hook event).
7. Log capability additions to `run_events`.

### 2.9. Sandbox constraints

Generated skill code MUST NOT:
- Import `os`, `subprocess`, `sys`, `shutil`, `importlib` directly.
- Open files outside `sandbox_root/`.
- Make network calls except through registered MCP adapters.

Enforcement: static analysis pass before test execution. Reject proposals that import forbidden modules.

### 2.10. Acceptance criteria

- [ ] `skill.generate` creates a valid `SkillProposal` with `DRAFT` status and all 4 files.
- [ ] `skill.test` runs pytest in subprocess, captures results, sets `TEST_PASSED`/`TEST_FAILED`.
- [ ] Failed tests do not advance to `PROPOSED`.
- [ ] `skill.deploy` requires approval (broker returns `requires_approval=True`).
- [ ] After deploy, `SkillRegistry.skills` contains the new skill.
- [ ] After deploy, `RuntimeIntrospector.discover_capabilities()` reflects new capabilities.
- [ ] `.generation_context.json` contains correct provenance.
- [ ] Forbidden imports are rejected before test execution.
- [ ] Existing tests in `tests/test_skills_memory.py` still pass.

---

## 3. SPEC-EVOLVE — Diagnosis → Propose → Approve → Deploy

### 3.1. Purpose

Make the existing `DiagnosisEngine` actually drive the evolution loop: detect gaps → generate proposals → surface to user → deploy on approval. Currently `DiagnosisEngine.analyze_runs()` returns a `DiagnosisReport` but nothing acts on it.

### 3.2. Existing code touched

| File | Change type |
|------|-------------|
| `synapse/diagnosis.py` | Extend `DiagnosisEngine` with `propose_fixes()`, `evolution_score()` |
| `synapse/store.py` | Add `diagnosis_reports` table, `evolution_metrics` table |
| `synapse/models.py` | Add `DiagnosisProposal`, `ProposalType` |
| `synapse/hooks.py` | Add `DIAGNOSIS_PROPOSAL_CREATED`, `EVOLUTION_TICK` events |
| `synapse/capabilities.py` | Add `diagnosis.propose`, `diagnosis.evolve`, `diagnosis.history` |
| `synapse/broker.py` | Add broker rules for new diagnosis actions |
| `HEARTBEAT.md` | Update heartbeat to include weekly diagnosis summary |

### 3.3. Extended DiagnosisReport

Extend the existing `DiagnosisReport` in `synapse/diagnosis.py`:

```python
class ProposalType(StrEnum):
    NEW_SKILL = "new_skill"
    SKILL_IMPROVEMENT = "skill_improvement"
    NEW_MCP = "new_mcp"
    CONFIG_CHANGE = "config_change"
    CAPABILITY_EXTENSION = "capability_extension"

class DiagnosisProposal(BaseModel):
    proposal_type: ProposalType
    title: str
    description: str
    target_gap: Gap
    priority: str                          # "critical" | "high" | "medium" | "low"
    estimated_effort: str                  # "auto-generate" | "needs-human-setup" | "research-required"
    dependencies: list[str] = Field(default_factory=list)  # MCP URLs or capability actions needed
    auto_actionable: bool = False          # Can SPEC-CODEGEN handle this automatically?

class DiagnosisReport(BaseModel):       # EXTEND existing
    total_runs: int
    completed_runs: int
    failed_runs: int
    gaps: list[Gap] = Field(default_factory=list)
    improvements: list[Improvement] = Field(default_factory=list)
    run_states: dict[str, int] = Field(default_factory=dict)
    # NEW FIELDS:
    proposals: list[DiagnosisProposal] = Field(default_factory=list)
    strengths: list[str] = Field(default_factory=list)
    evolution_score: float = 0.0           # 0-100, computed
    report_window_hours: int = 168         # Default 7 days
    generated_at: str = ""
```

### 3.4. Store schema additions

```sql
CREATE TABLE IF NOT EXISTS diagnosis_reports (
    report_id TEXT PRIMARY KEY,
    total_runs INTEGER NOT NULL,
    completed_runs INTEGER NOT NULL,
    failed_runs INTEGER NOT NULL,
    health_score REAL NOT NULL,
    evolution_score REAL NOT NULL,
    gaps_json TEXT NOT NULL,
    proposals_json TEXT NOT NULL,
    strengths_json TEXT NOT NULL,
    window_hours INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_diagnosis_reports_created ON diagnosis_reports(created_at);

CREATE TABLE IF NOT EXISTS evolution_metrics (
    metric_id TEXT PRIMARY KEY,
    metric_type TEXT NOT NULL,      -- "skills_added" | "skills_improved" | "failure_rate" | "capability_count"
    metric_value REAL NOT NULL,
    measured_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evolution_metrics_type ON evolution_metrics(metric_type);
```

### 3.5. DiagnosisEngine extensions

Add to `synapse/diagnosis.py`:

```python
class DiagnosisEngine:
    # ... existing __init__, analyze_runs ...

    async def propose_fixes(self, report: DiagnosisReport, *, model_router: ModelRouter) -> list[DiagnosisProposal]:
        """For each gap in the report, use LLM to propose a concrete fix."""
        # 1. Build context: gap details, existing capabilities, existing skills, connected MCPs
        # 2. LLM classifies each gap into ProposalType
        # 3. LLM generates title, description, dependencies, effort estimate
        # 4. Mark auto_actionable=True if type is NEW_SKILL and all dependencies are met
        # 5. Return proposals sorted by priority

    def evolution_score(self, *, window_hours: int = 168) -> float:
        """Compute 0-100 score based on:
        - Success rate trend (improving = +points)
        - Failure rate trend (declining = +points)
        - Skills added in window
        - Gaps closed vs gaps opened
        - User approval rate on proposals (high approval = good proposals)
        """

    def persist_report(self, report: DiagnosisReport) -> str:
        """Write report to diagnosis_reports table, return report_id."""

    def persist_metric(self, metric_type: str, value: float) -> None:
        """Write a point to evolution_metrics."""
```

### 3.6. Evolution loop integration

The evolution loop runs on two triggers:

**Trigger 1: Heartbeat (scheduled)**
- During heartbeat execution (existing `HEARTBEAT.md` system), after the standard heartbeat tasks:
- Call `DiagnosisEngine.analyze_runs(window_hours=168)`.
- Call `propose_fixes()` on the result.
- For any `auto_actionable` proposals: call `SkillGenerator.generate()` + `test()` automatically.
- Surface all proposals (auto-generated and manual) to user via the heartbeat response.
- Fire `HookEventType.EVOLUTION_TICK`.

**Trigger 2: On-demand**
- User asks "what are you bad at?" / "how can you improve?" / capability `diagnosis.report`.
- Same flow but immediate, not waiting for heartbeat.

### 3.7. State machine for proposals

```
Gap detected → DiagnosisProposal created
                    │
        ┌───────────┴───────────┐
        │                       │
  auto_actionable=True    auto_actionable=False
        │                       │
  SkillGenerator.generate()     Surface to user as
        │                       "needs human setup"
  SkillGenerator.test()               │
        │                       User takes manual action
  TEST_PASSED → PROPOSED              │
        │                       (or dismisses)
  User approves/rejects
        │
  APPROVED → SkillGenerator.deploy()
        │
  DEPLOYED → Self-model updated
```

### 3.8. Acceptance criteria

- [ ] `DiagnosisEngine.propose_fixes()` returns at least one proposal for every gap with `frequency >= 2`.
- [ ] `auto_actionable` proposals trigger `SkillGenerator.generate()` during heartbeat.
- [ ] `evolution_score()` increases when skills are added and failure rate drops.
- [ ] `diagnosis_reports` table stores complete report history.
- [ ] `evolution_metrics` table tracks metrics over time for trend analysis.
- [ ] Heartbeat message includes diagnosis summary when there are gaps.
- [ ] Existing `tests/test_diagnosis.py` tests still pass.

---

## 4. SPEC-FINANCE — Financial Advisor Domain Plugin

### 4.1. Purpose

Add a financial advisor capability covering: portfolio viewing (equities + mutual funds), technical analysis, swing trade scanning, chart analysis, and market intelligence. All read operations are safe; all trade actions require approval.

### 4.2. MCP dependencies

| MCP | URL | Auth | Provides | Required |
|-----|-----|------|----------|----------|
| Zerodha Kite | `https://mcp.kite.trade/mcp` | OAuth (Zerodha login) | Holdings, positions, margins, MF holdings, orders, GTT, historical data | Yes — primary broker |
| India MF API | Via XPack (`mfapi.in`) | None | All Indian MF schemes, daily NAV, full history | Yes — MF data |
| TradingView Data | `https://mcp.tradingviewapi.com/mcp` | JWT via RapidAPI | Prices, quotes, TA scores, calendar, news | Yes — market data |
| TradingView Chart | `ertugrul59/tradingview-chart-mcp` or Chart-IMG MCP | Session cookies or API key | Chart screenshots as PNG/base64 | Optional — visual analysis |
| Indian Broker (multi) | `Sparker0i/indian-stock-mcp-agent` | Browser login | Groww + Zerodha + INDmoney unified view | Optional — multi-broker |
| HDFC Sky | HDFC Sky MCP endpoint | OAuth | Research reports, portfolio | Optional — additional broker |

### 4.3. New module: `synapse/mcp/`

```
synapse/mcp/
├── __init__.py
├── adapter.py             # Base MCP client
├── registry.py            # MCP connection registry
├── health.py              # Health check + auto-reconnect
└── security.py            # Scope checking, rate limiting, audit log
```

#### 4.3.1. MCPAdapter interface

```python
class MCPAdapter:
    def __init__(self, name: str, url: str, auth: MCPAuth | None = None) -> None: ...

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def list_tools(self) -> list[MCPToolDefinition]: ...
    async def call_tool(self, tool_name: str, params: dict[str, Any]) -> MCPToolResult: ...
    async def health_check(self) -> bool: ...

class MCPToolDefinition(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any]

class MCPToolResult(BaseModel):
    success: bool
    data: Any
    error: str | None = None

class MCPAuth(BaseModel):
    auth_type: str        # "oauth" | "jwt" | "api_key" | "none"
    config: dict[str, Any]
```

#### 4.3.2. MCPRegistry

```python
class MCPRegistry:
    def __init__(self, store: SQLiteStore) -> None: ...

    async def register(self, config: MCPConnectionConfig) -> MCPAdapter: ...
    async def unregister(self, name: str) -> None: ...
    def get(self, name: str) -> MCPAdapter | None: ...
    def list_connected(self) -> list[MCPConnectionStatus]: ...
    async def discover_all_tools(self) -> list[MCPToolDefinition]: ...
    def capabilities_from_tools(self, tools: list[MCPToolDefinition]) -> list[CapabilityDefinition]: ...
```

When `discover_all_tools()` runs, it calls `list_tools()` on every connected MCP, then calls `capabilities_from_tools()` to generate `CapabilityDefinition` entries that get added to the runtime `CapabilityRegistry`. This is how MCP tools become visible in `SELF.md` and the planner.

#### 4.3.3. Store schema

```sql
CREATE TABLE IF NOT EXISTS mcp_connections (
    name TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    auth_type TEXT NOT NULL,
    auth_config TEXT NOT NULL,          -- JSON, encrypted at rest
    status TEXT NOT NULL,               -- "connected" | "disconnected" | "error"
    last_health_check TEXT,
    tools_json TEXT,                    -- Cached tool list
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mcp_call_log (
    call_id TEXT PRIMARY KEY,
    mcp_name TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    run_id TEXT,
    params_hash TEXT NOT NULL,          -- SHA256 of params (not the params themselves)
    success INTEGER NOT NULL,
    latency_ms INTEGER NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mcp_call_log_mcp ON mcp_call_log(mcp_name);
```

### 4.4. Finance skill set

```
skills/finance/
├── manifest.json
├── SKILL.md
└── sub-skills defined via capabilities
```

`manifest.json`:
```json
{
  "id": "finance",
  "name": "Financial Advisor",
  "description": "Portfolio analysis, mutual fund tracking, technical analysis, swing trade scanning, and market intelligence. All trade actions require approval.",
  "capabilities": [
    "finance.holdings.read",
    "finance.positions.read",
    "finance.mf.holdings",
    "finance.mf.nav_history",
    "finance.mf.sip_xirr",
    "finance.technical.analyze",
    "finance.technical.scan",
    "finance.chart.capture",
    "finance.chart.analyze",
    "finance.sentiment.analyze",
    "finance.macro.summary",
    "finance.trade.suggest",
    "finance.trade.gtt_place",
    "finance.portfolio.summary",
    "finance.portfolio.risk"
  ]
}
```

### 4.5. Capability registrations

Add to `DEFAULT_CAPABILITY_REGISTRY`:

```python
# Finance — Read (safe)
CapabilityDefinition("finance.holdings.read", "finance", "Read equity holdings from connected broker.", "{}"),
CapabilityDefinition("finance.positions.read", "finance", "Read open positions from connected broker.", "{}"),
CapabilityDefinition("finance.mf.holdings", "finance", "Read mutual fund holdings from broker.", "{}"),
CapabilityDefinition("finance.mf.nav_history", "finance", "Fetch NAV history for a mutual fund scheme.", "{scheme_code}"),
CapabilityDefinition("finance.mf.sip_xirr", "finance", "Compute XIRR for SIP investments.", "{scheme_code, sip_dates?, sip_amounts?}"),
CapabilityDefinition("finance.technical.analyze", "finance", "Run technical analysis on a symbol.", "{symbol, indicators?}"),
CapabilityDefinition("finance.technical.scan", "finance", "Scan a universe for swing trade setups.", "{universe?, criteria?}"),
CapabilityDefinition("finance.chart.capture", "finance", "Capture a TradingView chart screenshot.", "{symbol, interval, indicators?}"),
CapabilityDefinition("finance.chart.analyze", "finance", "Analyze a chart image using vision.", "{symbol, interval}"),
CapabilityDefinition("finance.sentiment.analyze", "finance", "Analyze news and social sentiment for a symbol.", "{symbol}"),
CapabilityDefinition("finance.macro.summary", "finance", "Summarize macro context: global cues, FII/DII, yields, crude.", "{}"),
CapabilityDefinition("finance.portfolio.summary", "finance", "Generate portfolio summary across all connected brokers.", "{}"),
CapabilityDefinition("finance.portfolio.risk", "finance", "Analyze portfolio risk: sector concentration, correlation, VaR.", "{}"),

# Finance — Write (approval required)
CapabilityDefinition("finance.trade.suggest", "finance", "Generate a trade suggestion with entry/exit/SL.", "{symbol, direction}"),
CapabilityDefinition("finance.trade.gtt_place", "finance", "Place a GTT order on Zerodha.", "{symbol, trigger_price, quantity, order_type}"),
```

### 4.6. Broker policy for finance

Add to `CapabilityBroker.decide()`:

```python
if action.startswith("finance.") and action.endswith(".read"):
    return CapabilityDecision(allowed=True, requires_approval=False, executor="host", reason="finance read operations are safe")
if action in {"finance.mf.holdings", "finance.mf.nav_history", "finance.mf.sip_xirr",
              "finance.technical.analyze", "finance.technical.scan",
              "finance.chart.capture", "finance.chart.analyze",
              "finance.sentiment.analyze", "finance.macro.summary",
              "finance.portfolio.summary", "finance.portfolio.risk"}:
    return CapabilityDecision(allowed=True, requires_approval=False, executor="host", reason="finance analysis is read-only")
if action == "finance.trade.suggest":
    return CapabilityDecision(allowed=True, requires_approval=False, executor="host", reason="suggestions are advisory only")
if action == "finance.trade.gtt_place":
    return CapabilityDecision(allowed=True, requires_approval=True, executor="host", reason="placing orders requires explicit approval")
```

### 4.7. Finance executor

New file: `synapse/finance/executor.py`

The finance executor maps `finance.*` actions to MCP tool calls:

```python
class FinanceExecutor:
    def __init__(self, mcp_registry: MCPRegistry, model_router: ModelRouter) -> None: ...

    async def execute(self, action: PlannedAction) -> ExecutionResult:
        """Route finance.* actions to the appropriate MCP tool calls."""
        match action.action:
            case "finance.holdings.read":
                return await self._kite_call("get_holdings")
            case "finance.mf.holdings":
                return await self._kite_call("get_mf_holdings")
            case "finance.mf.nav_history":
                return await self._mfapi_call("get_nav_history", action.payload)
            case "finance.technical.analyze":
                return await self._technical_analysis(action.payload)
            case "finance.chart.capture":
                return await self._chart_capture(action.payload)
            case "finance.chart.analyze":
                img = await self._chart_capture(action.payload)
                return await self._vision_analyze(img, action.payload)
            # ... etc

    async def _kite_call(self, tool_name: str, params: dict | None = None) -> ExecutionResult:
        adapter = self.mcp_registry.get("kite")
        if adapter is None:
            return ExecutionResult(action="finance.*", success=False,
                                  detail="Kite MCP not connected. Connect via: mcp.kite.trade/mcp")
        result = await adapter.call_tool(tool_name, params or {})
        return ExecutionResult(action="finance.*", success=result.success,
                              detail=str(result.data), artifacts={"raw": result.data})

    async def _vision_analyze(self, chart_result: ExecutionResult, payload: dict) -> ExecutionResult:
        """Send chart image to LLM vision for pattern analysis."""
        # Uses model_router to call a vision-capable model with the chart image
        # Returns structured analysis: patterns, S/R levels, confirmation of technical signals
```

### 4.8. MCP connection configuration

Add to `synapse/config/`:

```yaml
# config/mcp.yaml — user-edited or wizard-generated
mcps:
  kite:
    url: "https://mcp.kite.trade/mcp"
    auth:
      type: oauth
      # OAuth handled by Kite MCP server — user logs in via browser
  mfapi:
    url: "https://xpack.ai/mcp/mfapi"  # Or self-hosted
    auth:
      type: none
  tradingview:
    url: "https://mcp.tradingviewapi.com/mcp"
    auth:
      type: jwt
      config:
        rapidapi_key: "${TRADINGVIEW_RAPIDAPI_KEY}"
  tradingview-chart:
    url: "localhost:8080/mcp"  # Self-hosted chart MCP
    auth:
      type: session
      config:
        session_id: "${TRADINGVIEW_SESSION_ID}"
        session_sign: "${TRADINGVIEW_SESSION_SIGN}"
```

### 4.9. Wizard extension

Extend `synapse onboard` wizard (`synapse/wizard/`) to include:

1. **MCP setup step**: "Would you like to connect financial services?"
2. **Kite connection**: Opens browser for Zerodha OAuth, stores connection.
3. **MF data**: Auto-enabled (free, no auth).
4. **TradingView**: Optional, asks for RapidAPI key.
5. **Chart MCP**: Optional, advanced setup.

### 4.10. Acceptance criteria

- [ ] `MCPAdapter` can connect to `mcp.kite.trade/mcp` and call `list_tools()`.
- [ ] `MCPRegistry.discover_all_tools()` adds finance capabilities to `CapabilityRegistry`.
- [ ] `finance.holdings.read` returns holdings from Kite MCP.
- [ ] `finance.mf.holdings` returns MF holdings from Kite MCP.
- [ ] `finance.mf.nav_history` returns NAV data from mfapi.in.
- [ ] `finance.technical.analyze` computes RSI, MACD, Bollinger for a given symbol.
- [ ] `finance.chart.capture` returns a base64 chart image (when chart MCP connected).
- [ ] `finance.chart.analyze` returns structured pattern analysis from vision model.
- [ ] `finance.trade.gtt_place` requires approval (broker returns `requires_approval=True`).
- [ ] `finance.portfolio.summary` aggregates across all connected broker MCPs.
- [ ] All `finance.*` actions log to `mcp_call_log`.
- [ ] `synapse onboard` wizard includes MCP setup steps.

---

## 5. Integration between the three specs

### 5.1. The full loop in action

```
User asks: "Track my SIP returns vs NIFTY benchmark"
    │
    ├─ Gateway → Planner → intent: finance.mf.sip_xirr
    │
    ├─ Broker: finance.mf.sip_xirr is safe, allowed
    │
    ├─ Executor: FinanceExecutor → Kite MCP get_mf_holdings
    │                           → mfapi.in get_nav_history
    │                           → compute XIRR
    │                           → FAIL: no XIRR computation skill exists
    │
    ├─ Run event logged: action.unsupported → finance.mf.sip_xirr.compute
    │
    ├─ [Next heartbeat or on-demand diagnosis]
    │   DiagnosisEngine.analyze_runs() → Gap: "No XIRR computation" (freq: 1)
    │   DiagnosisEngine.propose_fixes() → DiagnosisProposal:
    │     type: NEW_SKILL
    │     auto_actionable: True (dependencies met: mfapi MCP connected)
    │
    ├─ SkillGenerator.generate() → sip_xirr_analyzer skill
    │   SkillGenerator.test() → TEST_PASSED
    │   Status: PROPOSED
    │
    ├─ Heartbeat message to user:
    │   "I couldn't compute SIP XIRR last time. I've generated a skill for it.
    │    Tests passed. Approve to deploy?"
    │
    ├─ User: "Yes"
    │   SkillGenerator.deploy() → skill live in registry
    │   Self-model updated: finance.mf.sip_xirr.compute now available
    │
    └─ Next time user asks: works.
```

### 5.2. pyproject.toml additions

```toml
[project]
dependencies = [
  # ... existing ...
  "httpx-sse>=0.4,<1.0",          # For MCP SSE transport
  "yfinance>=0.2,<1.0",           # Price data fallback
  "numpy>=1.26,<3.0",             # Financial computations
  "scipy>=1.12,<2.0",             # XIRR solver (newton method)
]

[project.optional-dependencies]
finance = [
  "openbb>=4.0,<5.0",             # OpenBB Platform
  "mplfinance>=0.12,<1.0",        # Chart generation
  "ta>=0.11,<1.0",                # Technical analysis indicators
]
```

### 5.3. setuptools package additions

```toml
[tool.setuptools]
packages = [
  # ... existing ...
  "synapse.mcp",
  "synapse.finance",
  "synapse.skill_generator",
]
```

---

## 6. Test plan

### 6.1. New test files

| File | Covers |
|------|--------|
| `tests/test_skill_generator.py` | SPEC-CODEGEN: generate, test, deploy lifecycle |
| `tests/test_mcp_adapter.py` | SPEC-FINANCE: MCP adapter connect, tool listing, tool calling |
| `tests/test_mcp_registry.py` | SPEC-FINANCE: registry, discovery, capability injection |
| `tests/test_finance_executor.py` | SPEC-FINANCE: action routing, MCP call mapping |
| `tests/test_evolution_loop.py` | SPEC-EVOLVE: diagnosis → proposal → codegen → deploy |
| `tests/test_broker_finance.py` | SPEC-FINANCE: broker policy for all `finance.*` actions |
| `tests/test_broker_skill.py` | SPEC-CODEGEN: broker policy for all `skill.*` actions |

### 6.2. Existing test changes

| File | Change |
|------|--------|
| `tests/test_diagnosis.py` | Add tests for `propose_fixes()`, `evolution_score()` |
| `tests/test_self_model.py` | Add tests for MCP-injected capabilities in self-model |
| `tests/test_broker.py` | Add tests for new `finance.*` and `skill.*` broker rules |
| `tests/test_introspection.py` | Add tests for MCP discovery in introspection |

### 6.3. Integration test

`tests/test_integration_full_loop.py`:
- Mock MCP server returning holdings data.
- Trigger a request that causes `action.unsupported`.
- Run diagnosis, verify gap detected.
- Run codegen, verify skill generated and tested.
- Approve, verify deployed and self-model updated.
- Re-run original request, verify success.

---

## 7. Migration

### 7.1. Database migration

New tables (`skill_proposals`, `diagnosis_reports`, `evolution_metrics`, `mcp_connections`, `mcp_call_log`) are additive. No existing tables are altered. `SQLiteStore.initialize()` uses `CREATE TABLE IF NOT EXISTS` — safe for existing deployments.

### 7.2. Backwards compatibility

All new capabilities are additive to `DEFAULT_CAPABILITY_REGISTRY`. Existing capabilities unchanged. Existing broker rules unchanged. Existing skill manifests unchanged. No breaking changes to any public interface.

---

## 8. Open questions

1. **MCP transport**: Kite uses HTTP+SSE. TradingView uses StreamableHTTP. Should `MCPAdapter` support both, or normalize to one?
2. **Chart MCP hosting**: `ertugrul59/tradingview-chart-mcp` requires Selenium + browser. Self-hosted or use Chart-IMG API (cloud, simpler but less control)?
3. **LLM routing for codegen**: Use deep-thinking model (Opus) for skill generation, quick model (Haiku) for tests? Or same model throughout?
4. **Multi-broker reconciliation**: If user has Kite + Groww + INDmoney, how to deduplicate holdings that appear in multiple brokers?
5. **Rate limiting**: Kite MCP has rate limits. How aggressive can the swing scanner be? Need to define scan frequency and batch sizes.
