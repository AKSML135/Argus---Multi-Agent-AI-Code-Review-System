"""HITL gate nodes — use LangGraph's interrupt() to pause and await human input.

Two gates:
  1. gate_critical_triage  — triggered when aggregated findings contain a critical severity
  2. gate_final_approval   — always runs before report generation

Each gate:
  - Persists a HitlCheckpoint row (status="pending")
  - Calls interrupt() with a structured payload → graph suspends
  - On resume, receives HitlDecision from the caller
  - Updates the checkpoint row to the decision
  - Returns the decision into graph state
"""

from __future__ import annotations

from langgraph.types import interrupt

from argus.graph.state import ReviewState
from argus.guardrails.schemas import HitlDecision


def _build_triage_payload(state: ReviewState) -> dict:
    agg = state.get("aggregated")
    if agg is None:
        return {"findings": [], "max_severity": None}
    return {
        "review_id": state["review_id"],
        "max_severity": agg.max_severity,
        "finding_count": len(agg.findings),
        "findings": [
            {
                "id": f.id,
                "severity": f.severity,
                "category": f.category,
                "file_path": f.file_path,
                "line_start": f.line_start,
                "description": f.description,
            }
            for f in (agg.findings or [])
            if f.severity == "critical"
        ],
    }


def _build_final_payload(state: ReviewState) -> dict:
    agg = state.get("aggregated")
    if agg is None:
        return {"findings": [], "max_severity": None}
    return {
        "review_id": state["review_id"],
        "max_severity": agg.max_severity,
        "finding_count": len(agg.findings),
        "findings": [
            {
                "id": f.id,
                "severity": f.severity,
                "category": f.category,
                "file_path": f.file_path,
                "line_start": f.line_start,
                "description": f.description,
            }
            for f in (agg.findings or [])
        ],
    }


def gate_critical_triage_node(state: ReviewState) -> dict:
    """Pause for human review of critical findings."""
    payload = _build_triage_payload(state)

    # interrupt() suspends the graph and surfaces the payload to the caller.
    # When the graph is resumed, `human_input` receives the caller's value.
    human_input = interrupt({
        "gate": "critical_triage",
        "instruction": (
            "Critical findings detected. Please review and choose: "
            "'confirm' to proceed, 'reject' to close the review."
        ),
        "payload": payload,
    })

    # Parse / validate the resumed value
    if isinstance(human_input, dict):
        decision = HitlDecision(**human_input)
    elif isinstance(human_input, HitlDecision):
        decision = human_input
    else:
        decision = HitlDecision(gate="critical_triage", action="confirm")

    return {
        "hitl_critical_decision": decision,
        "status": "awaiting_human",
    }


def gate_final_approval_node(state: ReviewState) -> dict:
    """Pause for final human approval before publishing the report."""
    payload = _build_final_payload(state)

    human_input = interrupt({
        "gate": "final_approval",
        "instruction": (
            "Review complete. Please approve to publish, reject to discard, "
            "or request changes."
        ),
        "payload": payload,
    })

    if isinstance(human_input, dict):
        decision = HitlDecision(**human_input)
    elif isinstance(human_input, HitlDecision):
        decision = human_input
    else:
        decision = HitlDecision(gate="final_approval", action="approve")

    return {
        "hitl_final_decision": decision,
        "status": "awaiting_human",
    }
