# Argus — Demo Walkthrough

This document is a scripted, step-by-step walkthrough designed for an interviewer or anyone evaluating the project. Run it top to bottom to see every major feature in action.

**Time estimate:** ~10 minutes end-to-end (mostly waiting on LLM responses)

---

## Prerequisites

```bash
git clone <repo>
cd argus
pip install -e ".[dev]"
cp .env.example .env
# Edit .env: add ARGUS_GROQ_API_KEY (free tier at console.groq.com)
```

---

## Step 1 — Verify installation

```bash
argus --help
```

Expected output: the Typer help listing `review`, `serve`, and `parse-comment` commands.

```bash
pytest tests/ -q
# Expected: 292 passed, 0 failed
```

---

## Step 2 — Local review (no-wait, no HITL)

The fastest way to see Argus work. Uses the bundled fixture diff which contains a SQL injection, a hardcoded secret, and a debug endpoint.

```bash
argus review --diff fixtures/sample.patch
```

What happens:
1. **Input guardrail** checks for injection patterns and size limits — passes clean.
2. **Supervisor** builds a `ReviewPlan` for all five workers.
3. **Five workers run in parallel** (static analysis, security subgraph, logic, code quality, documentation).
4. **Aggregator** deduplicates findings and runs the LLM critic loop.
5. **Output guardrail** validates citations and redacts any secrets in the output.
6. **Report generator** synthesizes a Markdown report.

A Rich table of findings prints to the terminal, followed by the Markdown report.

To get JSON output (useful for piping to CI tools):

```bash
argus review --diff fixtures/sample.patch --json
```

---

## Step 3 — Gate CI on severity

```bash
# Exit code 1 if any critical finding is present
argus review --diff fixtures/sample.patch --fail-on critical
echo "Exit code: $?"
```

```bash
# Exit code 0 on a clean diff
argus review --diff /dev/null --fail-on critical
echo "Exit code: $?"
```

---

## Step 4 — Start the API server

In a **new terminal**:

```bash
argus serve --port 8000
```

Leave this running. The server logs every event with `review_id`-bound structured output (structlog).

---

## Step 5 — Submit a review via REST

In your original terminal:

```bash
REVIEW_ID=$(python3 -c "import uuid; print(uuid.uuid4())")
DIFF=$(cat fixtures/sample.patch)

curl -s -X POST http://localhost:8000/reviews \
  -H "x-api-key: dev-secret-key" \
  -H "Content-Type: application/json" \
  -d "{\"diff\": $(python3 -c "import json,sys; print(json.dumps(sys.stdin.read()))" < fixtures/sample.patch), \"repo\": \"demo/repo\", \"pr_number\": 1}" \
  | python3 -m json.tool
```

Expected response:

```json
{
    "review_id": "<uuid>",
    "status": "pending",
    "stream_url": "/reviews/<uuid>/stream"
}
```

Save the `review_id`:

```bash
REVIEW_ID="<paste review_id here>"
```

---

## Step 6 — Watch SSE streaming progress

```bash
curl -N "http://localhost:8000/reviews/${REVIEW_ID}/stream" \
  -H "x-api-key: dev-secret-key"
```

You'll see a stream of Server-Sent Events, one per node execution:

```
data: {"review_id": "...", "event": "node_start", "agent": "input_guardrail", "elapsed_ms": 12}
data: {"review_id": "...", "event": "node_start", "agent": "supervisor", "elapsed_ms": 340}
data: {"review_id": "...", "event": "node_start", "agent": "static_analysis_node", "elapsed_ms": 890}
...
data: {"review_id": "...", "event": "awaiting_human", "gate": "gate_critical_triage", "elapsed_ms": 8200}
```

When the stream pauses at `awaiting_human`, move to Step 7.

---

## Step 7 — Inspect the paused review

```bash
curl -s "http://localhost:8000/reviews/${REVIEW_ID}" \
  -H "x-api-key: dev-secret-key" | python3 -m json.tool
```

The response shows `"status": "awaiting_human"` — the graph is paused, durable state saved to `data/checkpoints.db`. You can kill the server now and restart it; the review will still be recoverable.

---

## Step 8 — Approve the HITL gate

```bash
curl -s -X POST "http://localhost:8000/reviews/${REVIEW_ID}/resume" \
  -H "x-api-key: dev-secret-key" \
  -H "Content-Type: application/json" \
  -d '{"decision": {"gate": "gate_critical_triage", "action": "confirm"}}' \
  | python3 -m json.tool
```

The graph resumes. The SSE stream (if still open) will emit more events, eventually reaching the `gate_final_approval` gate.

```bash
# Approve the final gate
curl -s -X POST "http://localhost:8000/reviews/${REVIEW_ID}/resume" \
  -H "x-api-key: dev-secret-key" \
  -H "Content-Type: application/json" \
  -d '{"decision": {"gate": "gate_final_approval", "action": "approve"}}' \
  | python3 -m json.tool
```

---

## Step 9 — Fetch the published report

```bash
curl -s "http://localhost:8000/reviews/${REVIEW_ID}/report" \
  -H "x-api-key: dev-secret-key" | python3 -m json.tool
```

The `content_markdown` field contains the full review report. Status is `"published"`.

---

## Step 10 — Metrics endpoint

```bash
curl -s http://localhost:8000/metrics | grep argus_
```

Expected metrics (prefix `argus_`):

```
argus_llm_calls_total{provider="groq"} ...
argus_llm_retries_total{provider="groq"} ...
argus_hitl_wait_seconds_bucket{gate="gate_critical_triage",...} ...
argus_node_duration_seconds_bucket{node="aggregator_node",...} ...
argus_reviews_total{status="published"} 1
```

---

## Step 11 — Restart survivability (HITL + checkpointing together)

This demonstrates that HITL + checkpointing work *together*, not separately.

```bash
# 1. Submit a new review
NEW_ID=$(curl -s -X POST http://localhost:8000/reviews \
  -H "x-api-key: dev-secret-key" \
  -H "Content-Type: application/json" \
  -d "{\"diff\": $(python3 -c "import json,sys; print(json.dumps(sys.stdin.read()))" < fixtures/sample.patch)}" \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['review_id'])")
echo "Review: $NEW_ID"

# 2. Wait for it to hit the first HITL gate (poll status)
sleep 30  # or watch the SSE stream

# 3. Kill the server (Ctrl-C in the server terminal)
# 4. Restart the server
argus serve --port 8000 &
sleep 2

# 5. Approve — graph resumes exactly where it paused
curl -s -X POST "http://localhost:8000/reviews/${NEW_ID}/resume" \
  -H "x-api-key: dev-secret-key" \
  -H "Content-Type: application/json" \
  -d '{"decision": {"gate": "gate_critical_triage", "action": "confirm"}}' \
  | python3 -m json.tool
```

---

## Step 12 — Authentication

```bash
# Without API key → 401
curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/reviews

# With wrong key → 401
curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/reviews \
  -H "x-api-key: wrong-key"
```

---

## Step 13 — Run the evaluation harness

```bash
# Runs the full offline eval against the 15 fixture PRs
pytest tests/eval/ -v
```

The harness computes precision/recall/F1 per finding category and exits non-zero if F1 drops below threshold — the actual CI-gating behavior.

---

## What to note for an interviewer

| Question | Where to look |
|---|---|
| "How do your agents coordinate?" | `src/argus/graph/graph.py` — `_fan_out_workers()`, `Send()` |
| "How does HITL work?" | `src/argus/graph/nodes/hitl.py` — `interrupt()` |
| "What happens if the LLM returns garbage?" | `src/argus/guardrails/output.py` — `check_output()` |
| "How does a restart not lose HITL state?" | `src/argus/graph/checkpointer.py` + `data/checkpoints.db` |
| "How do you trace one review end-to-end?" | `src/argus/observability/decorators.py` — `@traced_node` |
| "How do you know quality improved?" | `src/argus/eval/offline/harness.py` — precision/recall/F1 |
| "What would you add next?" | `ARCHITECTURE.md §11` — scoped deferral list |
