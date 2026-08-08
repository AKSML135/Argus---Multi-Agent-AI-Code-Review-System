"""Aggregator node and graph routing functions."""

from __future__ import annotations

from argus.agents.aggregator import AggregatorAgent
from argus.graph.state import ReviewState


async def aggregator_node(state: ReviewState, agent: AggregatorAgent) -> dict:
    """Merge raw findings from all workers into a deduplicated AggregatedFindings."""
    raw = state.get("raw_findings") or []
    review_id = state["review_id"]

    aggregated = await agent.run(raw, review_id)
    return {
        "aggregated": aggregated,
        "refine_iteration": aggregated.refine_iterations,
        "status": "aggregated",
    }


# ---------------------------------------------------------------------------
# Routing functions (called by LangGraph conditional_edges)
# ---------------------------------------------------------------------------

def route_after_supervisor(state: ReviewState) -> list[str]:
    """Return the list of worker node names to fan-out to."""
    plan = state.get("plan")
    if plan is None:
        return ["aggregator"]
    return [f"{w}_node" for w in plan.workers]


def route_after_aggregation(state: ReviewState) -> str:
    """Decide next step after aggregation.

    - critical findings → HITL critical triage gate
    - no critical findings → final approval gate
    """
    agg = state.get("aggregated")
    if agg and agg.max_severity == "critical":
        return "gate_critical_triage"
    return "gate_final_approval"


def route_after_critical_triage(state: ReviewState) -> str:
    """After human reviews critical findings, go to report generation."""
    decision = state.get("hitl_critical_decision")
    if decision and decision.action == "reject":
        return "__end__"
    return "gate_final_approval"


def route_after_final_approval(state: ReviewState) -> str:
    """After final human approval, generate report or end."""
    decision = state.get("hitl_final_decision")
    if decision and decision.action in ("reject", "changes_requested"):
        return "__end__"
    return "report_generator"
