"""Supervisor node — plans which agents to run based on the diff."""

from __future__ import annotations

from argus.graph.state import ReviewState
from argus.guardrails.schemas import ReviewPlan

ALL_WORKERS = [
    "static_analysis",
    "security_supervisor",
    "logic_correctness",
    "code_quality",
    "documentation",
]


def supervisor_node(state: ReviewState) -> dict:
    """Decide which workers to dispatch. Currently runs all workers."""
    plan = ReviewPlan(
        review_id=state["review_id"],
        workers=ALL_WORKERS,
        token_budget=50_000,
        notes="Full review",
    )
    return {"plan": plan, "status": "running"}
