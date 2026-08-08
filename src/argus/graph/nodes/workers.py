"""Worker nodes — one async function per agent, called in parallel via Send()."""

from __future__ import annotations

from argus.graph.state import ReviewState
from argus.guardrails.output import check_output
from argus.guardrails.schemas import Finding


def _make_worker_node(agent):
    """Factory: create a LangGraph node function for a given agent instance."""

    async def worker_node(state: ReviewState) -> dict:
        diff = state["diff"]
        review_id = state["review_id"]
        try:
            findings: list[Finding] = await agent.run(diff, review_id)
            # Apply output guardrails
            result = check_output(findings, diff, review_id)
            return {"raw_findings": result.findings}
        except Exception as exc:
            # Worker failures are non-fatal — return empty list, log error
            return {"raw_findings": [], "error": str(exc)}

    worker_node.__name__ = f"{agent.name}_node"
    return worker_node
