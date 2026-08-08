"""Tests for M5: LLM-backed worker agents — mocked router."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from argus.agents.code_quality.agent import CodeQualityAgent, check_complexity
from argus.agents.documentation.agent import DocumentationAgent
from argus.agents.logic.agent import LogicAgent
from argus.guardrails.schemas import Finding


SAMPLE_DIFF = """\
diff --git a/src/main.py b/src/main.py
index abc..def 100644
--- a/src/main.py
+++ b/src/main.py
@@ -1,5 +1,10 @@
+def process(items):
+    result = []
+    for i in items:
+        if i > 0:
+            result.append(i * 2)
+    return result
 def hello():
     pass
"""

COMPLEX_DIFF = """\
diff --git a/src/complex.py b/src/complex.py
index 000..111 100644
--- a/src/complex.py
+++ b/src/complex.py
@@ -1,2 +1,22 @@
+def very_complex(x, y, z):
+    if x > 0:
+        if y > 0:
+            if z > 0:
+                for i in range(x):
+                    if i % 2 == 0:
+                        while y > 0:
+                            if z > i:
+                                y -= 1
+                            elif z == i:
+                                y -= 2
+                            else:
+                                break
+                    elif i % 3 == 0:
+                        for j in range(y):
+                            if j > 0 and z > 0:
+                                z -= 1
+    elif x < 0:
+        return -1
+    else:
+        return 0
+    return x + y + z
 pass
"""


def make_mock_router(response_model):
    router = MagicMock()
    router.complete_structured = AsyncMock(return_value=response_model)
    return router


# ---------------------------------------------------------------------------
# Logic Agent
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_logic_agent_no_router_returns_empty():
    agent = LogicAgent(router=None)
    findings = await agent.run(SAMPLE_DIFF, "rev-1")
    assert findings == []


@pytest.mark.asyncio
async def test_logic_agent_returns_findings_from_router():
    from argus.agents.logic.agent import LogicAnalysisResponse, RawFinding

    mock_response = LogicAnalysisResponse(findings=[
        RawFinding(
            file_path="src/main.py",
            line_start=2,
            description="Missing null check before iteration",
            severity="high",
            confidence=0.9,
        )
    ])
    router = make_mock_router(mock_response)
    agent = LogicAgent(router=router)
    findings = await agent.run(SAMPLE_DIFF, "rev-1")

    assert len(findings) == 1
    f = findings[0]
    assert f.agent_name == "logic_correctness"
    assert f.category == "logic_bug"
    assert f.severity == "high"
    assert f.review_id == "rev-1"
    assert f.confidence == 0.9


@pytest.mark.asyncio
async def test_logic_agent_invalid_severity_defaults_to_medium():
    from argus.agents.logic.agent import LogicAnalysisResponse, RawFinding

    mock_response = LogicAnalysisResponse(findings=[
        RawFinding(
            file_path="src/main.py",
            line_start=1,
            description="Some issue",
            severity="extreme",  # invalid
        )
    ])
    router = make_mock_router(mock_response)
    agent = LogicAgent(router=router)
    findings = await agent.run(SAMPLE_DIFF, "rev-1")
    assert findings[0].severity == "medium"


@pytest.mark.asyncio
async def test_logic_agent_empty_findings():
    from argus.agents.logic.agent import LogicAnalysisResponse

    mock_response = LogicAnalysisResponse(findings=[])
    router = make_mock_router(mock_response)
    agent = LogicAgent(router=router)
    findings = await agent.run(SAMPLE_DIFF, "rev-1")
    assert findings == []


# ---------------------------------------------------------------------------
# Code Quality Agent — complexity check (deterministic)
# ---------------------------------------------------------------------------

def test_complexity_check_detects_complex_function():
    findings = check_complexity(COMPLEX_DIFF, "rev-2", threshold=5)
    assert len(findings) >= 1
    f = findings[0]
    assert f.agent_name == "code_quality"
    assert f.category == "quality"
    assert "very_complex" in f.description
    assert f.confidence == 1.0


def test_complexity_check_ignores_simple_function():
    findings = check_complexity(SAMPLE_DIFF, "rev-2", threshold=10)
    assert findings == []


def test_complexity_threshold_respected():
    # With a very high threshold, nothing should be flagged
    findings = check_complexity(COMPLEX_DIFF, "rev-2", threshold=100)
    assert findings == []


@pytest.mark.asyncio
async def test_code_quality_agent_no_router_returns_complexity_only():
    agent = CodeQualityAgent(router=None, complexity_threshold=5)
    findings = await agent.run(COMPLEX_DIFF, "rev-3")
    assert any("very_complex" in f.description for f in findings)


@pytest.mark.asyncio
async def test_code_quality_agent_combines_llm_and_complexity():
    from argus.agents.code_quality.agent import QualityAnalysisResponse, RawFinding

    mock_response = QualityAnalysisResponse(findings=[
        RawFinding(
            file_path="src/complex.py",
            line_start=1,
            description="Magic number used",
            severity="low",
        )
    ])
    router = make_mock_router(mock_response)
    agent = CodeQualityAgent(router=router, complexity_threshold=5)
    findings = await agent.run(COMPLEX_DIFF, "rev-3")

    # Should have complexity finding + LLM finding
    categories = [f.description for f in findings]
    assert any("very_complex" in d for d in categories)
    assert any("Magic number" in d for d in categories)


# ---------------------------------------------------------------------------
# Documentation Agent
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_doc_agent_no_router_returns_empty():
    agent = DocumentationAgent(router=None)
    findings = await agent.run(SAMPLE_DIFF, "rev-4")
    assert findings == []


@pytest.mark.asyncio
async def test_doc_agent_returns_missing_docs_findings():
    from argus.agents.documentation.agent import DocAnalysisResponse, RawFinding

    mock_response = DocAnalysisResponse(findings=[
        RawFinding(
            file_path="src/main.py",
            line_start=1,
            description="Public function 'process' is missing a docstring",
            severity="low",
            confidence=0.85,
        )
    ])
    router = make_mock_router(mock_response)
    agent = DocumentationAgent(router=router)
    findings = await agent.run(SAMPLE_DIFF, "rev-4")

    assert len(findings) == 1
    assert findings[0].category == "missing_docs"
    assert findings[0].agent_name == "documentation"
    assert findings[0].severity == "low"


@pytest.mark.asyncio
async def test_doc_agent_all_findings_have_correct_category():
    from argus.agents.documentation.agent import DocAnalysisResponse, RawFinding

    mock_response = DocAnalysisResponse(findings=[
        RawFinding(file_path="f.py", line_start=1, description="a"),
        RawFinding(file_path="f.py", line_start=2, description="b"),
    ])
    router = make_mock_router(mock_response)
    agent = DocumentationAgent(router=router)
    findings = await agent.run(SAMPLE_DIFF, "rev-4")
    for f in findings:
        assert f.category == "missing_docs"
