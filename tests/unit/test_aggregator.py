"""Tests for M8: Aggregator deduplication, critic loop, and routing."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from argus.agents.aggregator import AggregatorAgent, CriticResponse, _dedup_findings
from argus.graph.nodes.aggregator import route_after_aggregation, route_after_critical_triage, route_after_final_approval
from argus.guardrails.schemas import AggregatedFindings, Finding, HitlDecision


def make_finding(**kwargs) -> Finding:
    base = dict(
        review_id="rev-1",
        agent_name="test",
        category="logic_bug",
        severity="medium",
        file_path="src/main.py",
        line_start=10,
        description="Test finding",
        confidence=0.9,
    )
    base.update(kwargs)
    return Finding(**base)


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def test_dedup_identical_findings_merged():
    f1 = make_finding(severity="medium", confidence=0.8)
    f2 = make_finding(severity="high", confidence=0.9)
    result = _dedup_findings([f1, f2])
    # Same file/line/category → merged into one
    assert len(result) == 1
    # Keeps highest severity
    assert result[0].severity == "high"
    # Averages confidence
    assert abs(result[0].confidence - 0.85) < 0.01


def test_dedup_different_lines_not_merged():
    f1 = make_finding(line_start=1)
    f2 = make_finding(line_start=2)
    result = _dedup_findings([f1, f2])
    assert len(result) == 2


def test_dedup_different_categories_not_merged():
    f1 = make_finding(category="logic_bug")
    f2 = make_finding(category="security_flaw")
    result = _dedup_findings([f1, f2])
    assert len(result) == 2


def test_dedup_sorted_by_severity():
    findings = [
        make_finding(severity="low", line_start=1),
        make_finding(severity="critical", line_start=2),
        make_finding(severity="medium", line_start=3),
    ]
    result = _dedup_findings(findings)
    severities = [f.severity for f in result]
    assert severities == ["critical", "medium", "low"]


def test_dedup_empty_list():
    assert _dedup_findings([]) == []


# ---------------------------------------------------------------------------
# AggregatorAgent — no LLM
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_aggregator_no_router_deduplicates():
    agent = AggregatorAgent(router=None)
    findings = [
        make_finding(severity="medium", confidence=0.7),
        make_finding(severity="high", confidence=0.9),  # same key → merge
        make_finding(severity="low", line_start=20),
    ]
    agg = await agent.run(findings, "rev-1")
    assert isinstance(agg, AggregatedFindings)
    assert len(agg.findings) == 2  # merged + separate


@pytest.mark.asyncio
async def test_aggregator_computes_max_severity():
    agent = AggregatorAgent(router=None)
    findings = [
        make_finding(severity="low"),
        make_finding(severity="critical", line_start=99),
    ]
    agg = await agent.run(findings, "rev-1")
    assert agg.max_severity == "critical"


@pytest.mark.asyncio
async def test_aggregator_empty_findings():
    agent = AggregatorAgent(router=None)
    agg = await agent.run([], "rev-1")
    assert agg.findings == []
    assert agg.max_severity is None


# ---------------------------------------------------------------------------
# AggregatorAgent — with critic loop
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_critic_loop_removes_false_positives():
    f1 = make_finding(line_start=1, confidence=0.9)
    f2 = make_finding(line_start=2, confidence=0.5)

    router = MagicMock()
    # Critic says f2 is a false positive on first pass, then none
    router.complete_structured = AsyncMock(side_effect=[
        CriticResponse(false_positive_ids=[f2.id], reasoning="Unlikely"),
        CriticResponse(false_positive_ids=[]),
    ])

    agent = AggregatorAgent(router=router, max_iterations=3)
    agg = await agent.run([f1, f2], "rev-1")

    remaining_ids = {f.id for f in agg.findings}
    assert f1.id in remaining_ids
    assert f2.id not in remaining_ids


@pytest.mark.asyncio
async def test_critic_loop_stops_when_no_false_positives():
    findings = [make_finding()]
    router = MagicMock()
    router.complete_structured = AsyncMock(
        return_value=CriticResponse(false_positive_ids=[])
    )
    agent = AggregatorAgent(router=router, max_iterations=3)
    agg = await agent.run(findings, "rev-1")
    # Called once (stopped because no FPs)
    assert router.complete_structured.call_count == 1


@pytest.mark.asyncio
async def test_critic_loop_max_iterations_respected():
    findings = [make_finding(line_start=i) for i in range(1, 5)]
    router = MagicMock()
    # Always returns some FP to keep looping
    router.complete_structured = AsyncMock(
        return_value=CriticResponse(false_positive_ids=[findings[0].id])
    )
    agent = AggregatorAgent(router=router, max_iterations=2)
    await agent.run(findings, "rev-1")
    assert router.complete_structured.call_count <= 2


# ---------------------------------------------------------------------------
# Routing functions
# ---------------------------------------------------------------------------

def make_state(**kwargs):
    base = {
        "review_id": "rev-1",
        "diff": "",
        "plan": None,
        "raw_findings": [],
        "aggregated": None,
        "refine_iteration": 0,
        "hitl_critical_decision": None,
        "hitl_final_decision": None,
        "report": None,
        "report_iteration": 0,
        "error": None,
        "status": "running",
        "messages": [],
    }
    base.update(kwargs)
    return base


def test_route_after_aggregation_critical_goes_to_triage():
    agg = AggregatedFindings(review_id="r", findings=[], max_severity="critical")
    state = make_state(aggregated=agg)
    assert route_after_aggregation(state) == "gate_critical_triage"


def test_route_after_aggregation_non_critical_goes_to_final_approval():
    agg = AggregatedFindings(review_id="r", findings=[], max_severity="high")
    state = make_state(aggregated=agg)
    assert route_after_aggregation(state) == "gate_final_approval"


def test_route_after_aggregation_no_findings_goes_to_final_approval():
    agg = AggregatedFindings(review_id="r", findings=[], max_severity=None)
    state = make_state(aggregated=agg)
    assert route_after_aggregation(state) == "gate_final_approval"


def test_route_after_critical_triage_reject_ends():
    decision = HitlDecision(gate="critical_triage", action="reject")
    state = make_state(hitl_critical_decision=decision)
    assert route_after_critical_triage(state) == "__end__"


def test_route_after_critical_triage_confirm_goes_to_final():
    decision = HitlDecision(gate="critical_triage", action="confirm")
    state = make_state(hitl_critical_decision=decision)
    assert route_after_critical_triage(state) == "gate_final_approval"


def test_route_after_final_approval_approve_goes_to_report():
    decision = HitlDecision(gate="final_approval", action="approve")
    state = make_state(hitl_final_decision=decision)
    assert route_after_final_approval(state) == "report_generator"


def test_route_after_final_approval_reject_ends():
    decision = HitlDecision(gate="final_approval", action="reject")
    state = make_state(hitl_final_decision=decision)
    assert route_after_final_approval(state) == "__end__"


def test_route_after_final_approval_changes_requested_ends():
    decision = HitlDecision(gate="final_approval", action="changes_requested")
    state = make_state(hitl_final_decision=decision)
    assert route_after_final_approval(state) == "__end__"
