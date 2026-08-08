"""LLM-as-judge grading for free-form findings.

Given an actual finding and its expected finding specification, the judge
decides whether they match semantically — not just by exact field equality.

The judge output is schema-constrained and logged for every graded pair.
When no LLM router is provided the judge falls back to deterministic
keyword/field matching, which is sufficient for testing and CI without keys.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from argus.guardrails.schemas import Finding

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class ExpectedFinding(BaseModel):
    """A hand-labeled finding specification from the eval dataset."""

    category: str
    severity: str
    file_path: str
    line_start: int
    description_keywords: list[str] = Field(default_factory=list)


class JudgeVerdict(BaseModel):
    """Schema-constrained output from the LLM-as-judge grading step."""

    matched: bool
    confidence: float = Field(ge=0.0, le=1.0, default=1.0)
    reason: str = ""
    expected_id: str | None = None
    actual_finding_id: str | None = None


# ---------------------------------------------------------------------------
# Deterministic (keyword) matching — used when no LLM router is available
# ---------------------------------------------------------------------------

def _deterministic_match(actual: Finding, expected: ExpectedFinding) -> JudgeVerdict:
    """Match an actual finding against an expected spec using field rules.

    Rules (all must pass for a match):
    - ``category`` must be equal
    - ``file_path`` must be equal
    - ``line_start`` must be within ±5 lines (tolerates off-by-one in diffs)
    - At least one ``description_keyword`` must appear (case-insensitive) in
      the actual finding's description, *if* keywords are specified
    """
    if actual.category != expected.category:
        return JudgeVerdict(
            matched=False,
            confidence=1.0,
            reason=f"category mismatch: got '{actual.category}', expected '{expected.category}'",
            actual_finding_id=actual.id,
        )

    if actual.file_path != expected.file_path:
        return JudgeVerdict(
            matched=False,
            confidence=1.0,
            reason=f"file_path mismatch: got '{actual.file_path}', expected '{expected.file_path}'",
            actual_finding_id=actual.id,
        )

    line_delta = abs(actual.line_start - expected.line_start)
    if line_delta > 5:
        return JudgeVerdict(
            matched=False,
            confidence=0.8,
            reason=(
                f"line_start too far: got {actual.line_start}, "
                f"expected ~{expected.line_start} (delta={line_delta})"
            ),
            actual_finding_id=actual.id,
        )

    if expected.description_keywords:
        desc_lower = actual.description.lower()
        matched_kw = [kw for kw in expected.description_keywords if kw.lower() in desc_lower]
        if not matched_kw:
            return JudgeVerdict(
                matched=False,
                confidence=0.7,
                reason=(
                    f"no description keywords matched "
                    f"(wanted any of {expected.description_keywords!r})"
                ),
                actual_finding_id=actual.id,
            )

    return JudgeVerdict(
        matched=True,
        confidence=0.9,
        reason="category, file_path, line_start, and description keywords all match",
        actual_finding_id=actual.id,
    )


# ---------------------------------------------------------------------------
# LLM-as-judge grading
# ---------------------------------------------------------------------------

JUDGE_SYSTEM = """\
You are an expert code-review evaluator. You are given:
1. An EXPECTED finding (hand-labeled by a human reviewer)
2. An ACTUAL finding (produced by an automated agent)

Decide whether the actual finding correctly identifies the same issue as the
expected finding. Be generous with line-number discrepancies (±5 lines is fine),
and allow paraphrased descriptions. Be strict on category and file_path.

Respond ONLY with a JSON object matching the schema.
"""

JUDGE_USER_TEMPLATE = """\
EXPECTED finding:
  category: {category}
  file_path: {file_path}
  line_start: {line_start}
  description_keywords: {keywords}

ACTUAL finding:
  category: {actual_category}
  severity: {actual_severity}
  file_path: {actual_file_path}
  line_start: {actual_line_start}
  description: {actual_description}

Does the actual finding match the expected finding? Respond with a JSON object.
"""


class FindingGrader:
    """Grades actual findings against expected specs, optionally using an LLM."""

    def __init__(self, router=None):
        self._router = router

    async def grade(
        self,
        actual: Finding,
        expected: ExpectedFinding,
    ) -> JudgeVerdict:
        """Return a JudgeVerdict indicating whether ``actual`` matches ``expected``."""

        # --- Deterministic pre-check (fast path) ---
        # If category or file_path don't match, no LLM call needed.
        if actual.category != expected.category or actual.file_path != expected.file_path:
            verdict = _deterministic_match(actual, expected)
            logger.debug(
                "judge.deterministic_reject: finding_id=%s category_match=%s file_match=%s",
                actual.id,
                actual.category == expected.category,
                actual.file_path == expected.file_path,
            )
            return verdict

        # --- LLM grading (when router is available) ---
        if self._router is not None:
            try:
                messages = [
                    {"role": "system", "content": JUDGE_SYSTEM},
                    {
                        "role": "user",
                        "content": JUDGE_USER_TEMPLATE.format(
                            category=expected.category,
                            file_path=expected.file_path,
                            line_start=expected.line_start,
                            keywords=expected.description_keywords,
                            actual_category=actual.category,
                            actual_severity=actual.severity,
                            actual_file_path=actual.file_path,
                            actual_line_start=actual.line_start,
                            actual_description=actual.description,
                        ),
                    },
                ]
                verdict: JudgeVerdict = await self._router.complete_structured(
                    messages, JudgeVerdict
                )
                verdict.actual_finding_id = actual.id
                logger.info(
                    "judge.llm_verdict: finding_id=%s matched=%s confidence=%s",
                    actual.id,
                    verdict.matched,
                    verdict.confidence,
                )
                return verdict
            except Exception as exc:
                logger.warning(
                    "judge.llm_failed_falling_back: finding_id=%s error=%s",
                    actual.id,
                    str(exc),
                )

        # --- Deterministic fallback ---
        verdict = _deterministic_match(actual, expected)
        logger.debug(
            "judge.deterministic_verdict: finding_id=%s matched=%s",
            actual.id,
            verdict.matched,
        )
        return verdict
