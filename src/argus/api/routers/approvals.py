"""Approvals router — POST /reviews/{id}/approve.

Resumes a graph that is paused at a HITL interrupt gate.
The body maps directly to HitlDecision: gate + action (+ optional comment/edits).
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from argus.api.deps import verify_api_key
from argus.graph.graph import compile_graph
from argus.guardrails.schemas import HitlDecision
from argus.persistence.db import get_session
from argus.persistence.models import HitlCheckpoint, Review

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/reviews", tags=["approvals"])


class ApproveRequest(BaseModel):
    gate: str = "final_approval"
    action: str = "approve"  # "approve" | "reject" | "edited"
    comment: str = ""


class ApproveResponse(BaseModel):
    review_id: str
    gate: str
    action: str
    status: str  # new review status after resume


@router.post("/{review_id}/approve", response_model=ApproveResponse)
async def approve_review(
    review_id: str,
    body: ApproveRequest,
    _: None = Depends(verify_api_key),
) -> ApproveResponse:
    """Resume a HITL-paused review with a human decision.

    - `gate`: which gate is being decided (``critical_triage`` or ``final_approval``)
    - `action`: ``approve`` | ``reject`` | ``edited``

    Returns the new review status after the graph resumes.
    """
    # Validate action value
    valid_actions = {"approve", "reject", "edited"}
    if body.action not in valid_actions:
        raise HTTPException(
            status_code=422,
            detail=f"action must be one of {sorted(valid_actions)}",
        )

    # Confirm review exists and is in awaiting_human state
    with get_session() as session:
        review = session.get(Review, review_id)
        if not review:
            raise HTTPException(status_code=404, detail="Review not found")
        if review.status not in ("awaiting_human", "running", "pending"):
            raise HTTPException(
                status_code=409,
                detail=f"Review is in status '{review.status}', not awaitable",
            )

    decision = HitlDecision(gate=body.gate, action=body.action, comment=body.comment)

    # Persist the HITL checkpoint decision
    with get_session() as session:
        checkpoint = HitlCheckpoint(
            review_id=review_id,
            gate_name=body.gate,
            status=body.action,
            payload_snapshot=decision.model_dump_json(),
        )
        session.add(checkpoint)
        session.commit()

    # Resume the graph
    graph = compile_graph(router=None)
    config = {"configurable": {"thread_id": review_id}}

    try:
        from langgraph.types import Command

        async for _ in graph.astream(
            Command(resume=decision.model_dump()),
            config=config,
        ):
            pass

        # Re-read state to determine new status
        state = graph.get_state(config)
        if state and state.next:
            new_status = "awaiting_human"
        else:
            values = state.values if state else {}
            new_status = values.get("status", "running")

        # Persist findings / report accumulated after resume
        from argus.api.routers.reviews import _persist_graph_results
        _persist_graph_results(review_id, graph, config)

        # Update review status
        with get_session() as session:
            review = session.get(Review, review_id)
            if review:
                review.status = new_status
                session.add(review)
                session.commit()

        log.info(
            "review.approved",
            review_id=review_id,
            gate=body.gate,
            action=body.action,
            new_status=new_status,
        )
        return ApproveResponse(
            review_id=review_id,
            gate=body.gate,
            action=body.action,
            status=new_status,
        )

    except Exception as exc:
        log.error("review.approve_failed", review_id=review_id, error=str(exc))
        raise HTTPException(status_code=500, detail=f"Resume failed: {exc}") from exc
