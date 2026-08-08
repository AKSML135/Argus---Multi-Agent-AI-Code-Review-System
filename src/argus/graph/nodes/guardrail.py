"""Input guardrail node — first node in the graph."""

from __future__ import annotations

from argus.graph.state import ReviewState
from argus.guardrails.input import InputGuardrailError, check_input


def input_guardrail_node(state: ReviewState) -> dict:
    """Validate the diff before any agent sees it."""
    from argus.config import get_settings
    settings = get_settings()
    try:
        check_input(state["diff"], state["review_id"], max_lines=settings.max_diff_lines)
        return {"status": "running"}
    except InputGuardrailError as exc:
        return {"status": "failed", "error": str(exc)}
