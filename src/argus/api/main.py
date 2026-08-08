"""Argus FastAPI application — M11 service layer.

Entry points:
    uvicorn argus.api.main:app --reload

Routers:
    /reviews          → routers/reviews.py   (POST, GET)
    /reviews/{id}/approve → routers/approvals.py  (POST)
    /reviews/{id}/stream  → routers/stream.py     (GET SSE)

Extra endpoints:
    /health           — liveness probe
    /metrics          — Prometheus scrape endpoint (M12)
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.responses import Response

from argus.api.routers import approvals, reviews, stream
from argus.observability.logging import configure_logging
from argus.observability.metrics import get_content_type, get_metrics_output
from argus.observability.tracing import configure_tracing
from argus.persistence.db import init_db

log = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    configure_tracing()   # M12: boot OTel provider
    init_db()
    log.info("argus.startup", version="0.1.0")
    yield
    log.info("argus.shutdown")


app = FastAPI(
    title="Argus — AI Code Review",
    version="0.1.0",
    description="Multi-agent code review with HITL gates and SSE streaming.",
    lifespan=lifespan,
)

# --- Routers ---
app.include_router(reviews.router)
app.include_router(approvals.router)
app.include_router(stream.router)


@app.get("/health", tags=["meta"])
async def health():
    return {"status": "ok", "version": "0.1.0"}


@app.get("/metrics", tags=["meta"])
async def metrics():
    """Prometheus scrape endpoint. Exposes LLM call counts, retry counts, HITL durations."""
    return Response(content=get_metrics_output(), media_type=get_content_type())
