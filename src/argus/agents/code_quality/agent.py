"""Code Quality Agent — LLM-based style analysis + deterministic complexity check."""

from __future__ import annotations

import ast
import re

from pydantic import BaseModel, Field

from argus.guardrails.schemas import Finding

SYSTEM_PROMPT = """\
You are an expert code reviewer specializing in code quality, style, and maintainability.

Given a unified diff, identify:
- Poor naming conventions (single-letter variables outside loops, misleading names)
- Functions that do too many things (violate single responsibility)
- Excessive nesting or deeply nested conditions
- Code duplication or repeated logic that should be extracted
- Magic numbers or strings that should be constants
- Missing type annotations on public functions

Focus ONLY on quality issues — not security, logic bugs, or documentation.
"""

USER_PROMPT_TEMPLATE = """\
Review the following diff for code quality issues.

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
    confidence: float = Field(ge=0.0, le=1.0, default=0.75)


class QualityAnalysisResponse(BaseModel):
    findings: list[RawFinding] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Deterministic complexity check
# ---------------------------------------------------------------------------

def _cyclomatic_complexity(source: str) -> int:
    """Rough cyclomatic complexity: 1 + number of branching keywords."""
    branches = len(re.findall(
        r'\b(if|elif|else|for|while|except|and|or|assert|with)\b', source
    ))
    return 1 + branches


def _extract_added_python_source(diff: str) -> dict[str, tuple[str, int]]:
    """Return {filepath: (source_code, first_line_number)} for added Python files."""
    result: dict[str, tuple[str, int]] = {}
    file_blocks = re.split(r"(?=^diff --git)", diff, flags=re.MULTILINE)
    for block in file_blocks:
        m = re.search(r"^\+\+\+ b/(.+)$", block, re.MULTILINE)
        if not m or not m.group(1).endswith(".py"):
            continue
        filepath = m.group(1)

        hunk_headers = list(re.finditer(
            r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", block, re.MULTILINE
        ))
        lines: list[str] = []
        first_line = 1
        for i, hm in enumerate(hunk_headers):
            if i == 0:
                first_line = int(hm.group(1))
            hunk_start = hm.end()
            hunk_end = hunk_headers[i + 1].start() if i + 1 < len(hunk_headers) else len(block)
            for line in block[hunk_start:hunk_end].split("\n"):
                if line.startswith("+") and not line.startswith("+++"):
                    lines.append(line[1:])
        if lines:
            result[filepath] = ("\n".join(lines), first_line)
    return result


def check_complexity(diff: str, review_id: str, threshold: int = 10) -> list[Finding]:
    """Deterministic complexity check — no LLM involved."""
    findings: list[Finding] = []
    for filepath, (source, first_line) in _extract_added_python_source(diff).items():
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            func_src = ast.get_source_segment(source, node) or ""
            cc = _cyclomatic_complexity(func_src)
            if cc > threshold:
                line_num = max(1, first_line + (node.lineno - 1))
                findings.append(Finding(
                    review_id=review_id,
                    agent_name="code_quality",
                    category="quality",
                    severity="medium",
                    file_path=filepath,
                    line_start=line_num,
                    description=(
                        f"Function '{node.name}' has cyclomatic complexity {cc} "
                        f"(threshold: {threshold}). Consider refactoring."
                    ),
                    confidence=1.0,
                ))
    return findings


class CodeQualityAgent:
    """Code quality: LLM-based style analysis + deterministic complexity."""

    name = "code_quality"

    def __init__(self, router=None, complexity_threshold: int = 10):
        self._router = router
        self._threshold = complexity_threshold

    async def run(self, diff: str, review_id: str) -> list[Finding]:
        findings: list[Finding] = []

        # Always run deterministic complexity check
        findings.extend(check_complexity(diff, review_id, self._threshold))

        if self._router is None:
            return findings

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": USER_PROMPT_TEMPLATE.format(diff=diff)},
        ]
        response: QualityAnalysisResponse = await self._router.complete_structured(
            messages, QualityAnalysisResponse
        )
        for raw in response.findings:
            severity = raw.severity if raw.severity in (
                "critical", "high", "medium", "low", "info"
            ) else "low"
            findings.append(Finding(
                review_id=review_id,
                agent_name=self.name,
                category="quality",
                severity=severity,  # type: ignore[arg-type]
                file_path=raw.file_path,
                line_start=raw.line_start,
                line_end=raw.line_end,
                description=raw.description,
                confidence=raw.confidence,
            ))
        return findings
