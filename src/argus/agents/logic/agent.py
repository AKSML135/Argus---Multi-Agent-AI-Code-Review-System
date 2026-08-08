"""Logic & Correctness Agent — LLM-based behavioral analysis."""

from __future__ import annotations

from pydantic import BaseModel, Field

from argus.guardrails.schemas import Finding

SYSTEM_PROMPT = """\
You are an expert code reviewer specializing in logic correctness and behavioral bugs.

Given a unified diff, identify:
- Off-by-one errors, null/undefined dereferences
- Missing error handling or exception propagation
- Edge cases not covered (empty collections, negative numbers, overflow)
- Race conditions or concurrency issues
- Incorrect return values or missing return statements
- Logic inversions (using AND instead of OR, etc.)

Focus ONLY on behavioral bugs — not style, formatting, or documentation issues.
"""

USER_PROMPT_TEMPLATE = """\
Review the following diff and identify logic and correctness issues.

<diff>
{diff}
</diff>

Respond with a JSON object matching the schema provided.
"""


class RawFinding(BaseModel):
    file_path: str
    line_start: int = Field(ge=1)
    line_end: int | None = None
    description: str
    severity: str = "medium"
    confidence: float = Field(ge=0.0, le=1.0, default=0.8)


class LogicAnalysisResponse(BaseModel):
    findings: list[RawFinding] = Field(default_factory=list)


class LogicAgent:
    name = "logic_correctness"

    def __init__(self, router=None):
        self._router = router

    async def run(self, diff: str, review_id: str) -> list[Finding]:
        if self._router is None:
            return []

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": USER_PROMPT_TEMPLATE.format(diff=diff)},
        ]
        response: LogicAnalysisResponse = await self._router.complete_structured(
            messages, LogicAnalysisResponse
        )
        findings: list[Finding] = []
        for raw in response.findings:
            severity = raw.severity if raw.severity in (
                "critical", "high", "medium", "low", "info"
            ) else "medium"
            findings.append(Finding(
                review_id=review_id,
                agent_name=self.name,
                category="logic_bug",
                severity=severity,  # type: ignore[arg-type]
                file_path=raw.file_path,
                line_start=raw.line_start,
                line_end=raw.line_end,
                description=raw.description,
                confidence=raw.confidence,
            ))
        return findings
