"""Tests for M9: HITL gate nodes — payload building and decision parsing.

We cannot run the full interrupt() flow in unit tests (it requires a live
LangGraph checkpointed graph), so we test:
  - Payload builder helpers
  - HitlDecision schema validation
  - Routing logic driven by decisions
"""

from __future__ import annotations

import pytest

from argus.graph.nodes.hitl import _build_final_payload, _build_triage_payload
from argus.graph.nodes.aggregator import route_after_critical_triage, route_after_final_approval
from argus.guardrails.schemas import AggregatedFindings, Finding, HitlDecision


def make_finding(**kwargs) -> Finding:
    base = dict(
        review_id="rev-1",
        agent_name="test",
        category="security_flaw",
        severity="critical",
        file_path="src/auth.py",
        line_start=42,
        description="SQL Injection",
        confidence=0.95,
    )
    base.update(kwargs)
    return Finding(**base)


def make_state(aggregated=None, hitl_critical=None, hitl_final=None):
    return {
        "review_id": "rev-1",
        "diff": "",
        "plan": None,
        "raw_findings": [],
        "aggregated": aggregated,
        "refine_iteration": 0,
        "hitl_critical_decision": hitl_critical,
        "hitl_final_decision": hitl_final,
        "report": None,
        "report_iteration": 0,
        "error": None,
        "status": "running",
        "messages": [],
    }


# ---------------------------------------------------------------------------
# Payload builders
# ---------------------------------------------------------------------------

def test_triage_payload_filters_critical_only():
    findings = [
        make_finding(severity="critical", line_start=1),
        make_finding(severity="high", line_start=2),
        make_finding(severity="medium", line_start=3),
    ]
    agg = AggregatedFindings(review_id="rev-1", findings=findings, max_severity="critical")
    state = make_state(aggregated=agg)
    payload = _build_triage_payload(state)

    assert payload["max_severity"] == "critical"
    assert payload["finding_count"] == 3
    # Only critical findings in the triage payload
    assert all(f["severity"] == "critical" for f in payload["findings"])
    assert len(payload["findings"]) == 1


def test_triage_payload_no_aggregated():
    state = make_state(aggregated=None)
    payload = _build_triage_payload(state)
    assert payload["findings"] == []
    assert payload["max_severity"] is None


def test_final_payload_includes_all_findings():
    findings = [
        make_finding(severity="critical", line_start=1),
        make_finding(severity="medium", line_start=2),
        make_finding(severity="low", line_start=3),
    ]
    agg = AggregatedFindings(review_id="rev-1", findings=findings, max_severity="critical")
    state = make_state(aggregated=agg)
    payload = _build_final_payload(state)

    assert payload["finding_count"] == 3
    assert len(payload["findings"]) == 3


def test_final_payload_contains_required_fields():
    findings = [make_finding()]
    agg = AggregatedFindings(review_id="rev-1", findings=findings, max_severity="critical")
    state = make_state(aggregated=agg)
    payload = _build_final_payload(state)

    f = payload["findings"][0]
    assert "id" in f
    assert "severity" in f
    assert "category" in f
    assert "file_path" in f
    assert "line_start" in f
    assert "description" in f


# ---------------------------------------------------------------------------
# HitlDecision schema
# ---------------------------------------------------------------------------

def test_hitl_decision_confirm_valid():
    d = HitlDecision(gate="critical_triage", action="confirm")
    assert d.decided_at is not None


def test_hitl_decision_approve_valid():
    d = HitlDecision(gate="final_approval", action="approve")
    assert d.action == "approve"


def test_hitl_decision_all_valid_actions():
    for action in ["confirm", "dismiss", "approve", "reject", "changes_requested"]:
        d = HitlDecision(gate="final_approval", action=action)
        assert d.action == action


def test_hitl_decision_invalid_action_raises():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        HitlDecision(gate="final_approval", action="maybe")


def test_hitl_decision_invalid_gate_raises():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        HitlDecision(gate="unknown_gate", action="approve")


def test_hitl_decision_with_comment():
    d = HitlDecision(
        gate="final_approval",
        action="changes_requested",
        comment="Please fix the SQL injection first",
    )
    assert "SQL" in d.comment


# ---------------------------------------------------------------------------
# Routing based on HITL decisions
# ---------------------------------------------------------------------------

def test_critical_triage_confirm_routes_to_final_approval():
    decision = HitlDecision(gate="critical_triage", action="confirm")
    state = make_state(hitl_critical=decision)
    assert route_after_critical_triage(state) == "gate_final_approval"


def test_critical_triage_dismiss_routes_to_final_approval():
    decision = HitlDecision(gate="critical_triage", action="dismiss")
    state = make_state(hitl_critical=decision)
    assert route_after_critical_triage(state) == "gate_final_approval"


def test_critical_triage_reject_routes_to_end():
    decision = HitlDecision(gate="critical_triage", action="reject")
    state = make_state(hitl_critical=decision)
    assert route_after_critical_triage(state) == "__end__"


def test_final_approval_approve_routes_to_report():
    decision = HitlDecision(gate="final_approval", action="approve")
    state = make_state(hitl_final=decision)
    assert route_after_final_approval(state) == "report_generator"


def test_final_approval_reject_routes_to_end():
    decision = HitlDecision(gate="final_approval", action="reject")
    state = make_state(hitl_final=decision)
    assert route_after_final_approval(state) == "__end__"


def test_final_approval_changes_requested_routes_to_end():
    decision = HitlDecision(gate="final_approval", action="changes_requested")
    state = make_state(hitl_final=decision)
    assert route_after_final_approval(state) == "__end__"


def test_no_hitl_decision_defaults_to_report():
    """If no decision is set (graph resumed without input), default to report."""
    state = make_state(hitl_final=None)
    # route_after_final_approval should handle None gracefully
    result = route_after_final_approval(state)
    assert result == "report_generator"
