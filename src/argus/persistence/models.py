"""SQLModel table definitions for argus.db (domain data).

checkpoints.db is owned exclusively by LangGraph's SqliteSaver — never
written to directly by application code.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlmodel import Field, SQLModel


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.utcnow()


class Review(SQLModel, table=True):
    __tablename__ = "reviews"

    id: str = Field(default_factory=_uuid, primary_key=True)
    repo: str = ""
    pr_number: int | None = None
    status: str = "pending"
    token_budget: int = 50_000
    diff_hash: str | None = None  # SHA-256 of diff for idempotency
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class AgentRun(SQLModel, table=True):
    __tablename__ = "agent_runs"

    id: str = Field(default_factory=_uuid, primary_key=True)
    review_id: str = Field(foreign_key="reviews.id", index=True)
    agent_name: str
    parent_agent_id: str | None = Field(default=None, foreign_key="agent_runs.id")
    status: str = "pending"
    provider: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    retries: int = 0
    started_at: datetime = Field(default_factory=_now)
    finished_at: datetime | None = None


class FindingRow(SQLModel, table=True):
    __tablename__ = "findings"

    id: str = Field(default_factory=_uuid, primary_key=True)
    review_id: str = Field(foreign_key="reviews.id", index=True)
    agent_run_id: str | None = Field(default=None, foreign_key="agent_runs.id")
    category: str
    severity: str
    file_path: str
    line_start: int
    line_end: int | None = None
    description: str
    confidence: float = 1.0
    status: str = "open"
    dedup_group_id: str | None = None


class GuardrailEvent(SQLModel, table=True):
    __tablename__ = "guardrail_events"

    id: str = Field(default_factory=_uuid, primary_key=True)
    review_id: str = Field(foreign_key="reviews.id", index=True)
    stage: str  # "input" | "output"
    rule_name: str
    action: str  # "block" | "flag" | "redact"
    details: str = ""
    created_at: datetime = Field(default_factory=_now)


class HitlCheckpoint(SQLModel, table=True):
    __tablename__ = "hitl_checkpoints"

    id: str = Field(default_factory=_uuid, primary_key=True)
    review_id: str = Field(foreign_key="reviews.id", index=True)
    gate_name: str  # "critical_triage" | "final_approval"
    status: str = "pending"  # "pending" | "approved" | "rejected" | "edited"
    payload_snapshot: str = ""  # JSON blob of what was shown to the human
    decided_at: datetime | None = None


class ReportRow(SQLModel, table=True):
    __tablename__ = "reports"

    id: str = Field(default_factory=_uuid, primary_key=True)
    review_id: str = Field(foreign_key="reviews.id", index=True, unique=True)
    content_markdown: str = ""
    published: bool = False
    published_at: datetime | None = None
