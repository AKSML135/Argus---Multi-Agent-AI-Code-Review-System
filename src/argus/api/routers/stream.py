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
    """Stream graph events as SSE for a given review_id."""
    from argus.graph.graph import compile_graph

    graph = compile_graph(router=None)
    config = {"configurable": {"thread_id": review_id}}
    start_ms = int(time.time() * 1000)

    def _build_sse(event_name: str, data: dict) -> str:
        payload = json.dumps(data, default=str)
        return f"event: {event_name}\ndata: {payload}\n\n"

    # Send a "connected" heartbeat immediately
    yield _build_sse(
        "connected",
        {"review_id": review_id, "event": "connected", "agent": None, "elapsed_ms": 0},
    )

    try:
        # Replay existing state first (review may already be progressing)
        graph.get_state(config)

        async for raw_event in graph.astream_events(
            None,  # no new input; replay/stream existing run
            config=config,
            version="v2",
        ):
            if await request.is_disconnected():
                break

            event_type = raw_event.get("event", "")
            node_name = raw_event.get("name", "")
            elapsed_ms = int(time.time() * 1000) - start_ms

            agent = _NODE_TO_AGENT.get(node_name, node_name or None)
            data_payload = raw_event.get("data", {})

            # Only forward meaningful lifecycle events
            if event_type in ("on_chain_start", "on_chain_end", "on_chain_stream"):
                sse_data = {
                    "review_id": review_id,
                    "event": event_type,
                    "agent": agent,
                    "elapsed_ms": elapsed_ms,
                    "data": {
                        k: v
                        for k, v in (data_payload if isinstance(data_payload, dict) else {}).items()
                        if k not in ("input", "output")  # skip full state blobs
                    },
                }
                yield _build_sse(event_type, sse_data)

            elif event_type == "on_custom_event":
                # Custom events emitted by nodes (e.g. agent progress)
                sse_data = {
                    "review_id": review_id,
                    "event": node_name,
                    "agent": agent,
                    "elapsed_ms": elapsed_ms,
                    "data": data_payload if isinstance(data_payload, dict) else {},
                }
                yield _build_sse("agent_event", sse_data)

    except StopAsyncIteration:
        pass
    except Exception as exc:
        log.warning("stream.error", review_id=review_id, error=str(exc))
        yield _build_sse(
            "error",
            {"review_id": review_id, "event": "error", "agent": None, "error": str(exc), "elapsed_ms": 0},
        )
    finally:
        # Send terminal event
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
