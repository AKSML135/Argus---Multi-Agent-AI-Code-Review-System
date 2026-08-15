"""Approvals router — POST /reviews/{id}/approve.

Resumes a graph that is paused at a HITL interrupt gate.
The body maps directly to HitlDecision: gate + action (+ optional comment/edits).
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from argus.api.deps import verify_api_key
from argus.graph.checkpointer import get_checkpointer
from argus.graph.graph import compile_graph
from argus.guardrails.schemas import HitlDecision
from argus.persistence.db import get_session
from argus.persistence.models import HitlCheckpoint, Review

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/reviews", tags=["approvals"])


class HitlDecisionBody(BaseModel):
    gate: str = "final_approval"
    action: str = "approve"  # "approve" | "reject" | "edited"
    comment: str = ""


class ApproveRequest(BaseModel):
    decision: HitlDecisionBody


class ApproveResponse(BaseModel):
    review_id: str
    gate: str
    action: str
    status: str  # new review status after resume


@router.post("/{review_id}/resume", response_model=ApproveResponse)
async def approve_review(
    review_id: str,
    body: ApproveRequest,
    _: None = Depends(verify_api_key),
) -> ApproveResponse:
    """Resume a HITL-paused review with a human decision.

    - `decision.gate`: which gate is being decided (``critical_triage`` or ``final_approval``)
    - `decision.action`: ``approve`` | ``reject`` | ``edited`` | ``confirm``

    Returns the new review status after the graph resumes.
    """
    gate = body.decision.gate
    action = body.decision.action
    comment = body.decision.comment

    # HitlDecision.gate is a Literal["critical_triage", "final_approval"],
    # but the graph node names (and DEMO.md's curl bodies) use the prefixed
    # form "gate_critical_triage" / "gate_final_approval". Normalize before
    # constructing the internal schema, and keep the prefixed form in our
    # own request/response/checkpoint records since that's what callers
    # actually send and expect back.
    internal_gate = gate[len("gate_") :] if gate.startswith("gate_") else gate

    # Validate action value
    # NOTE: "confirm" is included because docs/DEMO.md's critical_triage step
    # uses it. I could not verify against src/argus/graph/nodes/hitl.py what
    # literal that node actually expects — the uploaded archive was
    # corrupted/truncated for that file specifically, so double check it
    # accepts "confirm" (or change DEMO.md to send "approve" if not).
    valid_actions = {"approve", "reject", "edited", "confirm"}
    if action not in valid_actions:
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

    # Verify the graph is actually paused at the gate the caller thinks
    # they're deciding. aget_state() is a read-only checkpoint lookup — it
    # does NOT drive/resume the graph, so it's safe to call here without
    # racing the single-writer rule that governs astream/astream_events.
    # Without this check, sending "gate_critical_triage" while the graph
    # was really paused at "gate_final_approval" would silently resume the
    # final-approval gate anyway — approving something the caller never
    # actually looked at.
    normalized_requested = gate if gate.startswith("gate_") else f"gate_{gate}"
    graph = compile_graph(router=None, checkpointer=await get_checkpointer())
    config = {"configurable": {"thread_id": review_id}}
    state = await graph.aget_state(config)
    paused_at = set(state.next) if state and state.next else set()

    if normalized_requested not in paused_at:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Review is paused at {sorted(paused_at) or 'no gate (not awaiting human)'}, "
                f"not '{normalized_requested}'. Resend with the correct gate."
            ),
        )

    decision = HitlDecision(gate=internal_gate, action=action, comment=comment)

    # Persist the HITL checkpoint decision
    with get_session() as session:
        checkpoint = HitlCheckpoint(
            review_id=review_id,
            gate_name=gate,
            status=action,
            payload_snapshot=decision.model_dump_json(),
        )
        session.add(checkpoint)
        session.commit()

    try:
        from langgraph.types import Command

        # Resume through _run_review — it is the single coroutine allowed to
        # drive this thread_id. Driving the graph independently here (as a
        # second astream call) would race any open /stream subscriber on the
        # same thread_id and corrupt the checkpoint, the same failure mode
        # the SSE endpoint used to have.
        from argus.api.routers.reviews import _run_review

        await _run_review(
            review_id, diff="", resume_input=Command(resume=decision.model_dump())
        )

        with get_session() as session:
            review = session.get(Review, review_id)
            new_status = review.status if review else "running"

        log.info(
            "review.approved",
            review_id=review_id,
            gate=gate,
            action=action,
            new_status=new_status,
        )
        return ApproveResponse(
            review_id=review_id,
            gate=gate,
            action=action,
            status=new_status,
        )

    except Exception as exc:
        log.error("review.approve_failed", review_id=review_id, error=str(exc))
        raise HTTPException(status_code=500, detail=f"Resume failed: {exc}") from exc