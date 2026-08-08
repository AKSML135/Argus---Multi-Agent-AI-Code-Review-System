"""Report generator graph node."""

from __future__ import annotations

from datetime import datetime

from argus.agents.report_generator import ReportGeneratorAgent
from argus.graph.state import ReviewState
from argus.guardrails.schemas import AggregatedFindings


async def report_generator_node(state: ReviewState, agent: ReportGeneratorAgent) -> dict:
    """Generate the final markdown report from aggregated findings."""
    agg = state.get("aggregated") or AggregatedFindings(
        review_id=state["review_id"], findings=[]
    )
    report = await agent.run(agg, state["review_id"])
    report.published = True
    report.published_at = datetime.utcnow()
    return {
        "report": report,
        "status": "published",
    }
