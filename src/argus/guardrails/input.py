"""Input guardrails — applied before any LLM sees the diff.

Two rules:
1. Prompt-injection detection: flag diffs containing adversarial patterns.
2. Size limits: reject diffs exceeding the configured line count.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from argus.guardrails.schemas import GuardrailEvent

# ---------------------------------------------------------------------------
# Typed result / error
# ---------------------------------------------------------------------------

class InputGuardrailError(Exception):
    """Raised when the diff is blocked by an input guardrail."""

    def __init__(self, rule: str, details: str, event: GuardrailEvent):
        super().__init__(f"[{rule}] {details}")
        self.rule = rule
        self.details = details
        self.event = event


@dataclass
class InputGuardrailResult:
    """Successful guardrail pass — diff is clean."""

    diff: str
    events: list[GuardrailEvent]


# ---------------------------------------------------------------------------
# Injection patterns
# ---------------------------------------------------------------------------

INJECTION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"ignore\s+(previous|all|prior)\s+instructions", re.IGNORECASE),
    re.compile(r"you\s+are\s+now\s+a\s+", re.IGNORECASE),
    re.compile(r"system\s*:\s*", re.IGNORECASE),
    re.compile(r"<\|?system\|?>", re.IGNORECASE),
    re.compile(r"disregard\s+(previous|all)\s+(instructions|rules)", re.IGNORECASE),
    re.compile(r"jailbreak", re.IGNORECASE),
    re.compile(r"\[INST\]", re.IGNORECASE),
    re.compile(r"###\s*Instruction", re.IGNORECASE),
]


def _detect_injection(diff: str) -> str | None:
    """Return the matched pattern string if injection is detected, else None."""
    for pattern in INJECTION_PATTERNS:
        m = pattern.search(diff)
        if m:
            return m.group(0)
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check_input(
    diff: str,
    review_id: str,
    max_lines: int = 2000,
) -> InputGuardrailResult:
    """Validate a raw diff against all input guardrail rules.

    Returns `InputGuardrailResult` on success.
    Raises `InputGuardrailError` if the diff must be blocked.
    """
    events: list[GuardrailEvent] = []

    # --- Rule 1: size limit ---
    line_count = len(diff.splitlines())
    if line_count > max_lines:
        evt = GuardrailEvent(
            review_id=review_id,
            stage="input",
            rule_name="size_limit",
            action="block",
            details=f"Diff has {line_count} lines, limit is {max_lines}",
        )
        raise InputGuardrailError(
            rule="size_limit",
            details=f"Diff has {line_count} lines (limit {max_lines})",
            event=evt,
        )

    # --- Rule 2: injection detection ---
    match = _detect_injection(diff)
    if match:
        evt = GuardrailEvent(
            review_id=review_id,
            stage="input",
            rule_name="injection_detection",
            action="block",
            details=f"Injection pattern detected: '{match}'",
        )
        raise InputGuardrailError(
            rule="injection_detection",
            details=f"Injection pattern detected: '{match}'",
            event=evt,
        )

    return InputGuardrailResult(diff=diff, events=events)
