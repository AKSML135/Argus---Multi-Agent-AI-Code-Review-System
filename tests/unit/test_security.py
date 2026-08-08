"""Tests for M6: Secret Scanner, SAST Agent, and Security Supervisor."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from argus.agents.security.secret_scanner import SecretScannerAgent, _parse_diff_added_lines
from argus.agents.security.sast_agent import SastAgent, SastResponse
from argus.agents.security.supervisor import SecuritySupervisor


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

CLEAN_DIFF = """\
diff --git a/src/main.py b/src/main.py
--- a/src/main.py
+++ b/src/main.py
@@ -1,3 +1,5 @@
+def greet(name: str) -> str:
+    return f"Hello, {name}"
+
 def hello():
     pass
"""

SECRET_DIFF = """\
diff --git a/src/config.py b/src/config.py
--- a/src/config.py
+++ b/src/config.py
@@ -1,3 +1,5 @@
+AWS_ACCESS_KEY = "AKIAIOSFODNN7EXAMPLE"
+OPENAI_KEY = "sk-abcdefghijklmnopqrst1234567890AB"
+DB_PASSWORD = "supersecretpassword123"
 host = "localhost"
"""

SQL_INJECTION_DIFF = """\
diff --git a/src/db.py b/src/db.py
--- a/src/db.py
+++ b/src/db.py
@@ -1,3 +1,5 @@
+def get_user(user_id):
+    query = f"SELECT * FROM users WHERE id = {user_id}"
+    return db.execute(query)
 pass
"""


# ---------------------------------------------------------------------------
# Secret Scanner
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_secret_scanner_detects_aws_key():
    agent = SecretScannerAgent()
    findings = await agent.run(SECRET_DIFF, "rev-1")
    aws_findings = [f for f in findings if "AWS" in f.description]
    assert len(aws_findings) >= 1
    assert aws_findings[0].severity == "critical"
    assert aws_findings[0].category == "leaked_secret"
    assert aws_findings[0].confidence == 1.0


@pytest.mark.asyncio
async def test_secret_scanner_detects_openai_key():
    agent = SecretScannerAgent()
    findings = await agent.run(SECRET_DIFF, "rev-1")
    openai_findings = [f for f in findings if "OpenAI" in f.description]
    assert len(openai_findings) >= 1


@pytest.mark.asyncio
async def test_secret_scanner_clean_diff_no_findings():
    agent = SecretScannerAgent()
    findings = await agent.run(CLEAN_DIFF, "rev-2")
    assert findings == []


@pytest.mark.asyncio
async def test_secret_scanner_empty_diff():
    agent = SecretScannerAgent()
    findings = await agent.run("", "rev-3")
    assert findings == []


@pytest.mark.asyncio
async def test_secret_scanner_deduplicates_same_line():
    """Same file+line+rule should only produce one finding."""
    agent = SecretScannerAgent()
    findings = await agent.run(SECRET_DIFF, "rev-4")
    # Check no exact duplicates
    seen = set()
    for f in findings:
        key = (f.file_path, f.line_start, f.description)
        assert key not in seen, f"Duplicate finding: {key}"
        seen.add(key)


@pytest.mark.asyncio
async def test_secret_scanner_correct_file_path():
    agent = SecretScannerAgent()
    findings = await agent.run(SECRET_DIFF, "rev-5")
    assert all(f.file_path == "src/config.py" for f in findings)


def test_parse_diff_added_lines():
    lines = _parse_diff_added_lines(SECRET_DIFF)
    assert len(lines) > 0
    files = {l[0] for l in lines}
    assert "src/config.py" in files


# ---------------------------------------------------------------------------
# SAST Agent
# ---------------------------------------------------------------------------

def make_mock_router(response_model):
    router = MagicMock()
    router.complete_structured = AsyncMock(return_value=response_model)
    return router


@pytest.mark.asyncio
async def test_sast_no_router_returns_empty():
    agent = SastAgent(router=None)
    findings = await agent.run(SQL_INJECTION_DIFF, "rev-6")
    assert findings == []


@pytest.mark.asyncio
async def test_sast_returns_security_findings():
    from argus.agents.security.sast_agent import RawFinding

    mock_response = SastResponse(findings=[
        RawFinding(
            file_path="src/db.py",
            line_start=2,
            description="SQL Injection via f-string query (OWASP A03:2021)",
            severity="critical",
            confidence=0.95,
        )
    ])
    router = make_mock_router(mock_response)
    agent = SastAgent(router=router)
    findings = await agent.run(SQL_INJECTION_DIFF, "rev-6")

    assert len(findings) == 1
    f = findings[0]
    assert f.agent_name == "sast"
    assert f.category == "security_flaw"
    assert f.severity == "critical"
    assert f.review_id == "rev-6"


@pytest.mark.asyncio
async def test_sast_invalid_severity_defaults_to_high():
    from argus.agents.security.sast_agent import RawFinding

    mock_response = SastResponse(findings=[
        RawFinding(
            file_path="src/db.py",
            line_start=1,
            description="Issue",
            severity="extreme",
        )
    ])
    router = make_mock_router(mock_response)
    agent = SastAgent(router=router)
    findings = await agent.run(SQL_INJECTION_DIFF, "rev-6")
    assert findings[0].severity == "high"


# ---------------------------------------------------------------------------
# Security Supervisor
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_security_supervisor_merges_both_agents():
    from argus.agents.security.sast_agent import RawFinding

    mock_response = SastResponse(findings=[
        RawFinding(
            file_path="src/config.py",
            line_start=1,
            description="Eval usage",
            severity="high",
        )
    ])
    router = make_mock_router(mock_response)
    supervisor = SecuritySupervisor(router=router)

    # SECRET_DIFF has secrets (from scanner) + SAST mock adds one more
    findings = await supervisor.run(SECRET_DIFF, "rev-7")
    agent_names = {f.agent_name for f in findings}
    assert "secret_scanner" in agent_names
    assert "sast" in agent_names


@pytest.mark.asyncio
async def test_security_supervisor_no_router_returns_scanner_only():
    supervisor = SecuritySupervisor(router=None)
    findings = await supervisor.run(SECRET_DIFF, "rev-8")
    assert all(f.agent_name == "secret_scanner" for f in findings)
    assert len(findings) > 0


@pytest.mark.asyncio
async def test_security_supervisor_clean_diff_no_findings():
    supervisor = SecuritySupervisor(router=None)
    findings = await supervisor.run(CLEAN_DIFF, "rev-9")
    assert findings == []


@pytest.mark.asyncio
async def test_security_supervisor_all_findings_have_review_id():
    supervisor = SecuritySupervisor(router=None)
    findings = await supervisor.run(SECRET_DIFF, "rev-10")
    for f in findings:
        assert f.review_id == "rev-10"
