"""Tests for M3: Output guardrails."""

import pytest

from argus.guardrails.output import check_output, extract_diff_files
from argus.guardrails.schemas import Finding


SAMPLE_DIFF = """\
diff --git a/src/main.py b/src/main.py
--- a/src/main.py
+++ b/src/main.py
@@ -1,3 +1,4 @@
 def hello():
+    print("world")
     return 42
"""


def make_finding(**kwargs) -> Finding:
    base = dict(
        review_id="rev-1",
        agent_name="test",
        category="logic_bug",
        severity="medium",
        file_path="src/main.py",
        line_start=2,
        description="Test finding",
    )
    base.update(kwargs)
    return Finding(**base)


# ---------------------------------------------------------------------------
# Citation / hallucination check
# ---------------------------------------------------------------------------

def test_valid_file_path_passes():
    finding = make_finding(file_path="src/main.py")
    result = check_output([finding], SAMPLE_DIFF, "rev-1")
    assert result.findings[0].status == "open"
    assert not any(e.rule_name == "citation_check" for e in result.events)


def test_invalid_file_path_downgraded_not_dropped():
    finding = make_finding(file_path="src/nonexistent.py")
    result = check_output([finding], SAMPLE_DIFF, "rev-1")
    # Must still be present
    assert len(result.findings) == 1
    assert result.findings[0].status == "low_confidence"
    assert result.findings[0].confidence <= 0.3


def test_citation_event_recorded():
    finding = make_finding(file_path="src/ghost.py")
    result = check_output([finding], SAMPLE_DIFF, "rev-1")
    events = [e for e in result.events if e.rule_name == "citation_check"]
    assert len(events) == 1
    assert events[0].action == "flag"
    assert events[0].stage == "output"


# ---------------------------------------------------------------------------
# Secret redaction
# ---------------------------------------------------------------------------

def test_api_key_pattern_redacted():
    finding = make_finding(description="Found hardcoded api_key=sk-abcdef1234567890ABCDEF")
    result = check_output([finding], SAMPLE_DIFF, "rev-1")
    assert "[REDACTED]" in result.findings[0].description
    assert "sk-abcdef" not in result.findings[0].description


def test_clean_description_not_modified():
    finding = make_finding(description="Off-by-one error in loop condition")
    result = check_output([finding], SAMPLE_DIFF, "rev-1")
    assert result.findings[0].description == "Off-by-one error in loop condition"
    assert not any(e.rule_name == "secret_redaction" for e in result.events)


def test_redaction_preserves_metadata():
    """Secret is masked, but file/line/rule metadata stays intact."""
    finding = make_finding(
        file_path="src/main.py",
        line_start=5,
        description="Token found: ghp_ABCDE12345ABCDE12345ABCDE12345ABCDE12",
    )
    result = check_output([finding], SAMPLE_DIFF, "rev-1")
    f = result.findings[0]
    assert f.file_path == "src/main.py"
    assert f.line_start == 5
    assert "[REDACTED]" in f.description


def test_redaction_event_recorded():
    finding = make_finding(description="key=AKIA1234567890ABCDEF")
    result = check_output([finding], SAMPLE_DIFF, "rev-1")
    events = [e for e in result.events if e.rule_name == "secret_redaction"]
    assert len(events) == 1
    assert events[0].action == "redact"


# ---------------------------------------------------------------------------
# extract_diff_files
# ---------------------------------------------------------------------------

def test_extract_diff_files():
    files = extract_diff_files(SAMPLE_DIFF)
    assert "src/main.py" in files


def test_extract_diff_files_multiple():
    diff = SAMPLE_DIFF + """
diff --git a/tests/test_main.py b/tests/test_main.py
--- a/tests/test_main.py
+++ b/tests/test_main.py
@@ -1 +1,2 @@
+import pytest
"""
    files = extract_diff_files(diff)
    assert "src/main.py" in files
    assert "tests/test_main.py" in files


# ---------------------------------------------------------------------------
# All events are structured GuardrailEvent
# ---------------------------------------------------------------------------

def test_all_events_have_review_id():
    findings = [
        make_finding(file_path="ghost.py"),
        make_finding(description="api_key=secret123key_value_here"),
    ]
    result = check_output(findings, SAMPLE_DIFF, "rev-42")
    for evt in result.events:
        assert evt.review_id == "rev-42"
        assert evt.stage == "output"
        assert evt.action in ("block", "flag", "redact")
