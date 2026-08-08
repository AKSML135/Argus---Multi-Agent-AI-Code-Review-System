"""Report Generator — synthesises aggregated findings into a structured markdown report.

Uses an LLM to write prose explanations, but falls back to a deterministic
template when no router is configured (useful in tests and CI).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from argus.guardrails.schemas import AggregatedFindings, Finding, Report

SYSTEM_PROMPT = """\
You are a senior software engineer writing a code-review report.

Given a list of deduplicated findings, write a concise, actionable markdown report.

Structure:
## Summary
One paragraph: what was reviewed, overall risk level, total findings.

## Critical & High Findings
For each critical/high finding: bullet with file:line — description — remediation hint.

## Medium & Low Findings
For each medium/low finding: brief bullet.

## Recommendations
3-5 actionable recommendations prioritised by impact.

Rules:
- Be specific and actionable, not generic.
- Never invent findings not in the list.
- Keep total length under 800 words.
"""

USER_PROMPT_TEMPLATE = """\
Write a code-review report for the following findings.

<review_id>{review_id}</review_id>
<max_severity>{max_severity}</max_severity>
<findings>
{findings_text}
</findings>

Respond with a JSON object matching the schema.
"""


def _findings_text(findings: list[Finding]) -> str:
    lines = []
    for f in findings:
        lines.append(
            f"[{f.severity.upper()}] {f.file_path}:{f.line_start} "
            f"({f.category}) — {f.description}"
        )
    return "\n".join(lines) if lines else "No findings."


def _deterministic_report(agg: AggregatedFindings) -> str:
    """Fallback template — no LLM required."""
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    critical_high = [f for f in agg.findings if f.severity in ("critical", "high")]
    others = [f for f in agg.findings if f.severity not in ("critical", "high")]

    lines = [
        "# Code Review Report",
        "",
        f"**Review ID:** `{agg.review_id}`  ",
        f"**Generated:** {now}  ",
        f"**Overall Risk:** {agg.max_severity or 'none'}  ",
        f"**Total Findings:** {len(agg.findings)}",
        "",
        "## Critical & High Findings",
        "",
    ]
    if critical_high:
        for f in critical_high:
            lines.append(
                f"- **[{f.severity.upper()}]** `{f.file_path}:{f.line_start}` "
                f"({f.category}) — {f.description}"
            )
    else:
        lines.append("_None._")

    lines += ["", "## Medium & Low Findings", ""]
    if others:
        for f in others:
            lines.append(
                f"- **[{f.severity.upper()}]** `{f.file_path}:{f.line_start}` — {f.description}"
            )
    else:
        lines.append("_None._")

    lines += [
        "",
        "## Recommendations",
        "",
        "1. Address all critical and high severity findings before merging.",
        "2. Review flagged files for related issues not captured here.",
        "3. Add tests covering the corrected logic.",
    ]
    return "\n".join(lines)


class ReportContent(BaseModel):
    markdown: str = Field(description="Full markdown report content")


class ReportGeneratorAgent:
    """Produces the final review report from aggregated findings."""

    name = "report_generator"

    def __init__(self, router=None):
        self._router = router

    async def run(self, agg: AggregatedFindings, review_id: str) -> Report:
        if self._router is None or not agg.findings:
            markdown = _deterministic_report(agg)
        else:
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": USER_PROMPT_TEMPLATE.format(
                        review_id=review_id,
                        max_severity=agg.max_severity or "none",
                        findings_text=_findings_text(agg.findings),
                    ),
                },
            ]
            try:
                result: ReportContent = await self._router.complete_structured(
                    messages, ReportContent
                )
                markdown = result.markdown
            except Exception:
                markdown = _deterministic_report(agg)

        return Report(
            review_id=review_id,
            content_markdown=markdown,
            published=False,
        )
