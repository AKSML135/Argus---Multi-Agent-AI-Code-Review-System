# TASKS.md — Argus Implementation Plan (Portfolio Edition)

Derived from `ARCHITECTURE.md` v2.0. Scoped for a solo Senior/Lead AI Engineer
portfolio build: every milestone still lands one of the five signals that
matter in interviews — **multi-agent orchestration, human-in-the-loop,
durable checkpointing, guardrails, observability** — with everything else
(sandboxing, a second nested subgraph, dashboards, online eval, Postgres)
deferred to the "Future Extensions" list in the architecture doc.

**Total milestones: 15**

Sequencing logic is unchanged from v1: foundation → external integrations
(LLM gateway) → cross-cutting concerns (guardrails) → agents (deterministic
before LLM, simple before nested) → graph assembly → control-flow loops
(critic, HITL) → API surface → observability → CI → evaluation → polish.

---

## How to read this document

Each milestone is independently testable — buildable, runnable, and
verifiable in isolation via a unit test or a small script, without later
milestones existing yet. Dependencies define *build order*, not test
coupling; earlier milestones' tests mock anything not yet built.

**Complexity legend:** S = 0.5–1 day · M = 1–3 days · L = 3–5 days

## Milestone dependency overview

| # | Milestone | Depends on |
|---|---|---|
| 1 | Project Setup & Foundations (scaffold, config, contracts, DB, checkpointer) | — |
| 2 | LLM Gateway (Groq + Gemini, fallback, rate limiting) | 1 |
| 3 | Guardrails Layer (input + output) | 1 |
| 4 | Agent Framework & Static Analysis Agent | 1 |
| 5 | Parallel LLM Worker Agents (Logic, Code Quality, Documentation) | 2, 3, 4 |
| 6 | Nested Multi-Agent Subgraph: Security Supervisor | 2, 3, 4 |
| 7 | Supervisor Orchestration: Fan-Out + Checkpointer | 1, 3, 5, 6 |
| 8 | Aggregator / Critic Agent + Refinement Loop | 7 |
| 9 | HITL Gates (interrupt-based, x2) | 3, 8 |
| 10 | Report Generation & Full Graph E2E | 9 |
| 11 | FastAPI Service Layer (REST + SSE + Approval) | 10 |
| 12 | Observability (Logs, Traces, Metrics) | 10 |
| 13 | CLI + GitHub Actions CI Integration | 11 |
| 14 | Evaluation Harness (Offline) | 10 |
| 15 | Polish & Resume Readiness | 11, 12, 13, 14 |

---

## M1. Project Setup & Foundations

**Goal:** Everything needed before any real logic gets written, in one
milestone: repo scaffold, tooling, typed config, shared Pydantic contracts,
the domain DB schema, and the LangGraph checkpointer wired up. Nothing about
this milestone is interesting on its own — it exists so M2 onward has solid
ground to build on.

**Files to create:**
- `pyproject.toml`, `.gitignore`, `.pre-commit-config.yaml` (ruff, mypy)
- `src/argus/config.py` — `pydantic-settings` `Settings` (API keys, DB paths, rate limits)
- `src/argus/guardrails/schemas.py` — `Finding`, `Report`, `ReviewPlan`, `AggregatedFindings`, `HitlDecision` (Pydantic v2, matching §5 of the architecture doc)
- `src/argus/persistence/models.py` — SQLModel tables: `Review`, `AgentRun`, `Finding`, `GuardrailEvent`, `HitlCheckpoint`, `Report`
- `src/argus/persistence/db.py` — engine (WAL mode), session factory
- `src/argus/graph/checkpointer.py` — `SqliteSaver` setup against `checkpoints.db`
- `tests/unit/test_config.py`, `test_schemas.py`, `test_db.py`

**Acceptance criteria:**
- [ ] `pip install -e .[dev]` succeeds; `ruff check .`, `mypy src/`, `pytest` all run clean on the skeleton
- [ ] `severity`/`status`/`category` fields are `Literal`/`Enum`, not bare `str`; `Finding.model_validate(bad_payload)` raises `ValidationError` for a missing `file_path`
- [ ] `Settings()` loads defaults with no `.env`; missing API keys raise only when a component that needs them is instantiated, not at import time
- [ ] A round-trip test writes a `Review` + `Finding` row and reads it back
- [ ] A `SqliteSaver` checkpointer instance can be created against a throwaway DB file with no errors (no graph exists yet — just proving the plumbing works)

**Complexity:** M · **Depends on:** —

---

## M2. LLM Gateway

**Goal:** A provider-agnostic router so no agent ever calls Groq or Gemini
directly — this is what makes the fallback/rate-limit story real rather
than aspirational.

**Files to create:**
- `src/argus/llm/provider.py` — common `LLMProvider` interface, `GroqProvider`, `GeminiProvider`
- `src/argus/llm/router.py` — tries primary, falls back to secondary on error/timeout; tenacity-based retry with backoff
- `src/argus/llm/rate_limiter.py` — token-bucket limiter per provider
- `tests/unit/test_router.py` (mocked HTTP clients)

**Acceptance criteria:**
- [ ] Calling the router with the primary provider mocked to raise triggers a fallback call to the secondary provider, verified by call-count assertions
- [ ] Rate limiter rejects/queues a call once the token bucket for a provider is exhausted (unit test with a tiny bucket size)
- [ ] Retries use exponential backoff + jitter and give up after a configured max attempts, raising a typed exception (not a bare `Exception`)
- [ ] A structured-output call (Pydantic schema passed in) returns a validated model instance or raises on malformed provider output — no silent `None`

**Complexity:** M · **Depends on:** M1

---

## M3. Guardrails Layer

**Goal:** Input and output guardrails as standalone, independently testable
functions — this is a cross-cutting concern that every later agent milestone
plugs into, not something bolted on at the end.

**Files to create:**
- `src/argus/guardrails/input.py` — prompt-injection pattern detection, diff size limits
- `src/argus/guardrails/output.py` — Pydantic schema validation wrapper, citation/hallucination check (file+line exists in the diff), secret/PII redaction
- `tests/unit/test_input_guardrail.py`, `test_output_guardrail.py`

**Acceptance criteria:**
- [ ] A diff containing an injection-pattern string ("ignore previous instructions...") is flagged/blocked; a clean diff passes through unchanged
- [ ] A diff over the configured size limit is rejected with a clear typed error, not silently truncated
- [ ] A `Finding` citing a file/line not present in the diff is downgraded to `low_confidence`, never dropped silently (assert it's still in the returned list)
- [ ] A finding containing something secret-shaped (API-key pattern) has the secret masked before the function returns, with only file/line/rule metadata intact
- [ ] Every guardrail decision returns a structured `GuardrailEvent`-shaped result (stage, rule, action) suitable for persisting later

**Complexity:** M · **Depends on:** M1

---

## M4. Agent Framework & Static Analysis Agent

**Goal:** The `BaseAgent` contract every worker implements, plus the first
concrete agent — deterministic, so it proves the framework without also
depending on the LLM gateway being perfect yet.

**Files to create:**
- `src/argus/agents/base.py` — `BaseAgent` Protocol (`async def run(state) -> Finding[]`)
- `src/argus/agents/registry.py` — plugin-style registration so new agents don't require editing the graph
- `src/argus/agents/static_analysis/agent.py` — runs ruff/eslint as subprocesses, normalizes output to `Finding`
- `tests/unit/test_base_agent.py`, `test_static_analysis.py`

**Acceptance criteria:**
- [ ] A dummy agent implementing `BaseAgent` can be registered and retrieved from the registry by name
- [ ] Static Analysis Agent run against a fixture diff with a known lint violation produces a `Finding` with the correct `file_path`/`line_start`/`category`
- [ ] A clean fixture diff produces zero findings (no false positives from the wrapper itself)
- [ ] Agent registration requires no changes to any other file — proven by a test that registers a second dummy agent and confirms the first is unaffected

**Complexity:** M · **Depends on:** M1

---

## M5. Parallel LLM Worker Agents

**Goal:** Three independent LLM-based workers, each testable in isolation
with a mocked LLM response — these are the "regular" fan-out workers,
distinct from the nested Security domain built next.

**Files to create:**
- `src/argus/agents/logic/agent.py` — behavioral bugs, edge cases, coverage gaps
- `src/argus/agents/code_quality/agent.py` — style/naming (LLM) + complexity threshold (deterministic)
- `src/argus/agents/documentation/agent.py` — docstring/README completeness vs. the diff
- `tests/unit/test_logic_agent.py`, `test_code_quality_agent.py`, `test_documentation_agent.py`

**Acceptance criteria:**
- [ ] Each agent, given a fixture diff and a mocked LLM Gateway response, returns `Finding[]` matching the shared schema from M1
- [ ] Each agent's LLM call passes the diff through the guardrail's structured-output validation (from M3) — malformed mocked output triggers the same error path as M3's tests, not a new ad hoc one
- [ ] Code Quality's deterministic complexity check flags a fixture function above the configured cyclomatic-complexity threshold, independent of any LLM call
- [ ] All three agents are independently runnable via a small script against a real fixture diff (LLM mocked), proving no hidden coupling between them

**Complexity:** L · **Depends on:** M2, M3, M4

---

## M6. Nested Multi-Agent Subgraph: Security Supervisor

**Goal:** The milestone that actually proves "multi-agent," not just
"multiple agents." Security is a compiled `StateGraph` used as a single node
in the parent graph — internally it fans out to two subagents and joins
them, and the parent has no idea that's happening.

**Files to create:**
- `src/argus/agents/security/secret_scanner.py` — deterministic entropy/regex credential detection
- `src/argus/agents/security/sast_agent.py` — LLM-based injection/SSRF/authZ reasoning
- `src/argus/agents/security/supervisor.py` — compiles a small `StateGraph` fanning out to both subagents and merging their output into one `Finding[]`
- `tests/unit/test_secret_scanner.py`, `test_sast_agent.py`, `tests/integration/test_security_subgraph.py`

**Acceptance criteria:**
- [ ] Secret Scanner flags a fixture diff containing a high-entropy string matching a credential pattern; a clean diff produces zero findings
- [ ] SAST subagent, given a mocked LLM response describing an injection flaw, returns a correctly-typed `security_flaw` finding
- [ ] The compiled security subgraph, invoked as a single callable with one diff input, returns the **merged** output of both subagents — a test asserts findings from both subagents are present without knowing about the internal fan-out
- [ ] `agent_runs.parent_agent_id` (from M1's schema) is populated correctly when the subgraph persists its subagents' runs, proving the nesting is reflected in the data model
- [ ] The parent-graph-facing interface of the security subgraph is a single node with one input/output shape — asserted by a type/contract test, since this is the property that makes the hierarchy extensible

**Complexity:** L · **Depends on:** M2, M3, M4

---

## M7. Supervisor Orchestration: Fan-Out + Checkpointer

**Goal:** Wire the Supervisor, input guardrail, and all five L1 workers
(including the Security subgraph) into one compiled graph with real
parallel dispatch and a durable checkpointer — this is where "multi-agent
orchestration" stops being five separate files and becomes one system.

**Files to create:**
- `src/argus/graph/state.py` — `ReviewState` (TypedDict/Pydantic) + reducers for fan-in
- `src/argus/graph/supervisor.py` — Supervisor plan node, `fan_out` via `Send`, graph assembly through the fan-in barrier
- `src/argus/graph/nodes/` — thin wrapper nodes calling each agent from M4–M6
- `tests/integration/test_fanout_graph.py`

**Acceptance criteria:**
- [ ] Running the compiled graph (through the fan-in barrier only — no critic/HITL yet) against a fixture diff with all LLM calls mocked produces findings from all five workers, including both Security subagents
- [ ] An input diff containing an injection pattern is blocked by the input guardrail node before any worker is dispatched (assert zero worker LLM calls happen)
- [ ] Injecting a failure in one worker (mocked exception) still lets the other four complete and reach the fan-in barrier — partial failure doesn't fail the whole run
- [ ] Interrupting the process mid-run (simulated by killing and restarting against the same `checkpoints.db`) resumes from the last completed node rather than restarting from scratch — this is the checkpointer earning its place in the architecture, not just being configured
- [ ] The compiled graph's node/edge structure matches §4's diagram, checked structurally via `graph.get_graph().nodes`/`.edges`, not just by behavior

**Complexity:** L · **Depends on:** M1, M3, M5, M6

---

## M8. Aggregator / Critic Agent + Refinement Loop

**Goal:** Merge and deduplicate the fan-out output, resolve severity
conflicts, and decide whether findings are confident enough to proceed or
need a bounded refinement pass.

**Files to create:**
- `src/argus/agents/aggregator.py` — dedup by `(file_path, line_range)` overlap, severity conflict resolution, confidence scoring
- `src/argus/graph/routing.py` — `route_after_critic` conditional edge (`refine` vs `proceed`)
- `tests/unit/test_aggregator.py`, `tests/integration/test_refine_loop.py`

**Acceptance criteria:**
- [ ] Two findings from different agents on the same file/line are merged into one canonical finding with a `dedup_group_id`, both originals still retrievable (no data loss)
- [ ] Conflicting severities on the same finding resolve to the higher severity, with the resolution logged
- [ ] Low-confidence aggregate output routes back to the Security subgraph for refinement; the loop is bounded by a retry counter and a forced-exit test confirms it terminates even if confidence never improves
- [ ] `AggregatedFindings` output validates against the M1 schema before leaving the node

**Complexity:** M · **Depends on:** M7

---

## M9. HITL Gates (interrupt-based, x2)

**Goal:** The human-in-the-loop core of the project — two `interrupt()`
gates, both resumable via `Command(resume=...)`, both surviving a process
restart because of M1/M7's checkpointer.

**Files to create:**
- `src/argus/graph/nodes/gate_critical_triage.py` — fires only when max severity ≥ HIGH
- `src/argus/graph/nodes/gate_final_approval.py` — always fires before publish
- `src/argus/graph/routing.py` (extend) — `route_after_triage`, `route_after_approval`
- `tests/integration/test_hitl_gates.py`

**Acceptance criteria:**
- [ ] A run with no HIGH/CRITICAL findings skips the triage gate entirely; a run with one HIGH finding pauses at it (assert graph status is `awaiting_human` and no further nodes ran)
- [ ] Calling the graph again with `Command(resume={"action": "confirm"})` resumes execution past the triage gate to the next node
- [ ] The final-approval gate always fires regardless of severity; `"changes_requested"` loops back to `draft_report` (bounded, per M8's pattern) and `"approved"` proceeds
- [ ] Killing the process while paused at a gate and restarting against the same `checkpoints.db`, then resuming, produces the same result as an uninterrupted run — this is the concrete proof that HITL + checkpointing work together, not separately
- [ ] Every gate decision is persisted to `hitl_checkpoints` (from M1) with `gate_name`, `status`, and a snapshot of what was shown to the human

**Complexity:** L · **Depends on:** M3, M8

---

## M10. Report Generation & Full Graph E2E

**Goal:** Close the loop — draft report, output guardrail + self-heal,
publish node — then run the entire compiled graph end-to-end for the first
time.

**Files to create:**
- `src/argus/agents/report_generator.py` — synthesizes `report.md` from `AggregatedFindings`
- `src/argus/graph/nodes/output_guardrail.py`, `self_heal.py`, `draft_report.py`, `publish.py`
- `src/argus/graph/graph.py` — final assembly wiring M7–M10 into the exact §4 diagram
- `tests/integration/test_full_graph_e2e.py`

**Acceptance criteria:**
- [ ] A full run — fixture diff in, mocked LLM throughout, both HITL gates auto-approved via `Command(resume=...)` — reaches `status="published"` with a `Report` row persisted
- [ ] A deliberately malformed mocked LLM response at the report-drafting step triggers `self_heal_reprompt` and succeeds on the retry, not a crash
- [ ] Re-running the identical fixture input twice does not create duplicate `Review` rows or duplicate findings (idempotency at the whole-graph level)
- [ ] A worker failure injected mid-run (per M7) still results in a published report, with the coverage gap explicitly noted in `report.md`
- [ ] The compiled graph's structure matches §4's mermaid diagram exactly (structural test, not just behavioral)

**Complexity:** L · **Depends on:** M9

---

## M11. FastAPI Service Layer

**Goal:** Expose the compiled graph as a long-lived service: submit a
review, stream progress, approve a paused gate over HTTP.

**Files to create:**
- `src/argus/api/main.py`, `deps.py`
- `src/argus/api/routers/reviews.py` — `POST /reviews`, `GET /reviews/{id}`
- `src/argus/api/routers/approvals.py` — `POST /reviews/{id}/approve`
- `src/argus/api/routers/stream.py` — `GET /reviews/{id}/stream` (SSE from `astream_events()`)
- `tests/integration/test_api.py` (httpx.AsyncClient, mocked LLM)

**Acceptance criteria:**
- [ ] `POST /reviews` with a fixture diff returns `202 Accepted` with `{review_id, stream_url}` immediately, without blocking on graph completion
- [ ] `GET /reviews/{id}/stream` emits SSE events with a consistent shape (`review_id`, `event`, `agent`, `elapsed_ms`) — consumed and validated by a test
- [ ] When the graph pauses at a gate, `GET /reviews/{id}` reports `status="awaiting_human"`; `POST /reviews/{id}/approve` resumes it via `Command(resume=...)`
- [ ] An unauthenticated request is rejected with `401` before reaching the graph (basic auth/API-key middleware)

**Complexity:** L · **Depends on:** M10

---

## M12. Observability (Logs, Traces, Metrics)

**Goal:** Structured logs, distributed traces, and metrics automatically
attached to every graph node via a decorator — proving observability was
designed in, not added retroactively.

**Files to create:**
- `src/argus/observability/logging.py` — structlog config, `review_id`-bound context
- `src/argus/observability/tracing.py` — OpenTelemetry setup, span-per-node
- `src/argus/observability/metrics.py` — Prometheus counters/histograms
- `src/argus/observability/decorators.py` — `@traced_node`
- `tests/integration/test_observability.py`

**Acceptance criteria:**
- [ ] Running the full E2E graph (M10) with `@traced_node` applied produces one span per node execution, all sharing a single `review_id` span attribute — verified against an in-memory span exporter
- [ ] Every LLM call, retry, and human decision is traceable via `review_id` — verified by cross-referencing spans against persisted `agent_runs`/`hitl_checkpoints` rows for the same review
- [ ] The metrics endpoint exposes at minimum: LLM call count by provider, retry count, HITL wait duration
- [ ] Applying `@traced_node` to a node requires zero changes to that node's own function body — confirmed by diffing node files before/after this milestone

**Complexity:** M · **Depends on:** M10

---

## M13. CLI + GitHub Actions CI Integration

**Goal:** `argus review` for local/CI use, plus a reference GitHub Actions
workflow with non-interactive HITL handling.

**Files to create:**
- `src/argus/cli.py` — `argus review --base --head --post-comment --wait-for-approval --fail-on`, and `--diff path --no-wait` for local iteration
- `.github/workflows/argus-review.yml`
- `tests/integration/test_cli.py`

**Acceptance criteria:**
- [ ] `argus review --diff fixtures/sample.patch --no-wait` against a mocked LLM layer prints a report and exits `0` with no HITL interaction
- [ ] `argus review --wait-for-approval` polls `GET /reviews/{id}` until status leaves `awaiting_human`, and exits non-zero on timeout — **fail-safe default blocks the merge**
- [ ] `--fail-on critical` causes a non-zero exit code when a `critical` finding is present, zero otherwise (both cases tested)
- [ ] A comment-parsing test confirms `/argus approve` and `/argus reject` map to the correct `POST /reviews/{id}/approve` payload

**Complexity:** M · **Depends on:** M11

---

## M14. Evaluation Harness (Offline)

**Goal:** Prove review quality can be measured and regression-tested — a
small golden dataset with precision/recall/F1, gated the same way you'd
gate any other code change.

**Files to create:**
- `eval_datasets/` — ~15–20 versioned fixture PRs with hand-labeled expected findings (seeded bugs/vulns, a few deliberately clean PRs)
- `src/argus/eval/offline/harness.py` — runs the full pipeline against the dataset, computes precision/recall/F1 per category
- `src/argus/eval/offline/judge.py` — LLM-as-judge grading for free-form findings, schema-constrained
- `tests/eval/test_offline_harness.py`

**Acceptance criteria:**
- [ ] Running the harness against the seeded dataset produces a precision/recall/F1 score per finding category
- [ ] A deliberately-clean fixture PR produces zero findings; if the pipeline flags one, the harness reports it as a false positive against that specific case
- [ ] Intentionally degrading a mocked agent (forced to return empty findings) causes the harness to detect an F1 drop beyond a configured threshold and exit non-zero — the actual CI-gating behavior, not just a number in a log
- [ ] The LLM-as-judge grading step's own output is schema-validated and logged

**Complexity:** L · **Depends on:** M10

---

## M15. Polish & Resume-Readiness

**Goal:** Turn a working system into something that reads well in a repo
link on a resume and survives five minutes of an interviewer clicking
around it.

**Files to create/update:**
- `README.md` — problem statement, architecture diagram, quickstart, demo GIF/recording, what's built vs. deferred (link to §11 of `ARCHITECTURE.md`)
- `docs/DEMO.md` — a scripted walkthrough: submit a review, watch SSE progress, hit a HITL gate, approve it, see the published report
- Cleanup pass: remove dead code paths from earlier milestones' scaffolding, confirm `.env.example` matches `Settings` exactly

**Acceptance criteria:**
- [ ] A person with no prior context can clone, install, configure `.env`, and run `argus review --diff fixtures/sample.patch --no-wait` successfully by following only the README
- [ ] The README's architecture diagram matches the actually-implemented graph (not the original v1.0 aspirational one)
- [ ] The full test suite (`pytest`) passes clean end-to-end, including `tests/eval`
- [ ] `docs/DEMO.md`'s walkthrough is verified by actually running it once, start to finish, against the real (not fully mocked) Groq/Gemini free tier

**Complexity:** S · **Depends on:** M11, M12, M13, M14
