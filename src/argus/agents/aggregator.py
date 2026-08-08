"""Aggregator agent — deduplication, severity reconciliation, and critic loop.

The critic loop asks the LLM to review the merged findings and flag any that
are likely false positives (confidence < threshold). It runs at most
max_iterations times or until no findings are flagged.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from argus.guardrails.schemas import AggregatedFindings, Finding

# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

_SEVERITY_ORDER = ["critical", "high", "medium", "low", "info"]


def _severity_rank(s: str) -> int:
    try:
        return _SEVERITY_ORDER.index(s)
    except ValueError:
        return len(_SEVERITY_ORDER)


def _dedup_findings(findings: list[Finding]) -> list[Finding]:
    """Merge findings that share the same (file, line, category).

    When duplicates exist: keep highest severity, average confidence,
    prefer the description from the highest-confidence finding.
    """
    groups: dict[tuple, list[Finding]] = {}
    for f in findings:
        key = (f.file_path, f.line_start, f.category)
        groups.setdefault(key, []).append(f)

    merged: list[Finding] = []
    for _key, group in groups.items():
        best = min(group, key=lambda x: _severity_rank(x.severity))
        avg_conf = sum(x.confidence for x in group) / len(group)
        canonical = best.model_copy(update={
            "confidence": round(avg_conf, 3),
            "dedup_group_id": best.id,
        })
        merged.append(canonical)

    return sorted(merged, key=lambda f: _severity_rank(f.severity))


# ---------------------------------------------------------------------------
# Critic loop
# ---------------------------------------------------------------------------

CRITIC_SYSTEM = """\
You are a senior code-review QA engineer. Your job is to review a list of
findings produced by automated agents and identify likely false positives.

For each finding you think is a false positive, return its `id`.
Be conservative: only flag findings you are highly confident are wrong.
"""

CRITIC_USER_TEMPLATE = """\
Review the following code-review findings and identify false positives.

<findings>
{findings_json}
</findings>

Respond with a JSON object matching the schema.
"""


class CriticResponse(BaseModel):
    false_positive_ids: list[str] = Field(default_factory=list)
    reasoning: str | None = None


class AggregatorAgent:
    """Deduplicates findings and runs an optional critic refinement loop."""

    name = "aggregator"

    def __init__(self, router=None, max_iterations: int = 3, confidence_threshold: float = 0.4):
        self._router = router
        self._max_iterations = max_iterations
        self._confidence_threshold = confidence_threshold

    async def run(
        self,
        findings: list[Finding],
        review_id: str,
    ) -> AggregatedFindings:
        # Step 1: Deduplicate
        deduped = _dedup_findings(findings)

        # Step 2: Critic loop (skipped if no router)
        iteration = 0
        if self._router and deduped:
            for _iteration in range(1, self._max_iterations + 1):
                import json
                findings_json = json.dumps([
                    {
                        "id": f.id,
                        "file_path": f.file_path,
                        "line_start": f.line_start,
                        "category": f.category,
                        "severity": f.severity,
                        "description": f.description,
                        "agent_name": f.agent_name,
                        "confidence": f.confidence,
                    }
                    for f in deduped
                ], indent=2)

                messages = [
                    {"role": "system", "content": CRITIC_SYSTEM},
                    {"role": "user", "content": CRITIC_USER_TEMPLATE.format(
                        findings_json=findings_json
                    )},
                ]
                critic: CriticResponse = await self._router.complete_structured(
                    messages, CriticResponse
                )

                if not critic.false_positive_ids:
                    break

                # Remove flagged findings (set status to false_positive)
                fp_set = set(critic.false_positive_ids)
                deduped = [
                    f.model_copy(update={"status": "false_positive"})
                    if f.id in fp_set else f
                    for f in deduped
                ]
                # Filter out false positives for next iteration
                remaining = [f for f in deduped if f.status != "false_positive"]
                if not remaining:
                    break
                deduped = remaining

        agg = AggregatedFindings(
            review_id=review_id,
            findings=deduped,
            refine_iterations=iteration,
        )
        agg.max_severity = agg.compute_max_severity()
        return agg
