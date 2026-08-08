"""Argus FastAPI application.

Endpoints:
  POST /reviews          — Submit a diff for review
  GET  /reviews/{id}     — Get review status + findings
  POST /reviews/{id}/resume — Resume a HITL-suspended graph
  GET  /reviews/{id}/report — Fetch the published report
  GET  /health           — Health check
"""

from __future__ import annotations

import hashlib
import uuid
from contextlib import asynccontextmanager

import structlog
from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlmodel import select

from argus.config import get_settings
from argus.graph.graph import compile_graph
from argus.guardrails.schemas import HitlDecision
from argus.observability.logging import configure_logging
from argus.persistence.db import get_session, init_db
from argus.persistence.models import FindingRow, ReportRow, Review

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    init_db()
    log.info("argus.startup", version="0.1.0")
    yield
    log.info("argus.shutdown")


app = FastAPI(
    title="Argus — AI Code Review",
    version="0.1.0",
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------

def verify_api_key(x_api_key: str = Header(default="")) -> None:
    settings = get_settings()
    if x_api_key != settings.api_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class SubmitReviewRequest(BaseModel):
    diff: str = Field(..., min_length=1)
    repo: str = ""
    pr_number: int | None = None


class SubmitReviewResponse(BaseModel):
    review_id: str
    status: str


class ReviewStatusResponse(BaseModel):
    review_id: str
    status: str
    finding_count: int
    findings: list[dict]


class ResumeRequest(BaseModel):
    decision: dict  # HitlDecision fields


class ReportResponse(BaseModel):
    review_id: str
    content_markdown: str
    published: bool


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "version": "0.1.0"}


@app.post("/reviews", response_model=SubmitReviewResponse, status_code=202)
async def submit_review(
    body: SubmitReviewRequest,
    _: None = Depends(verify_api_key),
) -> SubmitReviewResponse:
    """Submit a diff for async review."""
    from argus.guardrails.input import InputGuardrailError, check_input
    settings = get_settings()

    review_id = str(uuid.uuid4())
    diff_hash = hashlib.sha256(body.diff.encode()).hexdigest()

    # Input guardrail (synchronous — fast)
    try:
        check_input(body.diff, review_id, max_lines=settings.max_diff_lines)
    except InputGuardrailError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Persist review row
    with get_session() as session:
        review = Review(
            id=review_id,
            repo=body.repo,
            pr_number=body.pr_number,
            status="pending",
            diff_hash=diff_hash,
        )
        session.add(review)
        session.commit()

    # Run graph in background (fire-and-forget for now; production would use a task queue)
    import asyncio
    asyncio.create_task(_run_review(review_id, body.diff))

    log.info("review.submitted", review_id=review_id, repo=body.repo)
    return SubmitReviewResponse(review_id=review_id, status="pending")


async def _run_review(review_id: str, diff: str) -> None:
    """Run the review graph to the first HITL interrupt."""
    graph = compile_graph(router=None)  # no LLM in default mode
    config = {"configurable": {"thread_id": review_id}}
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
        async for _event in graph.astream(initial_state, config=config):
            pass  # graph runs to first interrupt or end
        _persist_results(review_id, graph, config)
    except Exception as exc:
        log.error("review.failed", review_id=review_id, error=str(exc))
        with get_session() as session:
            review = session.get(Review, review_id)
            if review:
                review.status = "failed"
                session.add(review)
                session.commit()


def _persist_results(review_id: str, graph, config: dict) -> None:
    """Pull final state from graph and persist findings + report."""
    try:
        state = graph.get_state(config)
        if state is None:
            return
        values = state.values

        with get_session() as session:
            review = session.get(Review, review_id)
            if not review:
                return

            review.status = values.get("status", "pending")
            session.add(review)

            # Persist findings
            agg = values.get("aggregated")
            if agg:
                for f in agg.findings:
                    row = FindingRow(
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
                    session.add(row)

            # Persist report
            report = values.get("report")
            if report:
                rrow = ReportRow(
                    id=report.id,
                    review_id=review_id,
                    content_markdown=report.content_markdown,
                    published=report.published,
                    published_at=report.published_at,
                )
                session.add(rrow)

            session.commit()
    except Exception as exc:
        log.error("persist.failed", review_id=review_id, error=str(exc))


@app.get("/reviews/{review_id}", response_model=ReviewStatusResponse)
async def get_review(
    review_id: str,
    _: None = Depends(verify_api_key),
) -> ReviewStatusResponse:
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


@app.post("/reviews/{review_id}/resume", status_code=200)
async def resume_review(
    review_id: str,
    body: ResumeRequest,
    _: None = Depends(verify_api_key),
) -> dict:
    """Resume a HITL-suspended graph with a human decision."""
    try:
        HitlDecision(**body.decision)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Invalid decision: {exc}") from exc

    graph = compile_graph(router=None)
    config = {"configurable": {"thread_id": review_id}}

    try:
        async for _ in graph.astream(
            {"messages": []},
            config=config,
            resuming=True,
        ):
            pass
        _persist_results(review_id, graph, config)
    except Exception as exc:
        log.error("resume.failed", review_id=review_id, error=str(exc))
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {"review_id": review_id, "resumed": True}


@app.get("/reviews/{review_id}/report", response_model=ReportResponse)
async def get_report(
    review_id: str,
    _: None = Depends(verify_api_key),
) -> ReportResponse:
    with get_session() as session:
        stmt = select(ReportRow).where(ReportRow.review_id == review_id)
        report = session.exec(stmt).first()
        if not report:
            raise HTTPException(status_code=404, detail="Report not found")

    return ReportResponse(
        review_id=review_id,
        content_markdown=report.content_markdown,
        published=report.published,
    )
