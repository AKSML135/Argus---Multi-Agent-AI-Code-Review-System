"""SSE streaming router — GET /reviews/{id}/stream.

Streams LangGraph astream_events() output as Server-Sent Events.
Each event has the shape:
    {
        "review_id": str,
        "event": str,          # node name or lifecycle event
        "agent": str | null,   # agent name if applicable
        "data": dict,          # event payload
        "elapsed_ms": int
    }
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from argus.api.deps import verify_api_key
from argus.api.event_bus import get_broadcaster
from argus.persistence.db import get_session
from argus.persistence.models import Review

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/reviews", tags=["stream"])

# Map LangGraph node names → human-readable agent names
_NODE_TO_AGENT: dict[str, str] = {
    "input_guardrail": "input_guardrail",
    "supervisor": "supervisor",
    "static_analysis_node": "static_analysis",
    "security_supervisor_node": "security",
    "logic_correctness_node": "logic_correctness",
    "code_quality_node": "code_quality",
    "documentation_node": "documentation",
    "aggregator_node": "aggregator",
    "gate_critical_triage": "hitl_critical_triage",
    "gate_final_approval": "hitl_final_approval",
    "report_generator": "report_generator",
}


async def _sse_event_generator(
    review_id: str, request: Request
) -> AsyncIterator[str]:
    """Relay graph progress events as SSE for a given review_id.

    IMPORTANT: this generator must never call graph.astream()/astream_events()
    itself. The graph for a given thread_id is driven by exactly one
    coroutine (`_run_review` in routers/reviews.py, also used for resumes in
    routers/approvals.py). This generator only subscribes to the events that
    coroutine publishes via `argus.api.event_bus`. A second independent
    invocation of the same thread_id races the background run and corrupts
    the AsyncSqliteSaver checkpoint — that used to be exactly what this
    function did, and was the source of the stream endpoint errors.
    """
    start_ms = int(time.time() * 1000)

    def _build_sse(event_name: str, data: dict) -> str:
        payload = json.dumps(data, default=str)
        return f"event: {event_name}\ndata: {payload}\n\n"

    # Send a "connected" heartbeat immediately
    yield _build_sse(
        "connected",
        {"review_id": review_id, "event": "connected", "agent": None, "elapsed_ms": 0},
    )

    broadcaster = get_broadcaster(review_id)
    queue = broadcaster.subscribe()

    try:
        while True:
            if await request.is_disconnected():
                break

            event = await queue.get()
            if event is None:  # sentinel: run finished (interrupt or completion)
                break

            node_name = event.get("agent") or ""
            event["agent"] = _NODE_TO_AGENT.get(node_name, node_name or None)
            yield _build_sse(event["event"], event)

    except Exception as exc:
        log.warning("stream.error", review_id=review_id, error=str(exc))
        yield _build_sse(
            "error",
            {"review_id": review_id, "event": "error", "agent": None, "error": str(exc), "elapsed_ms": 0},
        )
    finally:
        broadcaster.unsubscribe(queue)
        elapsed_ms = int(time.time() * 1000) - start_ms
        yield _build_sse(
            "done",
            {"review_id": review_id, "event": "done", "agent": None, "elapsed_ms": elapsed_ms},
        )


@router.get("/{review_id}/stream")
async def stream_review(
    review_id: str,
    request: Request,
    _: None = Depends(verify_api_key),
) -> StreamingResponse:
    """Stream review progress as Server-Sent Events.

    Events have a consistent shape::

        {
            "review_id": "<uuid>",
            "event": "<on_chain_start|on_chain_end|agent_event|done|error>",
            "agent": "<agent name or null>",
            "elapsed_ms": 1234,
            "data": {}
        }

    Connect with ``EventSource`` or ``curl -N``.
    The stream closes when the graph reaches END or a HITL interrupt.
    """
    with get_session() as session:
        review = session.get(Review, review_id)
        if not review:
            raise HTTPException(status_code=404, detail="Review not found")

    return StreamingResponse(
        _sse_event_generator(review_id, request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )