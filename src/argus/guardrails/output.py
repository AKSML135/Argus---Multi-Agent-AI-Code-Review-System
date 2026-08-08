"""Output guardrails — applied after every LLM response.

Three rules:
1. Schema validation — Pydantic wrapper (already in provider.py; this adds
   per-finding post-processing).
2. Citation / hallucination check — every finding's file_path must exist
   in the diff; unverifiable findings are downgraded to low_confidence.
3. Secret / PII redaction — high-entropy strings and API-key patterns are
   masked in finding descriptions.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from argus.guardrails.schemas import Finding, GuardrailEvent

# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class OutputGuardrailResult:
    findings: list[Finding]
    events: list[GuardrailEvent]


# ---------------------------------------------------------------------------
# Secret / PII patterns
# ---------------------------------------------------------------------------

SECRET_PATTERNS: list[re.Pattern[str]] = [
    # Generic high-value key patterns
    re.compile(r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*\S+"),
    re.compile(r"(?i)sk-[A-Za-z0-9]{20,}"),           # OpenAI-style keys
    re.compile(r"(?i)gsk_[A-Za-z0-9]{20,}"),          # Groq
    re.compile(r"(?i)AIza[0-9A-Za-z_-]{35}"),         # Google API key
    re.compile(r"(?i)ghp_[A-Za-z0-9]{36}"),           # GitHub PAT
    re.compile(r"(?i)xox[baprs]-[A-Za-z0-9-]+"),      # Slack token
    re.compile(r"AKIA[0-9A-Z]{16}"),                   # AWS access key
]


def _high_entropy(s: str, threshold: float = 4.5) -> bool:
    """Return True if the string has Shannon entropy above threshold."""
    if len(s) < 16:
        return False
    freq: dict[str, int] = {}
    for c in s:
        freq[c] = freq.get(c, 0) + 1
    entropy = -sum((v / len(s)) * math.log2(v / len(s)) for v in freq.values())
    return entropy >= threshold


def _redact_secrets(text: str) -> tuple[str, bool]:
    """Redact known secret patterns from text. Returns (redacted_text, was_redacted)."""
    original = text
    for pattern in SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)

    # Also check for standalone high-entropy tokens (> 20 chars, no spaces)
    words = re.findall(r"\b[A-Za-z0-9+/=_-]{20,}\b", text)
    for word in words:
        if _high_entropy(word):
            text = text.replace(word, "[REDACTED]")

    return text, text != original


# ---------------------------------------------------------------------------
# File-path extraction from unified diff
# ---------------------------------------------------------------------------

_DIFF_FILE_RE = re.compile(r"^\+\+\+ b/(.+)$", re.MULTILINE)


def extract_diff_files(diff: str) -> set[str]:
    """Extract all file paths mentioned in a unified diff."""
    return set(_DIFF_FILE_RE.findall(diff))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check_output(
    findings: list[Finding],
    diff: str,
    review_id: str,
) -> OutputGuardrailResult:
    """Post-process a list of findings through all output guardrail rules.

    - Unverifiable file citations → downgraded to low_confidence (never dropped)
    - Secret patterns in descriptions → redacted in-place
    - Each action produces a GuardrailEvent for auditability
    """
    events: list[GuardrailEvent] = []
    diff_files = extract_diff_files(diff)
    processed: list[Finding] = []

    for finding in findings:
        f = finding.model_copy(deep=True)

        # --- Rule 1: citation check ---
        if diff_files and f.file_path not in diff_files:
            f.status = "low_confidence"
            f.confidence = min(f.confidence, 0.3)
            evt = GuardrailEvent(
                review_id=review_id,
                stage="output",
                rule_name="citation_check",
                action="flag",
                details=(
                    f"file_path '{f.file_path}' not found in diff; "
                    f"downgraded to low_confidence"
                ),
            )
            events.append(evt)

        # --- Rule 2: secret redaction ---
        redacted_desc, was_redacted = _redact_secrets(f.description)
        if was_redacted:
            f.description = redacted_desc
            evt = GuardrailEvent(
                review_id=review_id,
                stage="output",
                rule_name="secret_redaction",
                action="redact",
                details=f"Secret pattern redacted from finding {f.id}",
            )
            events.append(evt)

        processed.append(f)

    return OutputGuardrailResult(findings=processed, events=events)
