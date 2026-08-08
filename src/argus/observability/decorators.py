"""``@traced_node`` decorator for LangGraph node functions.

Applying this decorator to a node function requires zero changes to the
function's own body — it wraps the call in an OTel span and records
node duration metrics automatically.

Usage::

    from argus.observability.decorators import traced_node

    @traced_node
    async def my_node(state: ReviewState) -> dict:
        ...

The decorator:
  1. Creates an OTel span named ``argus.node.<function_name>``
  2. Sets ``review_id`` as a span attribute (pulled from ``state["review_id"]``)
  3. Sets ``node.name`` as a span attribute
  4. Records ``argus_node_duration_seconds`` in Prometheus
  5. Records any exception as a span event before re-raising

Works with both sync and async node functions, though LangGraph nodes are
always async in this codebase.
"""

from __future__ import annotations

import functools
import time
from collections.abc import Callable
from typing import Any

from opentelemetry.trace import StatusCode

from argus.observability.metrics import record_node_duration
from argus.observability.tracing import get_tracer


def traced_node(fn: Callable) -> Callable:
    """Decorator: wrap a LangGraph node in an OTel span + Prometheus timer.

    The decorated function's signature is preserved; the ``review_id``
    attribute on the span is populated from ``state["review_id"]`` if
    the first positional argument is a dict-like state object.

    Applying this decorator requires **zero changes** to the node body.
    """
    if _is_async(fn):
        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            node_name = fn.__name__
            tracer = get_tracer()
            review_id = _extract_review_id(args, kwargs)
            start = time.perf_counter()

            with tracer.start_as_current_span(f"argus.node.{node_name}") as span:
                span.set_attribute("node.name", node_name)
                if review_id:
                    span.set_attribute("review_id", review_id)
                try:
                    result = await fn(*args, **kwargs)
                    span.set_status(StatusCode.OK)
                    return result
                except Exception as exc:
                    span.record_exception(exc)
                    span.set_status(StatusCode.ERROR, str(exc))
                    raise
                finally:
                    record_node_duration(node_name, time.perf_counter() - start)

        return async_wrapper

    else:
        @functools.wraps(fn)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            node_name = fn.__name__
            tracer = get_tracer()
            review_id = _extract_review_id(args, kwargs)
            start = time.perf_counter()

            with tracer.start_as_current_span(f"argus.node.{node_name}") as span:
                span.set_attribute("node.name", node_name)
                if review_id:
                    span.set_attribute("review_id", review_id)
                try:
                    result = fn(*args, **kwargs)
                    span.set_status(StatusCode.OK)
                    return result
                except Exception as exc:
                    span.record_exception(exc)
                    span.set_status(StatusCode.ERROR, str(exc))
                    raise
                finally:
                    record_node_duration(node_name, time.perf_counter() - start)

        return sync_wrapper


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _is_async(fn: Callable) -> bool:
    import asyncio
    return asyncio.iscoroutinefunction(fn)


def _extract_review_id(args: tuple, kwargs: dict) -> str | None:
    """Try to extract review_id from the first positional arg (state dict)."""
    if args:
        first = args[0]
        if isinstance(first, dict):
            return first.get("review_id")
    return kwargs.get("review_id")
