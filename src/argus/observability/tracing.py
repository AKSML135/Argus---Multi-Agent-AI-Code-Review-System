"""OpenTelemetry tracing setup for Argus.

Provides:
  - configure_tracing()          — call once at startup
  - get_tracer()                 — returns the module-level tracer
  - reset_tracing_for_tests()    — swap provider; used in test fixtures only
  - InMemorySpanExporter         — re-exported for test assertions
  - get_span_exporter()          — returns the active exporter (for tests)
"""

from __future__ import annotations

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
)
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

__all__ = [
    "configure_tracing",
    "get_tracer",
    "reset_tracing_for_tests",
    "InMemorySpanExporter",
    "get_span_exporter",
]

_TRACER_NAME = "argus"
_provider: TracerProvider | None = None
_span_exporter: object | None = None


def configure_tracing(
    exporter: object | None = None,
    service_name: str = "argus",
) -> TracerProvider:
    """Set up the global OTel TracerProvider.

    Args:
        exporter: An OTel SpanExporter. Defaults to ``ConsoleSpanExporter``.
                  Pass ``InMemorySpanExporter()`` in tests.
        service_name: ``service.name`` resource attribute.
    """
    global _provider, _span_exporter

    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)

    if exporter is None:
        exporter = ConsoleSpanExporter()

    _span_exporter = exporter

    if isinstance(exporter, InMemorySpanExporter):
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    else:
        provider.add_span_processor(BatchSpanProcessor(exporter))

    _provider = provider
    # Only set global provider once (OTel blocks overrides after first set)
    try:
        trace.set_tracer_provider(provider)
    except Exception:
        pass  # Already set — fine for production; tests use reset_tracing_for_tests()

    return provider


def reset_tracing_for_tests(exporter: InMemorySpanExporter) -> TracerProvider:
    """Replace the module-level provider with a fresh one backed by exporter.

    Called from test fixtures — NOT for production use.
    Works around OTel's restriction on overriding the global provider by
    storing the provider reference directly and having get_tracer() use it.
    """
    global _provider, _span_exporter

    resource = Resource.create({"service.name": "argus-test"})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    _provider = provider
    _span_exporter = exporter
    return provider


def get_tracer(name: str = _TRACER_NAME) -> trace.Tracer:
    """Return the OTel tracer.

    Uses the module-level provider if available (bypasses the global singleton
    so tests can inject their own provider via reset_tracing_for_tests()).
    """
    if _provider is not None:
        return _provider.get_tracer(name)
    return trace.get_tracer(name)


def get_span_exporter() -> object | None:
    """Return the active span exporter (used in tests to inspect recorded spans)."""
    return _span_exporter
