"""Documentation Agent — LLM-based docstring and README completeness analysis."""

from __future__ import annotations

from pydantic import BaseModel, Field

from argus.guardrails.schemas import Finding

SYSTEM_PROMPT = """\
You are an expert code reviewer specializing in documentation quality.

Given a unified diff, identify:
- Public functions, classes, or methods added/modified without docstrings
- Docstrings that don't describe parameters or return values
- README sections that are missing after significant feature additions
- Outdated comments that no longer match the code
- Missing type annotations that would serve as documentation

Focus ONLY on documentation gaps — not logic, style, or security issues.
"""

USER_PROMPT_TEMPLATE = """\
Review the following diff for documentation issues.

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
    severity: str = "low"
    confidence: float = Field(ge=0.0, le=1.0, default=0.7)


class DocAnalysisResponse(BaseModel):
    findings: list[RawFinding] = Field(default_factory=list)


class DocumentationAgent:
    """Checks docstring/README completeness using an LLM."""

    name = "documentation"

    def __init__(self, router=None):
        self._router = router

    async def run(self, diff: str, review_id: str) -> list[Finding]:
        if self._router is None:
            return []

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": USER_PROMPT_TEMPLATE.format(diff=diff)},
        ]
        response: DocAnalysisResponse = await self._router.complete_structured(
            messages, DocAnalysisResponse
        )
        findings: list[Finding] = []
        for raw in response.findings:
            severity = raw.severity if raw.severity in (
                "critical", "high", "medium", "low", "info"
            ) else "low"
            findings.append(Finding(
                review_id=review_id,
                agent_name=self.name,
                category="missing_docs",
                severity=severity,  # type: ignore[arg-type]
                file_path=raw.file_path,
                line_start=raw.line_start,
                line_end=raw.line_end,
                description=raw.description,
                confidence=raw.confidence,
            ))
        return findings
