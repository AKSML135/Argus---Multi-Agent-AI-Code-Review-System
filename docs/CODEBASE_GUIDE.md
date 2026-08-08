# Argus — Codebase Guide for New Joiners

**Read time: ~25 minutes. After this, you'll know what every file does, why it exists, and how the pieces fit together.**

---

## Table of Contents

1. [What Argus Does (2-minute version)](#1-what-argus-does)
2. [How a Review Flows — Start to Finish](#2-how-a-review-flows)
3. [Project Structure Overview](#3-project-structure-overview)
4. [Layer-by-Layer File Guide](#4-layer-by-layer-file-guide)
   - [Config & Entry Points](#41-config--entry-points)
   - [Schemas — The Shared Language](#42-schemas--the-shared-language)
   - [LLM Gateway](#43-llm-gateway)
   - [Agents](#44-agents)
   - [Graph — Orchestration Engine](#45-graph--orchestration-engine)
   - [Guardrails](#46-guardrails)
   - [Persistence](#47-persistence)
   - [API Layer](#48-api-layer)
   - [Observability](#49-observability)
   - [Evaluation Harness](#410-evaluation-harness)
5. [Key Design Decisions (and Why)](#5-key-design-decisions-and-why)
6. [Data Flow Diagrams](#6-data-flow-diagrams)
7. [How to Add a New Agent](#7-how-to-add-a-new-agent)
8. [How Tests Are Organised](#8-how-tests-are-organised)
9. [Common Gotchas](#9-common-gotchas)
10. [Glossary](#10-glossary)

---

## 1. What Argus Does

Argus is an **AI-powered code review system**. You give it a unified diff (the same format GitHub sends to CI). It runs five specialized agents in parallel, collects their findings, deduplicates them, asks a human to approve, and publishes a Markdown report.

Think of it as a small engineering team reviewing your PR:

| Agent | What it looks for |
|---|---|
| Static Analysis | Lint violations (ruff) |
| Secret Scanner | Hardcoded credentials, API keys |
| SAST | OWASP vulnerabilities (SQL injection, command injection, etc.) |
| Logic & Correctness | Off-by-one errors, missing edge cases, race conditions |
| Code Quality | Cyclomatic complexity, style, dead code |
| Documentation | Missing docstrings, stale comments, undocumented parameters |

A human must approve findings before anything is published. That approval gate is **durable** — if the server crashes, it picks back up exactly where it left off.

---

## 2. How a Review Flows — Start to Finish

```
User submits diff
       │
       ▼
[1] input_guardrail          Blocks prompt injection and oversized diffs.
       │
       ▼
[2] supervisor               Reads the diff, builds a ReviewPlan: which agents
                             to run and what token budget to give them.
       │
       ▼ (Send() fan-out — all 5 run simultaneously)
┌──────┬──────┬──────┬──────┐
│      │      │      │      │
▼      ▼      ▼      ▼      ▼
[3a]   [3b]   [3c]   [3d]   [3e]
static security logic  quality docs
       │
       ├─[3b-i]  secret_scanner  (deterministic regex)
       └─[3b-ii] sast_agent      (LLM)
       │
       │ (all 5 fan back in)
       ▼
[4] aggregator_node          Deduplicates findings that point to the same
                             file/line, runs LLM "critic" loop to remove
                             false positives, computes max_severity.
       │
       ▼
[5] output_guardrail         (run inside aggregator)
                             - Every finding's file/line is checked against
                               the actual diff. Unverifiable → low_confidence.
                             - Any secret-shaped string in descriptions → redacted.
       │
       ├── if critical findings present ──►
       │                                  ▼
       │                    [6a] gate_critical_triage
       │                         interrupt() — graph pauses.
       │                         Human reviews critical findings.
       │                         Resume: confirm → continue, reject → stop.
       │
       └── (always) ─────────────►
                                  ▼
                    [6b] gate_final_approval
                         interrupt() — graph pauses.
                         Human approves full finding set.
                         Resume: approve → report, reject/changes_requested → stop.
                              │
                              ▼
                    [7] report_generator    Synthesizes Markdown report from
                                           AggregatedFindings. Self-heals if
                                           LLM returns bad output.
                              │
                              ▼
                           __end__         Review row updated to "published".
                                           Report persisted to SQLite.
```

**Two databases:**
- `data/argus.db` — domain tables: reviews, findings, guardrail events, HITL decisions, reports.
- `data/checkpoints.db` — LangGraph's internal state snapshots (managed by `SqliteSaver`). This is what lets the server restart and resume a paused review.

---

## 3. Project Structure Overview

```
argus/
├── src/argus/               ← all application code
│   ├── config.py            ← single source of truth for every setting
│   ├── cli.py               ← Typer CLI (argus review / serve / parse-comment)
│   │
│   ├── guardrails/          ← input + output validation (run BEFORE/AFTER LLM)
│   │   ├── schemas.py       ← ALL Pydantic contracts (Finding, Report, HitlDecision…)
│   │   ├── input.py         ← injection detection, diff size check
│   │   └── output.py        ← citation check, secret redaction
│   │
│   ├── llm/                 ← LLM gateway (agents never call Groq/Gemini directly)
│   │   ├── provider.py      ← LLMProvider ABC + GroqProvider + GeminiProvider
│   │   ├── rate_limiter.py  ← async token-bucket (per provider, per minute)
│   │   └── router.py        ← primary→fallback, tenacity retry, rate limit check
│   │
│   ├── agents/              ← each agent is independently runnable
│   │   ├── base.py          ← BaseAgent Protocol (run(diff, review_id) → Finding[])
│   │   ├── registry.py      ← plugin dict: name → agent (new agents are additive)
│   │   ├── static_analysis/ ← deterministic ruff wrapper
│   │   ├── security/        ← nested subgraph: secret_scanner + sast_agent
│   │   ├── logic/           ← LLM behavioral analysis
│   │   ├── code_quality/    ← cyclomatic complexity + LLM style check
│   │   ├── documentation/   ← LLM docstring / README completeness
│   │   ├── aggregator.py    ← dedup + LLM critic loop
│   │   └── report_generator.py ← Markdown synthesis
│   │
│   ├── graph/               ← LangGraph wiring
│   │   ├── state.py         ← ReviewState TypedDict (ALL graph state in one place)
│   │   ├── graph.py         ← build_graph() + compile_graph() — the full topology
│   │   ├── checkpointer.py  ← SqliteSaver factory
│   │   └── nodes/           ← thin wrappers: call agent, return state delta
│   │       ├── guardrail.py
│   │       ├── supervisor.py
│   │       ├── workers.py
│   │       ├── aggregator.py  (+ all routing functions)
│   │       ├── hitl.py
│   │       └── report.py
│   │
│   ├── persistence/         ← SQLite (WAL mode via SQLModel)
│   │   ├── models.py        ← table definitions
│   │   └── db.py            ← engine factory, WAL pragma, session context
│   │
│   ├── api/                 ← FastAPI service
│   │   ├── app.py           ← main application, lifespan, /health, auth
│   │   ├── deps.py          ← FastAPI dependencies (auth, LLM router)
│   │   ├── main.py          ← app entry point for uvicorn
│   │   └── routers/
│   │       ├── reviews.py   ← POST/GET /reviews
│   │       ├── approvals.py ← POST /reviews/{id}/resume
│   │       └── stream.py    ← GET /reviews/{id}/stream (SSE)
│   │
│   ├── observability/       ← structured logs + traces + metrics
│   │   ├── logging.py       ← structlog (review_id bound to every line)
│   │   ├── tracing.py       ← OpenTelemetry provider setup
│   │   ├── metrics.py       ← Prometheus counters + histograms
│   │   └── decorators.py    ← @traced_node (zero changes to node body)
│   │
│   └── eval/
│       └── offline/
│           ├── harness.py   ← precision/recall/F1 per category
│           └── judge.py     ← deterministic matching + LLM-as-judge grading
│
├── eval_datasets/           ← 15 fixture PRs (seeded bugs + clean PRs)
├── fixtures/sample.patch    ← demo diff with SQL injection + leaked secret
├── tests/
│   ├── unit/                ← 14 modules, all mocked (no I/O)
│   ├── integration/         ← API, CLI, graph E2E, observability
│   └── eval/                ← harness tests (uses eval_datasets/)
├── docs/
│   ├── DEMO.md              ← scripted walkthrough for demos
│   └── CODEBASE_GUIDE.md    ← this file
├── .github/workflows/argus-review.yml  ← reference CI workflow
├── .env.example             ← all ARGUS_* settings documented
└── pyproject.toml           ← deps, entry point, ruff/mypy config
```

---

## 4. Layer-by-Layer File Guide

### 4.1 Config & Entry Points

#### `src/argus/config.py`
**What:** Single `pydantic-settings` class (`Settings`) that reads every configuration value from environment variables or a `.env` file. All settings use the `ARGUS_` prefix.

**Why:** Centralising config in one place means no agent ever hardcodes a value. Tests can override any setting by setting env vars. The `@lru_cache` wrapper ensures only one `Settings` instance is ever created per process.

**Key pattern:**
```python
# API keys are Optional so Settings() works with no .env
# Components that actually need a key call require_groq_key() and get an early error
groq_api_key: str | None = None

def require_groq_key(self) -> str:
    if not self.groq_api_key:
        raise ValueError("GROQ_API_KEY is required but not set")
    return self.groq_api_key
```

#### `src/argus/cli.py`
**What:** Typer CLI with three commands:
- `argus review` — run a diff through the pipeline locally (no server needed)
- `argus serve` — start the FastAPI server
- `argus parse-comment` — parse a GitHub PR comment for `/argus approve` or `/argus reject`

**Why:** The CLI lets the system work as a stateless CI tool without any persistent server. The `--no-wait` flag runs agents in-process; `--wait-for-approval` submits to the API and polls until a human approves.

**Key helpers:**
- `_exit_code_for_severity()` — maps max finding severity to a Unix exit code (for `--fail-on critical`)
- `_poll_for_approval()` — polls `GET /reviews/{id}` until status is no longer `awaiting_human`; exits 3 on timeout (fail-safe: blocks the merge)
- `_parse_comment_command()` — converts `/argus approve` → `{"command": "approve", "action": "approve"}`

---

### 4.2 Schemas — The Shared Language

#### `src/argus/guardrails/schemas.py`
**What:** Every cross-boundary data contract in the system, in one file. Nothing passes between components as bare dicts.

**Why:** When every agent output and graph state field is a typed Pydantic model, a malformed LLM response triggers a `ValidationError` immediately, not a `KeyError` three functions later.

**Key types:**

| Type | Purpose |
|---|---|
| `Finding` | One code-review finding (agent + category + severity + file/line + description) |
| `ReviewPlan` | Supervisor's output: which workers to run + token budget |
| `AggregatedFindings` | Deduplicated findings ready for reporting; knows `max_severity` |
| `HitlDecision` | Human's decision at a gate: gate name + action + optional comment |
| `GuardrailEvent` | Audit record for every guardrail decision (block / flag / redact) |
| `Report` | Final published Markdown report |

**Literals everywhere:**
```python
Severity = Literal["critical", "high", "medium", "low", "info"]
FindingCategory = Literal["security_flaw", "leaked_secret", "logic_bug", ...]
```
These mean `Finding(severity="crtiical", ...)` raises a `ValidationError` before the data ever leaves an agent.

---

### 4.3 LLM Gateway

The gateway is a first-class layer. Agents import `LLMRouter`, not `groq` or `google.generativeai`.

#### `src/argus/llm/provider.py`
**What:** Abstract base class (`LLMProvider`) + two concrete implementations (`GroqProvider`, `GeminiProvider`).

**Why the abstraction:** It makes provider-level fallback, rate limiting, mocking in tests, and swapping providers all a single-file concern. Agents are completely decoupled from the SDK.

The `complete_structured()` method on the base class appends a JSON schema instruction to the user message and validates the response against a Pydantic model — this is the mechanism that turns LLM free text into typed objects.

#### `src/argus/llm/rate_limiter.py`
**What:** An async token-bucket rate limiter, one bucket per provider name. Respects `ARGUS_RATE_LIMIT_RPM`.

**Why:** Groq and Gemini free-tier accounts have strict requests-per-minute limits. Without rate limiting, parallel workers would immediately saturate the quota and start hitting 429 errors.

#### `src/argus/llm/router.py`
**What:** `LLMRouter` — the single object agents hold a reference to.

**Key behaviour:**
1. Check rate limit → acquire token → call primary provider
2. If primary raises a *retryable* `LLMProviderError` → retry with tenacity (exponential backoff + jitter)
3. If primary exhausts retries → hand off to fallback provider
4. If both fail → raise `LLMError` (typed, not bare `Exception`)

`complete_structured()` additionally enforces Pydantic schema validation — `LLMOutputError` if the provider returns unparseable JSON, and this is **not** retried via fallback (it's a schema contract violation, not a transient error).

---

### 4.4 Agents

#### `src/argus/agents/base.py`
**What:** `BaseAgent` Protocol (structural subtyping — not inheritance). Every agent must expose:
```python
name: str
async def run(self, diff: str, review_id: str) -> list[Finding]: ...
```

**Why a Protocol instead of ABC:** Agents can be registered without inheriting from a common base, which makes third-party plugins easier and tests simpler (just mock the Protocol shape).

#### `src/argus/agents/registry.py`
**What:** A module-level dict (`_registry`) + `register()`, `get()`, `all_agents()` functions.

**Why:** New agents are added by registering them — no graph file needs editing. The Open/Closed Principle at the system level: you extend by adding, not by modifying.

#### `src/argus/agents/static_analysis/agent.py`
**What:** Runs `ruff --format json` as a subprocess against a temp file reconstructed from the diff, then normalises ruff's output into `Finding` objects.

**Why deterministic first:** This agent never calls an LLM. It runs fast, has zero cost, and has zero hallucination risk. It's the baseline that proves the `BaseAgent` framework works before any LLM complexity.

**Key behaviour:** Extracts added lines from the diff, writes them to a temp file, runs ruff, parses the JSON output, discards violations for lines not in the diff (to avoid flagging pre-existing issues).

#### `src/argus/agents/security/` (nested subgraph)
This is the only nested multi-agent subgraph. Three files:

**`secret_scanner.py`** — Deterministic regex-based credential scanner. Matches patterns for AWS keys, GitHub tokens, PEM headers, etc. Produces `Finding(category="leaked_secret", severity="critical")` for every match.

**`sast_agent.py`** — LLM-based OWASP vulnerability detection. Sends the diff to the LLM with a system prompt focused on OWASP Top 10 patterns. Receives a structured list of vulnerability findings.

**`supervisor.py`** — `SecuritySupervisor` runs both sub-agents concurrently (`asyncio.gather`) and merges their findings. From the parent graph's perspective, security is a single worker; internally it's two agents running in parallel.

**Why a nested subgraph:** It proves the pattern is recursive — a supervisor can contain a supervisor. Adding a third security sub-agent (e.g., dependency scanner) requires no changes to the parent graph.

#### `src/argus/agents/logic/agent.py`, `code_quality/agent.py`, `documentation/agent.py`
All three follow the same pattern:
1. Build a system prompt describing the analysis domain
2. Call `router.complete_structured(messages, schema=AgentResponseSchema)`
3. Validate the structured output
4. Map to `Finding` objects

`CodeQualityAgent` also runs a deterministic cyclomatic complexity check (AST-based) before calling the LLM, so it always produces complexity findings even with `router=None`.

#### `src/argus/agents/aggregator.py`
**What:** Two responsibilities:
1. **Deduplication** — groups findings by `(file_path, line_start, category)` and keeps the one with the highest severity (others get a `dedup_group_id` linking them to the winner)
2. **Critic loop** — sends the deduplicated findings to the LLM asking "which of these are false positives?" and removes any the LLM marks as FPs. Bounded by `ARGUS_MAX_REFINE_ITERATIONS`.

**Why the critic loop:** LLM agents individually hallucinate findings. The aggregator acts as a second-pass reviewer to remove noise before the human sees results.

#### `src/argus/agents/report_generator.py`
**What:** Takes `AggregatedFindings` and produces a Markdown report. Tries to use the LLM for a narrative summary; falls back to a deterministic template if the LLM is unavailable.

---

### 4.5 Graph — Orchestration Engine

#### `src/argus/graph/state.py`
**What:** `ReviewState` — a `TypedDict` that is the *only* data structure flowing through the LangGraph graph. Every node reads from it and writes a partial update back.

**Critical pattern:**
```python
raw_findings: Annotated[list[Finding], operator.add]
```
The `Annotated[..., operator.add]` tells LangGraph: when parallel worker branches rejoin, **append** their findings lists rather than overwriting. Without this, whichever worker finishes last would clobber the others.

Every field that can be absent before a node runs is typed `X | None`.

#### `src/argus/graph/graph.py`
**What:** The full graph topology in one place. `build_graph()` assembles the `StateGraph`; `compile_graph()` compiles it with `interrupt_before` set to the two HITL gate nodes.

**`_fan_out_workers()`** — the fan-out function. Called by LangGraph's conditional edges mechanism. Returns a list of `Send(node_name, state)` objects — one per worker in the plan. LangGraph executes these in parallel.

**`compile_graph(interrupt_before=[...])`** — this is the single line that makes HITL work. LangGraph will stop execution before any node in that list and surface the graph state to the caller.

#### `src/argus/graph/checkpointer.py`
**What:** Factory that creates a `SqliteSaver` connected to `data/checkpoints.db`.

**Why `checkpoints.db` is separate from `argus.db`:** The checkpoint file is owned by LangGraph. Application code never writes to it directly. Keeping it separate prevents schema conflicts and makes it easy to wipe just the checkpoints without losing domain data.

#### `src/argus/graph/nodes/`
Each file is a thin adapter between the graph state and an agent:

| File | What the node does |
|---|---|
| `guardrail.py` | Calls `check_input()`, writes `status="failed"` if blocked |
| `supervisor.py` | Calls supervisor agent, writes `plan` to state |
| `workers.py` | `_make_worker_node(agent)` — factory returning an async function that calls `agent.run()` and appends findings to `raw_findings` |
| `aggregator.py` | Calls `AggregatorAgent.run()`, writes `aggregated` to state; also contains all routing functions |
| `hitl.py` | Calls `interrupt()` → pauses graph; on resume, validates `HitlDecision` from caller |
| `report.py` | Calls `ReportGeneratorAgent.run()`, persists `ReportRow` to DB |

**Why nodes are thin:** Nodes shouldn't contain business logic. Business logic lives in agent classes (which are independently testable without LangGraph).

---

### 4.6 Guardrails

#### `src/argus/guardrails/input.py`
**What:** `check_input(diff, review_id, max_lines)` — runs two checks:
1. **Injection detection** — scans for patterns like "ignore previous instructions", "system:", "you are now" in the diff content. Diffs are untrusted input; this prevents a malicious diff from hijacking the LLM's behaviour.
2. **Size limit** — rejects diffs over `ARGUS_MAX_DIFF_LINES`. Oversized diffs would exceed LLM context windows and cost limits.

Returns a `GuardrailEvent` for every triggered rule. Raises `InputGuardrailError` to block the review.

#### `src/argus/guardrails/output.py`
**What:** `check_output(findings, diff, review_id)` — runs two checks on every finding:
1. **Citation check** — verifies the `file_path` and `line_start` actually exist in the diff. A finding citing `src/foo.py:42` when that file/line isn't in the diff is a hallucination; it's downgraded to `status="low_confidence"` rather than dropped (so we don't silently lose information).
2. **Secret redaction** — if a finding's `description` contains something that looks like an API key or credential, the actual value is replaced with `[REDACTED]`. The finding itself (file/line/rule) is preserved.

**Why downgrade instead of drop:** Silent deletion makes debugging impossible. `low_confidence` findings are visible in the report but marked clearly, so a human can make the final call.

---

### 4.7 Persistence

#### `src/argus/persistence/models.py`
**What:** Six SQLModel table classes, one for each domain concept:

| Table | Stores |
|---|---|
| `Review` | One row per review job. Status tracks the lifecycle: pending → running → awaiting_human → published/rejected/failed |
| `AgentRun` | One row per agent execution within a review. Tracks tokens, retries, provider used. `parent_agent_id` self-references for nested agents. |
| `FindingRow` | Persisted findings (mirrors `Finding` schema). |
| `GuardrailEvent` | Every guardrail decision: stage (input/output), rule name, action taken. |
| `HitlCheckpoint` | Each time the graph pauses for human input: gate name, what was shown to the human, what they decided. |
| `ReportRow` | The final published report markdown. One per review. |

#### `src/argus/persistence/db.py`
**What:** Engine factory (`get_engine()`) and session context manager (`get_session()`). Sets `PRAGMA journal_mode=WAL` on SQLite, which allows concurrent readers while a single writer is active — important because the FastAPI server handles multiple requests simultaneously.

---

### 4.8 API Layer

#### `src/argus/api/app.py`
**What:** The main FastAPI application. Handles the `lifespan` (DB init + logging), authentication (`verify_api_key` dependency on the `x-api-key` header), and the core routes.

**Key routes:**
- `POST /reviews` — validates the diff, persists a `Review` row, fires off `_run_review()` in a background task (returns 202 immediately), returns `{review_id, status}`.
- `GET /reviews/{id}` — queries `Review` + `FindingRow` tables, returns current status and findings.
- `POST /reviews/{id}/resume` — validates the `HitlDecision`, calls `graph.invoke(Command(resume=decision_dict))` to hand the decision back to the paused graph.
- `GET /reviews/{id}/report` — returns the persisted `ReportRow`.
- `GET /metrics` — Prometheus metrics endpoint.
- `GET /health` — liveness check.

**Why 202 not 201:** A code review takes 10–90 seconds. The HTTP response returns immediately; clients poll `GET /reviews/{id}` or stream `GET /reviews/{id}/stream`.

#### `src/argus/api/routers/stream.py`
**What:** SSE (Server-Sent Events) endpoint. Uses LangGraph's `graph.astream_events()` to emit one JSON event per node execution.

**Event shape:**
```json
{"review_id": "...", "event": "node_start", "agent": "supervisor", "elapsed_ms": 340}
```

**Why SSE instead of WebSocket:** SSE is one-directional (server → client), simpler to implement, and sufficient for progress streaming. WebSocket would add complexity with no benefit here.

#### `src/argus/api/deps.py`
**What:** FastAPI `Depends()` callables shared across routers. `verify_api_key()` reads the `x-api-key` header and raises `401` if it doesn't match `settings.api_key`. `get_llm_router()` builds an `LLMRouter` if API keys are configured, returns `None` otherwise.

---

### 4.9 Observability

#### `src/argus/observability/logging.py`
**What:** `configure_logging()` sets up structlog with JSON output and a `review_id` context variable. Every log line from any component automatically includes the current `review_id`.

**Why structlog:** Unlike Python's `logging`, structlog produces machine-readable JSON by default, which is what log aggregation systems (Datadog, ELK) need. The `review_id`-bound context means you can grep a log file for a single review ID and get its complete story.

#### `src/argus/observability/tracing.py`
**What:** OpenTelemetry provider setup. `configure_tracing(exporter)` creates a `TracerProvider` and registers it globally. `get_tracer()` returns a tracer for the `"argus"` instrumentation library.

`reset_tracing_for_tests()` is a test-only helper that installs an `InMemorySpanExporter` so tests can inspect emitted spans without a real OTLP collector.

#### `src/argus/observability/metrics.py`
**What:** Prometheus counters and histograms, all under the `argus_` prefix:

| Metric | Type | What it tracks |
|---|---|---|
| `argus_llm_calls_total` | Counter | LLM call count, labelled by provider |
| `argus_llm_retries_total` | Counter | Retry count, labelled by provider |
| `argus_hitl_wait_seconds` | Histogram | Time humans spend at each gate |
| `argus_node_duration_seconds` | Histogram | Execution time per graph node |
| `argus_reviews_total` | Counter | Completed reviews, labelled by outcome |

#### `src/argus/observability/decorators.py`
**What:** `@traced_node` — wraps a LangGraph node function in an OTel span and records node duration in Prometheus. Works on both sync and async functions.

**The key property:** Applying it requires **zero changes to the node's function body**:
```python
@traced_node           # ← add this one line
async def my_node(state: ReviewState) -> dict:
    # nothing changes here
    return {...}
```

The decorator extracts `review_id` from `state["review_id"]` automatically and adds it as a span attribute, so every trace from a single review shares the same `review_id`.

---

### 4.10 Evaluation Harness

#### `eval_datasets/`
**What:** 15 JSON fixture files, each representing a PR. Some have seeded bugs/vulnerabilities with hand-labeled expected findings; some are clean PRs (expected: zero findings).

**File format:**
```json
{
  "id": "pr_001_sql_injection",
  "description": "...",
  "tags": ["security", "sql_injection"],
  "diff": "diff --git a/...",
  "expected_findings": [
    {"category": "security_flaw", "severity": "critical",
     "file_path": "src/db.py", "must_match": true}
  ]
}
```

#### `src/argus/eval/offline/judge.py`
**What:** `FindingGrader` matches actual findings from the pipeline against expected findings. Uses two strategies:
1. **Deterministic matching** — if `file_path` and `category` match, it's a true positive. Fast, no LLM needed.
2. **LLM-as-judge** — for ambiguous cases, sends both findings to the LLM and asks "does this actual finding cover the same issue as the expected finding?" Returns a schema-validated `JudgeVerdict`.

#### `src/argus/eval/offline/harness.py`
**What:** `run_harness(eval_cases, threshold)` — runs the full pipeline against every fixture, collects `JudgeVerdict` results, computes precision/recall/F1 per `FindingCategory`, and raises `EvalThresholdError` if F1 drops below threshold.

**Why this matters for CI:** The harness is called in `tests/eval/`. If you change the aggregator logic or a system prompt, and F1 drops on the golden dataset, the test fails. This is the concrete, testable version of "how do you know your agent got better."

---

## 5. Key Design Decisions (and Why)

### "Supervisor–Worker" not "Group Chat"
Each worker agent specialises in one domain. The supervisor decides who runs. The aggregator synthesises. This mirrors how human review teams work and enables real parallelism. An AutoGen-style group chat (agents taking turns) would be nondeterministic and harder to bound.

### Schema-constrained LLM outputs everywhere
Every LLM call goes through `complete_structured(schema=MyPydanticModel)`. If the LLM returns malformed JSON, `LLMOutputError` is raised immediately. The self-heal loop at the report stage retries with a clearer prompt. There are no silent `None` returns.

### Two databases, not one
`argus.db` (domain data) is written by application code. `checkpoints.db` (LangGraph state) is written only by the framework. Mixing them would create schema coupling with LangGraph's internals.

### Deterministic agents before LLM agents
`StaticAnalysisAgent` and `SecretScannerAgent` never call an LLM. They run first, are cheap, and are always correct. LLM agents supplement them; they don't replace them.

### Guardrails are structural, not bolted on
Input guardrails run before the supervisor sees the diff. Output guardrails run after every agent and again after the aggregator. The diff is always treated as untrusted input. This isn't a safety feature added at the end — it's part of the graph topology.

### `Annotated[list[Finding], operator.add]` on `raw_findings`
Without this, parallel workers would overwrite each other's findings as their branches converged. The `operator.add` reducer tells LangGraph to append rather than replace. This is the LangGraph-specific pattern that makes parallel fan-out actually work correctly.

---

## 6. Data Flow Diagrams

### Finding lifecycle

```
Agent produces Finding (severity="high", file_path="src/db.py", line_start=42)
         │
         ▼
output guardrail:
  - Is file_path in the diff?  YES → status stays "open"
                               NO  → status = "low_confidence"
  - Does description contain a secret? → redact it
         │
         ▼
aggregator:
  - Group by (file_path, line_start, category)
  - If 3 agents flagged the same line: keep highest severity, set dedup_group_id
  - LLM critic: "is this a false positive?" → if yes, status = "false_positive"
         │
         ▼
HITL gate: human sees the finding in the approval payload
         │
         ▼
report_generator: finding appears in the Markdown report
         │
         ▼
persistence: FindingRow written to argus.db
```

### State flow through LangGraph

```
ReviewState = {
  review_id: "abc-123",
  diff: "diff --git ...",
  plan: None,           ← supervisor fills this
  raw_findings: [],     ← workers append to this (operator.add)
  aggregated: None,     ← aggregator fills this
  hitl_critical_decision: None,   ← gate fills this on resume
  hitl_final_decision: None,      ← gate fills this on resume
  report: None,         ← report_generator fills this
  status: "pending",
  error: None,
}
```

Each node returns a **partial dict** — only the keys it changes. LangGraph merges it into the full state.

---

## 7. How to Add a New Agent

Say you want to add a "License Compliance" agent.

**Step 1 — Create `src/argus/agents/license/agent.py`:**
```python
from argus.agents.base import BaseAgent
from argus.guardrails.schemas import Finding

class LicenseAgent(BaseAgent):
    name = "license_compliance"

    async def run(self, diff: str, review_id: str) -> list[Finding]:
        # your implementation here
        return findings
```

**Step 2 — Register it in the supervisor node** (`src/argus/graph/nodes/supervisor.py`):

The supervisor builds the `ReviewPlan` with a list of worker names. Add `"license_compliance"` to the workers list it returns.

**Step 3 — Add the node to the graph** (`src/argus/graph/graph.py`):
```python
license_agent = LicenseAgent()
worker_agents["license_compliance"] = license_agent
```

The `_fan_out_workers()` function already handles any worker name in the plan — no other graph code changes.

**Step 4 — Add a fixture to `eval_datasets/`** with a diff that has a license issue and hand-label the expected finding.

**Step 5 — Add a test in `tests/unit/`** covering the agent's `run()` method with a mock router.

No existing file needs modification beyond the supervisor and graph assembly. This is the Open/Closed Principle in practice.

---

## 8. How Tests Are Organised

```
tests/
├── unit/                   No I/O, no DB, all LLM calls mocked
│   ├── test_config.py      Settings loads, missing keys raise at right time
│   ├── test_schemas.py     Pydantic validation, Literal enforcement
│   ├── test_db.py          Round-trip read/write to tmp SQLite
│   ├── test_base_agent.py  Registry register/get/list
│   ├── test_input_guardrail.py   Injection patterns, size limits
│   ├── test_output_guardrail.py  Citation check, secret redaction
│   ├── test_router.py      Fallback triggers, retry count, rate limit
│   ├── test_static_analysis.py   Lint violation → Finding mapping
│   ├── test_worker_agents.py     Logic/quality/doc agents with mocked router
│   ├── test_security.py    Secret scanner patterns, SAST via mocked router
│   ├── test_aggregator.py  Dedup logic, critic loop with mocked router
│   ├── test_hitl.py        Gate nodes, interrupt behaviour
│   ├── test_report_generator.py  Report template + mocked LLM path
│   └── test_m15_polish.py  README/env completeness, ruff clean check
│
├── integration/            Real SQLite (tmp_path), LLM mocked
│   ├── test_graph_e2e.py   Full graph run, HITL resume, checkpointing
│   ├── test_api.py         FastAPI routes with TestClient
│   ├── test_m11_api.py     M11 acceptance criteria (SSE, auth, HITL via API)
│   ├── test_m12_observability.py  Spans emitted, metrics populated
│   └── test_cli.py         CLI commands with mocked agents
│
└── eval/
    └── test_offline_harness.py   Harness against fixture dataset
```

**Testing philosophy:**
- Unit tests mock at the LLM router level. The agent under test receives pre-crafted `Finding` lists.
- Integration tests use `tmp_path` SQLite so tests are isolated and leave no state behind.
- `conftest.py` at the test root provides the `isolated_db` fixture used by integration tests.
- Zero real LLM calls in any test. All 300 tests pass without API keys.

---

## 9. Common Gotchas

**1. `get_settings()` is cached**
`get_settings` uses `@lru_cache`. Tests that set env vars with `monkeypatch.setenv` must call `get_settings.cache_clear()` before and after, or the cached `Settings` instance will ignore the env var change.

**2. `raw_findings` uses `operator.add`, not `=`**
If you ever add a new parallel branch to the graph and it stores results in a plain list field (without the `Annotated[list, operator.add]`), results from earlier branches will be overwritten. Always use the annotated form for any field that multiple parallel nodes write to.

**3. `argus.db` vs `checkpoints.db`**
Never query `checkpoints.db` from application code. If you need to know graph state, call `graph.get_state(config)`. The checkpoint format is LangGraph's internal serialisation; it can change between LangGraph versions.

**4. `interrupt()` must be in `interrupt_before` in `compile_graph()`**
LangGraph only pauses at nodes declared in `interrupt_before`. If you add a new HITL gate node but forget to add it to `compile_graph(interrupt_before=[...])`, the `interrupt()` call inside the node will silently do nothing and execution will continue.

**5. `.env` prefix**
All settings use `ARGUS_` prefix. Setting `GROQ_API_KEY` (without the prefix) in your shell will do nothing. You need `ARGUS_GROQ_API_KEY`.

**6. Tests clear the `_engine` singleton**
`persistence/db.py` caches the SQLAlchemy engine in `_engine`. Tests that use `isolated_db` must set `db_module._engine = None` before and after to prevent the cached engine (pointing at the real DB) from being used.

---

## 10. Glossary

| Term | Meaning in this codebase |
|---|---|
| **diff / patch** | A unified diff: the input Argus operates on |
| **Finding** | One identified code issue: category + severity + file + line + description |
| **ReviewPlan** | Supervisor's output: list of worker agent names to dispatch + token budget |
| **AggregatedFindings** | Post-dedup, post-critic merged findings ready for human review |
| **HITL** | Human-in-the-loop: a pause in the graph that requires human input to resume |
| **interrupt()** | LangGraph primitive that suspends a graph at a node and surfaces state to the caller |
| **SqliteSaver** | LangGraph's durable checkpoint store — what makes HITL survive a process restart |
| **Send()** | LangGraph primitive for parallel fan-out: dispatches the same state to multiple nodes simultaneously |
| **Annotated[list, operator.add]** | LangGraph state reducer that appends across parallel branches instead of overwriting |
| **LLMRouter** | The single object agents use to call an LLM; handles fallback, retry, rate limits |
| **GuardrailEvent** | Structured audit record every time a guardrail blocks, flags, or redacts something |
| **traced_node** | Decorator that wraps a graph node in an OTel span + Prometheus timer, zero body changes |
| **EvalThresholdError** | Raised by the harness when F1 drops below threshold — exits non-zero to block CI |
| **WAL mode** | SQLite `journal_mode=WAL`: allows concurrent readers with one writer |
| **dedup_group_id** | UUID assigned by the aggregator to group findings that point to the same issue |
| **Critic loop** | LLM-based false-positive removal run by the aggregator; bounded by `max_refine_iterations` |
