"""Shared Pydantic v2 contracts for Argus.

All cross-boundary data — agent outputs, guardrail events, HITL decisions —
flows through these schemas. Strict typing prevents silent coercions.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------------
# Enums / Literals
# ---------------------------------------------------------------------------

Severity = Literal["critical", "high", "medium", "low", "info"]
FindingStatus = Literal["open", "confirmed", "false_positive", "low_confidence"]
FindingCategory = Literal[
    "security_flaw",
    "leaked_secret",
    "logic_bug",
    "quality",
    "missing_docs",
    "style",
    "type_error",
]
GuardrailStage = Literal["input", "output"]
GuardrailAction = Literal["block", "flag", "redact"]
HitlGate = Literal["critical_triage", "final_approval"]
HitlStatus = Literal["pending", "approved", "rejected", "edited"]
ReviewStatus = Literal[
    "pending",
    "running",
    "awaiting_human",
    "published",
    "rejected",
    "failed",
]


# ---------------------------------------------------------------------------
# Core finding
# ---------------------------------------------------------------------------

class Finding(BaseModel):
    """A single code-review finding from any worker agent."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    review_id: str
    agent_name: str
    category: FindingCategory
    severity: Severity
    file_path: str  # required — validated by output guardrail
    line_start: int = Field(ge=1)
    line_end: int | None = None
    description: str
    confidence: float = Field(ge=0.0, le=1.0, default=1.0)
    status: FindingStatus = "open"
    dedup_group_id: str | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)

    @field_validator("line_end", mode="before")
    @classmethod
    def line_end_gte_start(cls, v: int | None, info: object) -> int | None:
        if v is not None and hasattr(info, "data"):
            start = info.data.get("line_start", 1)
            if v < start:
                raise ValueError(f"line_end ({v}) must be >= line_start ({start})")
        return v


# ---------------------------------------------------------------------------
# Review plan (supervisor output)
# ---------------------------------------------------------------------------

class ReviewPlan(BaseModel):
    """Supervisor's dispatch plan for a given diff."""

    review_id: str
    workers: list[str]
    token_budget: int = Field(ge=0, default=50_000)
    notes: str = ""


# ---------------------------------------------------------------------------
# Aggregated findings (aggregator output)
# ---------------------------------------------------------------------------

class AggregatedFindings(BaseModel):
    """Deduplicated, severity-resolved findings ready for reporting."""

    review_id: str
    findings: list[Finding]
    confidence_score: float = Field(ge=0.0, le=1.0, default=1.0)
    refine_iterations: int = Field(ge=0, default=0)
    max_severity: Severity | None = None

    def compute_max_severity(self) -> Severity | None:
        order = ["critical", "high", "medium", "low", "info"]
        for sev in order:
            if any(f.severity == sev for f in self.findings):
                return sev  # type: ignore[return-value]
        return None


# ---------------------------------------------------------------------------
# HITL decision
# ---------------------------------------------------------------------------

class HitlDecision(BaseModel):
    """Human decision at a HITL gate."""

    gate: HitlGate
    action: Literal["confirm", "dismiss", "approve", "reject", "changes_requested"]
    comment: str | None = None
    decided_at: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# Guardrail event
# ---------------------------------------------------------------------------

class GuardrailEvent(BaseModel):
    """Structured record of a guardrail decision (for audit / persistence)."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    review_id: str
    stage: GuardrailStage
    rule_name: str
    action: GuardrailAction
    details: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

class Report(BaseModel):
    """Final synthesized report from the report generator."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    review_id: str
    content_markdown: str
    published: bool = False
    published_at: datetime | None = None
