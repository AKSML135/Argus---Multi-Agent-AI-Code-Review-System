"""Prometheus metrics for Argus.

Exposes the following metrics:
  argus_llm_calls_total{provider}         — counter: LLM calls by provider
  argus_llm_retries_total{provider}       — counter: LLM retries by provider
  argus_hitl_wait_seconds{gate}           — histogram: human decision latency
  argus_node_duration_seconds{node}       — histogram: per-node execution time
  argus_reviews_total{status}             — counter: completed reviews by outcome

Usage:
  from argus.observability.metrics import (
      record_llm_call, record_llm_retry, record_hitl_wait,
      record_node_duration, record_review_outcome,
      get_metrics_output,
  )

Prometheus scrape endpoint is registered on the FastAPI app at /metrics.
"""

from __future__ import annotations

import time
from collections.abc import Generator
from contextlib import contextmanager

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)

# ---------------------------------------------------------------------------
# Registry — isolated so tests don't share state with production
# ---------------------------------------------------------------------------

_registry = CollectorRegistry()

# ---------------------------------------------------------------------------
# Metrics definitions
# ---------------------------------------------------------------------------

_llm_calls = Counter(
    "argus_llm_calls_total",
    "Total LLM calls by provider",
    ["provider"],
    registry=_registry,
)

_llm_retries = Counter(
    "argus_llm_retries_total",
    "Total LLM retries by provider",
    ["provider"],
    registry=_registry,
)

_hitl_wait = Histogram(
    "argus_hitl_wait_seconds",
    "Time (seconds) a review spent waiting for human approval at a HITL gate",
    ["gate"],
    buckets=[1, 5, 15, 30, 60, 120, 300, 600, float("inf")],
    registry=_registry,
)

_node_duration = Histogram(
    "argus_node_duration_seconds",
    "Execution time per graph node",
    ["node"],
    buckets=[0.01, 0.05, 0.1, 0.5, 1, 5, 10, 30, 60, float("inf")],
    registry=_registry,
)

_reviews_total = Counter(
    "argus_reviews_total",
    "Total completed reviews by outcome status",
    ["status"],
    registry=_registry,
)

# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def record_llm_call(provider: str) -> None:
    """Increment the LLM call counter for a given provider."""
    _llm_calls.labels(provider=provider).inc()


def record_llm_retry(provider: str) -> None:
    """Increment the LLM retry counter for a given provider."""
    _llm_retries.labels(provider=provider).inc()


def record_hitl_wait(gate: str, duration_seconds: float) -> None:
    """Record how long a HITL gate waited for a human decision."""
    _hitl_wait.labels(gate=gate).observe(duration_seconds)


def record_node_duration(node: str, duration_seconds: float) -> None:
    """Record node execution duration."""
    _node_duration.labels(node=node).observe(duration_seconds)


def record_review_outcome(status: str) -> None:
    """Increment the reviews counter for a final outcome status."""
    _reviews_total.labels(status=status).inc()


@contextmanager
def timed_node(node_name: str) -> Generator[None, None, None]:
    """Context manager that records ``argus_node_duration_seconds`` automatically."""
    start = time.perf_counter()
    try:
        yield
    finally:
        record_node_duration(node_name, time.perf_counter() - start)


def get_metrics_output() -> bytes:
    """Return Prometheus text-format metrics (for the /metrics endpoint)."""
    return generate_latest(_registry)


def get_content_type() -> str:
    """Return the correct Content-Type header value for /metrics."""
    return CONTENT_TYPE_LATEST


def get_registry() -> CollectorRegistry:
    """Expose the registry (used in tests to reset state)."""
    return _registry
