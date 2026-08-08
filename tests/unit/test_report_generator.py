"""Tests for M10: Report Generator."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from argus.agents.report_generator import (
    ReportContent,
    ReportGeneratorAgent,
    _deterministic_report,
    _findings_text,
)
from argus.guardrails.schemas import AggregatedFindings, Finding, Report


def make_finding(**kwargs) -> Finding:
    base = dict(
        review_id="rev-1",
        agent_name="test",
        category="logic_bug",
        severity="medium",
        file_path="src/main.py",
        line_start=10,
        description="Off-by-one error",
        confidence=0.9,
    )
    base.update(kwargs)
    return Finding(**base)


def make_agg(findings=None, max_severity="medium") -> AggregatedFindings:
    findings = findings or [make_finding()]
    return AggregatedFindings(
        review_id="rev-1",
        findings=findings,
        max_severity=max_severity,
    )


# ---------------------------------------------------------------------------
# Deterministic report
# ---------------------------------------------------------------------------

def test_deterministic_report_contains_review_id():
    agg = make_agg()
    md = _deterministic_report(agg)
    assert "rev-1" in md


def test_deterministic_report_contains_severity():
    agg = make_agg(max_severity="critical")
    md = _deterministic_report(agg)
    assert "critical" in md.lower()


def test_deterministic_report_lists_findings():
    f = make_finding(file_path="src/auth.py", line_start=42, description="SQL injection")
    agg = make_agg(findings=[f])
    md = _deterministic_report(agg)
    assert "auth.py" in md
    assert "42" in md
    assert "SQL injection" in md


def test_deterministic_report_empty_findings():
    agg = AggregatedFindings(review_id="rev-1", findings=[], max_severity=None)
    md = _deterministic_report(agg)
    assert "None" in md or "none" in md.lower() or "_None._" in md


def test_deterministic_report_separates_severity_buckets():
    findings = [
        make_finding(severity="critical", line_start=1, description="Critical issue"),
        make_finding(severity="low", line_start=2, description="Style nit"),
    ]
    agg = make_agg(findings=findings, max_severity="critical")
    md = _deterministic_report(agg)
    assert "Critical & High" in md
    assert "Medium & Low" in md


def test_deterministic_report_contains_recommendations():
    agg = make_agg()
    md = _deterministic_report(agg)
    assert "Recommendations" in md


def test_findings_text_formats_correctly():
    f = make_finding(severity="high", file_path="src/db.py", line_start=7)
    text = _findings_text([f])
    assert "HIGH" in text
    assert "src/db.py:7" in text


def test_findings_text_empty():
    assert _findings_text([]) == "No findings."


# ---------------------------------------------------------------------------
# ReportGeneratorAgent — no router
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_agent_no_router_returns_report():
    agent = ReportGeneratorAgent(router=None)
    agg = make_agg()
    report = await agent.run(agg, "rev-1")
    assert isinstance(report, Report)
    assert len(report.content_markdown) > 0
    assert report.review_id == "rev-1"
    assert report.published is False


@pytest.mark.asyncio
async def test_agent_no_router_empty_findings():
    agent = ReportGeneratorAgent(router=None)
    agg = AggregatedFindings(review_id="rev-1", findings=[], max_severity=None)
    report = await agent.run(agg, "rev-1")
    assert isinstance(report, Report)
    assert "rev-1" in report.content_markdown


# ---------------------------------------------------------------------------
# ReportGeneratorAgent — with router
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_agent_uses_llm_when_router_available():
    router = MagicMock()
    router.complete_structured = AsyncMock(
        return_value=ReportContent(markdown="# LLM Report\n\nAll looks good.")
    )
    agent = ReportGeneratorAgent(router=router)
    agg = make_agg()
    report = await agent.run(agg, "rev-1")
    assert "LLM Report" in report.content_markdown
    router.complete_structured.assert_called_once()


@pytest.mark.asyncio
async def test_agent_falls_back_on_llm_failure():
    """If LLM raises, deterministic report is used as fallback."""
    router = MagicMock()
    router.complete_structured = AsyncMock(side_effect=Exception("LLM unavailable"))
    agent = ReportGeneratorAgent(router=router)
    agg = make_agg()
    report = await agent.run(agg, "rev-1")
    # Should still produce a valid report
    assert isinstance(report, Report)
    assert len(report.content_markdown) > 0


@pytest.mark.asyncio
async def test_report_has_unique_id():
    agent = ReportGeneratorAgent(router=None)
    agg1 = make_agg()
    agg2 = AggregatedFindings(review_id="rev-2", findings=[], max_severity=None)
    r1 = await agent.run(agg1, "rev-1")
    r2 = await agent.run(agg2, "rev-2")
    assert r1.id != r2.id
    assert r1.review_id == "rev-1"
    assert r2.review_id == "rev-2"
