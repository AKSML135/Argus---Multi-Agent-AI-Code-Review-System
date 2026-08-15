"""Reviews router — POST /reviews, GET /reviews/{id}."""

from __future__ import annotations

import asyncio
import hashlib
import uuid

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import select

from argus.api.deps import verify_api_key
from argus.api.event_bus import get_broadcaster
from argus.config import get_settings
from argus.graph.checkpointer import get_checkpointer  # now async
from argus.graph.graph import compile_graph
from argus.guardrails.input import InputGuardrailError, check_input
from argus.persistence.db import get_session
from argus.persistence.models import FindingRow, ReportRow, Review

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/reviews", tags=["reviews"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class SubmitReviewRequest(BaseModel):
    diff: str = Field(..., min_length=1)
    repo: str = ""
    pr_number: int | None = None


class SubmitReviewResponse(BaseModel):
    review_id: str
    status: str
    stream_url: str


class ReportResponse(BaseModel):
    review_id: str
    content_markdown: str
    published: bool


class ReviewStatusResponse(BaseModel):
    review_id: str
    status: str
    finding_count: int
    findings: list[dict]


# ---------------------------------------------------------------------------
# Background runner
# ---------------------------------------------------------------------------


async def _run_review(review_id: str, diff: str, resume_input=None) -> None:
    """Run the graph to completion (or next HITL interrupt).

    This is the ONLY coroutine allowed to call astream/astream_events on a
    given thread_id. The /stream endpoint must never invoke the graph
    itself — it only subscribes to the events published here via the
    event bus. Two concurrent invocations of the same thread_id race the
    checkpointer and corrupt state, so keep it that way.
    """

    graph = compile_graph(router=None, checkpointer=await get_checkpointer())
    config = {"configurable": {"thread_id": review_id}}
    broadcaster = get_broadcaster(review_id)
    start_ms = int(asyncio.get_event_loop().time() * 1000)

    initial_state = None
    if resume_input is None:
        initial_state = {
            "review_id": review_id,
            "diff": diff,
            "raw_findings": [],
            "plan": None,
            "aggregated": None,
            "refine_iteration": 0,
            "hitl_critical_decision": None,
            "hitl_final_decision": None,
            "report": None,
            "report_iteration": 0,
            "error": None,
            "status": "pending",
            "messages": [],
        }

    try:
        with get_session() as session:
            review = session.get(Review, review_id)
            if review:
                review.status = "running"
                session.add(review)
                session.commit()

        graph_input = initial_state if resume_input is None else resume_input
        async for raw_event in graph.astream_events(
            graph_input, config=config, version="v2"
        ):
            event_type = raw_event.get("event", "")
            if event_type in ("on_chain_start", "on_chain_end", "on_chain_stream"):
                elapsed_ms = int(asyncio.get_event_loop().time() * 1000) - start_ms
                data_payload = raw_event.get("data", {})
                broadcaster.publish(
                    {
                        "review_id": review_id,
                        "event": event_type,
                        "agent": raw_event.get("name"),
                        "elapsed_ms": elapsed_ms,
                        "data": {
                            k: v
                            for k, v in (
                                data_payload if isinstance(data_payload, dict) else {}
                            ).items()
                            if k not in ("input", "output")
                        },
                    }
                )

        await _persist_graph_results(review_id, graph, config)

    except Exception as exc:
        log.error("review.graph_failed", review_id=review_id, error=str(exc))
        with get_session() as session:
            review = session.get(Review, review_id)
            if review:
                review.status = "failed"
                session.add(review)
                session.commit()
    finally:
        broadcaster.finish()


async def _persist_graph_results(review_id: str, graph, config: dict) -> None:
    """Pull final state and upsert findings + report rows."""
    try:
        # Must use aget_state — graph uses AsyncSqliteSaver which only supports async calls
        state = await graph.aget_state(config)
        if state is None:
            return
        values = state.values

        with get_session() as session:
            review = session.get(Review, review_id)
            if not review:
                return

            new_status = values.get("status", "pending")
            # If graph hit a HITL interrupt, next nodes are queued — mark awaiting_human
            if state.next:
                new_status = "awaiting_human"
            review.status = new_status
            session.add(review)

            agg = values.get("aggregated")
            if agg:
                existing_ids = {
                    row.id
                    for row in session.exec(
                        select(FindingRow).where(FindingRow.review_id == review_id)
                    ).all()
                }
                for f in agg.findings:
                    if f.id not in existing_ids:
                        session.add(
                            FindingRow(
                                id=f.id,
                                review_id=review_id,
                                category=f.category,
                                severity=f.severity,
                                file_path=f.file_path,
                                line_start=f.line_start,
                                line_end=f.line_end,
                                description=f.description,
                                confidence=f.confidence,
                                status=f.status,
                            )
                        )

            report = values.get("report")
            if report:
                existing = session.exec(
                    select(ReportRow).where(ReportRow.review_id == review_id)
                ).first()
                if not existing:
                    session.add(
                        ReportRow(
                            id=report.id,
                            review_id=review_id,
                            content_markdown=report.content_markdown,
                            published=report.published,
                            published_at=report.published_at,
                        )
                    )

            session.commit()
    except Exception as exc:
        log.error("review.persist_failed", review_id=review_id, error=str(exc))


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("", response_model=SubmitReviewResponse, status_code=202)
async def submit_review(
    body: SubmitReviewRequest,
    _: None = Depends(verify_api_key),
) -> SubmitReviewResponse:
    """Submit a diff for async AI review. Returns immediately with stream_url."""
    settings = get_settings()
    review_id = str(uuid.uuid4())
    diff_hash = hashlib.sha256(body.diff.encode()).hexdigest()

    # Input guardrail (fast, synchronous)
    try:
        check_input(body.diff, review_id, max_lines=settings.max_diff_lines)
    except InputGuardrailError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Idempotency: same diff_hash + repo → return the existing review only
    # while it's genuinely in flight (pending/running), to collapse
    # accidental duplicate submissions of the same request. Once a review
    # has reached awaiting_human or published it is a *finished* prior run,
    # not a duplicate — re-submitting the same diff (e.g. re-running a demo
    # or CI script) should start a fresh review with a new UUID rather than
    # silently handing back the old one.
    _REUSABLE_STATUSES = {"pending", "running"}

    with get_session() as session:
        if body.repo:
            existing = session.exec(
                select(Review).where(
                    Review.diff_hash == diff_hash,
                    Review.repo == body.repo,
                )
            ).first()
            if existing:
                if existing.status in _REUSABLE_STATUSES:
                    return SubmitReviewResponse(
                        review_id=existing.id,
                        status=existing.status,
                        stream_url=f"/reviews/{existing.id}/stream",
                    )
                # Stale / failed row — delete it so we can start fresh
                session.delete(existing)
                session.commit()

        review = Review(
            id=review_id,
            repo=body.repo,
            pr_number=body.pr_number,
            status="pending",
            diff_hash=diff_hash,
        )
        session.add(review)
        session.commit()

    asyncio.create_task(_run_review(review_id, body.diff))
    log.info("review.submitted", review_id=review_id, repo=body.repo)

    return SubmitReviewResponse(
        review_id=review_id,
        status="pending",
        stream_url=f"/reviews/{review_id}/stream",
    )


@router.get("/{review_id}", response_model=ReviewStatusResponse)
async def get_review(
    review_id: str,
    _: None = Depends(verify_api_key),
) -> ReviewStatusResponse:
    """Get current review status and findings."""
    with get_session() as session:
        review = session.get(Review, review_id)
        if not review:
            raise HTTPException(status_code=404, detail="Review not found")

        findings = session.exec(
            select(FindingRow).where(FindingRow.review_id == review_id)
        ).all()

    return ReviewStatusResponse(
        review_id=review_id,
        status=review.status,
        finding_count=len(findings),
        findings=[
            {
                "id": f.id,
                "severity": f.severity,
                "category": f.category,
                "file_path": f.file_path,
                "line_start": f.line_start,
                "description": f.description,
                "confidence": f.confidence,
                "status": f.status,
            }
            for f in findings
        ],
    )


@router.get("/{review_id}/report", response_model=ReportResponse)
async def get_report(
    review_id: str,
    _: None = Depends(verify_api_key),
) -> ReportResponse:
    """Fetch the published report for a review.

    This route did not exist anywhere in the mounted app before — it was
    only ever defined in the standalone src/argus/api/app.py, which
    argus.api.main:app (the module `argus serve` actually runs) never
    imports or mounts. That file appears to be dead code left over from
    before the M11 router refactor.
    """
    with get_session() as session:
        review = session.get(Review, review_id)
        if not review:
            raise HTTPException(status_code=404, detail="Review not found")

        report = session.exec(
            select(ReportRow).where(ReportRow.review_id == review_id)
        ).first()
        if not report:
            raise HTTPException(
                status_code=404,
                detail=f"No report yet for review '{review_id}' (status: {review.status})",
            )

    return ReportResponse(
        review_id=review_id,
        content_markdown=report.content_markdown,
        published=report.published,
    )