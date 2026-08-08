"""Tests for M1: Pydantic schemas / contracts."""

import pytest
from pydantic import ValidationError

from argus.guardrails.schemas import (
    AggregatedFindings,
    Finding,
    GuardrailEvent,
    HitlDecision,
    Report,
    ReviewPlan,
)


def make_finding(**kwargs) -> dict:
    base = dict(
        review_id="rev-1",
        agent_name="test_agent",
        category="logic_bug",
        severity="medium",
        file_path="src/main.py",
        line_start=10,
        description="Test finding",
        confidence=0.9,
    )
    base.update(kwargs)
    return base


# --- Finding ---

def test_finding_valid():
    f = Finding(**make_finding())
    assert f.status == "open"
    assert f.id  # auto-generated UUID


def test_finding_missing_file_path_raises():
    payload = make_finding()
    del payload["file_path"]
    with pytest.raises(ValidationError):
        Finding(**payload)


def test_finding_invalid_severity_raises():
    with pytest.raises(ValidationError):
        Finding(**make_finding(severity="extreme"))


def test_finding_invalid_category_raises():
    with pytest.raises(ValidationError):
        Finding(**make_finding(category="bad_category"))


def test_finding_confidence_out_of_range():
    with pytest.raises(ValidationError):
        Finding(**make_finding(confidence=1.5))
    with pytest.raises(ValidationError):
        Finding(**make_finding(confidence=-0.1))


def test_finding_line_end_lt_start_raises():
    with pytest.raises(ValidationError):
        Finding(**make_finding(line_start=10, line_end=5))


def test_finding_line_end_gte_start_ok():
    f = Finding(**make_finding(line_start=5, line_end=10))
    assert f.line_end == 10


# --- ReviewPlan ---

def test_review_plan_valid():
    rp = ReviewPlan(review_id="rev-1", workers=["static_analysis", "security"])
    assert rp.token_budget == 50_000


# --- AggregatedFindings ---

def test_aggregated_findings_compute_max_severity():
    findings = [
        Finding(**make_finding(severity="medium")),
        Finding(**make_finding(severity="high")),
        Finding(**make_finding(severity="low")),
    ]
    agg = AggregatedFindings(review_id="rev-1", findings=findings)
    assert agg.compute_max_severity() == "high"


def test_aggregated_findings_empty():
    agg = AggregatedFindings(review_id="rev-1", findings=[])
    assert agg.compute_max_severity() is None


# --- GuardrailEvent ---

def test_guardrail_event_valid():
    evt = GuardrailEvent(
        review_id="rev-1",
        stage="input",
        rule_name="injection_detection",
        action="block",
    )
    assert evt.id


def test_guardrail_event_invalid_stage():
    with pytest.raises(ValidationError):
        GuardrailEvent(
            review_id="rev-1",
            stage="middleware",  # invalid
            rule_name="x",
            action="block",
        )


# --- HitlDecision ---

def test_hitl_decision_valid():
    d = HitlDecision(gate="final_approval", action="approve")
    assert d.decided_at is not None


def test_hitl_decision_invalid_gate():
    with pytest.raises(ValidationError):
        HitlDecision(gate="unknown_gate", action="approve")


# --- Report ---

def test_report_valid():
    r = Report(review_id="rev-1", content_markdown="# Report")
    assert r.published is False
