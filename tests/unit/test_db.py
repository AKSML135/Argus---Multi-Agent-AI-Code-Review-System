"""Tests for M1: Database round-trip and checkpointer plumbing."""

import tempfile
import os

import pytest
from sqlmodel import select

from argus.persistence.db import get_session, init_db, get_engine
from argus.persistence.models import FindingRow, Review


@pytest.fixture
def tmp_db(tmp_path):
    """Isolated SQLite DB for each test."""
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    return db_path


def test_review_round_trip(tmp_db):
    with get_session(tmp_db) as session:
        review = Review(repo="octocat/hello", pr_number=42, status="pending")
        session.add(review)
        session.commit()
        session.refresh(review)
        rev_id = review.id

    with get_session(tmp_db) as session:
        loaded = session.get(Review, rev_id)
        assert loaded is not None
        assert loaded.repo == "octocat/hello"
        assert loaded.pr_number == 42


def test_finding_round_trip(tmp_db):
    with get_session(tmp_db) as session:
        review = Review(repo="test/repo", status="running")
        session.add(review)
        session.commit()
        session.refresh(review)
        rev_id = review.id

        finding = FindingRow(
            review_id=rev_id,
            category="logic_bug",
            severity="high",
            file_path="src/app.py",
            line_start=42,
            description="Off-by-one error",
        )
        session.add(finding)
        session.commit()
        session.refresh(finding)
        fid = finding.id

    with get_session(tmp_db) as session:
        stmt = select(FindingRow).where(FindingRow.id == fid)
        loaded = session.exec(stmt).first()
        assert loaded is not None
        assert loaded.file_path == "src/app.py"
        assert loaded.severity == "high"


def test_wal_mode(tmp_db):
    """Database should be in WAL journal mode."""
    engine = get_engine(tmp_db)
    with engine.connect() as conn:
        result = conn.exec_driver_sql("PRAGMA journal_mode").fetchone()
        assert result[0] == "wal"


def test_checkpointer_instantiation(tmp_path):
    """SqliteSaver can be created against a throwaway DB — no graph required."""
    from argus.graph.checkpointer import get_checkpointer

    cp_path = str(tmp_path / "checkpoints.db")
    cp = get_checkpointer(cp_path)
    assert cp is not None
