"""LangGraph graph state — the single TypedDict that flows through every node.

Using `Annotated[list, operator.add]` for findings lets multiple parallel
branches append without clobbering each other (LangGraph merges annotations
automatically when branches rejoin).
"""

from __future__ import annotations

import operator
from typing import Annotated

from langgraph.graph import MessagesState

from argus.guardrails.schemas import (
    AggregatedFindings,
    Finding,
    HitlDecision,
    Report,
    ReviewPlan,
)


class ReviewState(MessagesState):
    # --- Input ---
    review_id: str
    diff: str

    # --- Supervisor output ---
    plan: ReviewPlan | None

    # --- Parallel worker findings (append-only across branches) ---
    raw_findings: Annotated[list[Finding], operator.add]

    # --- Aggregator / critic output ---
    aggregated: AggregatedFindings | None
    refine_iteration: int

    # --- HITL ---
    hitl_critical_decision: HitlDecision | None
    hitl_final_decision: HitlDecision | None

    # --- Report ---
    report: Report | None
    report_iteration: int

    # --- Control ---
    error: str | None
    status: str  # mirrors Review.status
