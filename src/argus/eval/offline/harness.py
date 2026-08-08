"""Offline evaluation harness — precision, recall, F1 per finding category.

Workflow
--------
1. Load fixture PRs from ``eval_datasets/`` (JSON files).
2. For each fixture, run the full Argus pipeline (agents + aggregator) against
   the fixture diff.  No LLM router is required — agents with ``router=None``
   still run the deterministic static-analysis and secret-scanner sub-agents,
   which is enough to catch the seeded bugs that those tools can detect.
3. Match actual findings to expected findings using :class:`FindingGrader`.
4. Compute precision / recall / F1 per ``category`` across all fixtures.
5. Check against configured thresholds and return a structured result.

The harness exits non-zero (raises ``EvalThresholdError``) when F1 drops
below the configured threshold — this is the CI-gating behaviour.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from argus.agents.aggregator import AggregatorAgent
from argus.agents.code_quality.agent import CodeQualityAgent
from argus.agents.documentation.agent import DocumentationAgent
from argus.agents.logic.agent import LogicAgent
from argus.agents.security.supervisor import SecuritySupervisor
from argus.agents.static_analysis.agent import StaticAnalysisAgent
from argus.eval.offline.judge import ExpectedFinding, FindingGrader, JudgeVerdict
from argus.guardrails.output import check_output
from argus.guardrails.schemas import AggregatedFindings, Finding

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public exception
# ---------------------------------------------------------------------------


class EvalThresholdError(Exception):
    """Raised when F1 drops below the configured threshold — used for CI gating."""


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class EvalCase:
    """One fixture PR with hand-labeled expected findings."""

    id: str
    description: str
    tags: list[str]
    diff: str
    expected_findings: list[ExpectedFinding]


@dataclass
class CategoryMetrics:
    """Precision / recall / F1 for a single finding category."""

    category: str
    true_positives: int = 0
    false_positives: int = 0
    false_negatives: int = 0

    @property
    def precision(self) -> float:
        denom = self.true_positives + self.false_positives
        return self.true_positives / denom if denom else 0.0

    @property
    def recall(self) -> float:
        denom = self.true_positives + self.false_negatives
        return self.true_positives / denom if denom else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0


@dataclass
class CaseResult:
    """Result for a single fixture PR."""

    case_id: str
    actual_findings: list[Finding] = field(default_factory=list)
    verdicts: list[JudgeVerdict] = field(default_factory=list)
    false_positives: list[Finding] = field(default_factory=list)
    false_negatives: list[ExpectedFinding] = field(default_factory=list)


@dataclass
class HarnessResult:
    """Aggregate result across all fixture PRs."""

    case_results: list[CaseResult] = field(default_factory=list)
    metrics_by_category: dict[str, CategoryMetrics] = field(default_factory=dict)
    overall_f1: float = 0.0
    passed_threshold: bool = True
    threshold: float = 0.0

    def summary(self) -> str:
        lines = [
            f"Eval harness result (threshold={self.threshold:.2f}, "
            f"overall_F1={self.overall_f1:.2f}, "
            f"passed={self.passed_threshold})",
            "",
        ]
        for cat, m in sorted(self.metrics_by_category.items()):
            lines.append(
                f"  {cat:25s}  P={m.precision:.2f}  R={m.recall:.2f}  F1={m.f1:.2f}"
                f"  (TP={m.true_positives} FP={m.false_positives} FN={m.false_negatives})"
            )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Dataset loading
# ---------------------------------------------------------------------------


def load_eval_cases(dataset_dir: Path) -> list[EvalCase]:
    """Load all JSON fixture files from ``dataset_dir``."""
    cases: list[EvalCase] = []
    for path in sorted(dataset_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            expected = [
                ExpectedFinding(**ef) for ef in data.get("expected_findings", [])
            ]
            cases.append(
                EvalCase(
                    id=data["id"],
                    description=data.get("description", ""),
                    tags=data.get("tags", []),
                    diff=data["diff"],
                    expected_findings=expected,
                )
            )
        except Exception as exc:
            logger.warning("harness.load_case_failed: path=%s error=%s", str(path), str(exc))
    return cases


# ---------------------------------------------------------------------------
# Pipeline runner
# ---------------------------------------------------------------------------


async def _run_pipeline(
    diff: str,
    review_id: str,
    router=None,
    settings=None,
) -> list[Finding]:
    """Run the full agent pipeline (no graph / no HITL) and return aggregated findings."""
    if settings is None:
        from argus.config import get_settings
        settings = get_settings()

    agents = [
        StaticAnalysisAgent(),
        SecuritySupervisor(router=router),
        LogicAgent(router=router),
        CodeQualityAgent(router=router, complexity_threshold=settings.complexity_threshold),
        DocumentationAgent(router=router),
    ]

    # Gather all agent findings in parallel
    results = await asyncio.gather(
        *[a.run(diff, review_id) for a in agents],
        return_exceptions=True,
    )

    raw: list[Finding] = []
    for r in results:
        if isinstance(r, BaseException):
            logger.warning("harness.agent_error: %s", str(r))
        else:
            raw.extend(r)

    # Output guardrail — validates citations, redacts secrets
    checked = check_output(raw, diff, review_id)

    # Aggregate / dedup
    aggregator = AggregatorAgent(router=router)
    agg: AggregatedFindings = await aggregator.run(checked.findings, review_id)
    return agg.findings


# ---------------------------------------------------------------------------
# Matching helpers
# ---------------------------------------------------------------------------


async def _match_findings(
    actual_findings: list[Finding],
    expected_findings: list[ExpectedFinding],
    grader: FindingGrader,
) -> tuple[list[JudgeVerdict], list[Finding], list[ExpectedFinding]]:
    """Match actual → expected using the judge; return verdicts, FP list, FN list.

    Greedy matching: each expected finding is matched at most once.
    """
    matched_expected: set[int] = set()
    matched_actual: set[str] = set()
    verdicts: list[JudgeVerdict] = []

    # For each expected finding, find the best actual finding
    for exp_idx, exp in enumerate(expected_findings):
        best_verdict: JudgeVerdict | None = None
        best_actual_idx: int | None = None

        for act_idx, act in enumerate(actual_findings):
            if act.id in matched_actual:
                continue
            verdict = await grader.grade(act, exp)
            if verdict.matched:
                if best_verdict is None or verdict.confidence > best_verdict.confidence:
                    best_verdict = verdict
                    best_actual_idx = act_idx

        if best_verdict is not None and best_actual_idx is not None:
            matched_expected.add(exp_idx)
            matched_actual.add(actual_findings[best_actual_idx].id)
            verdicts.append(best_verdict)

    false_positives = [a for a in actual_findings if a.id not in matched_actual]
    false_negatives = [
        expected_findings[i] for i in range(len(expected_findings)) if i not in matched_expected
    ]
    return verdicts, false_positives, false_negatives


# ---------------------------------------------------------------------------
# Harness entry point
# ---------------------------------------------------------------------------


async def run_harness(
    dataset_dir: Path,
    router=None,
    f1_threshold: float = 0.0,
    settings=None,
) -> HarnessResult:
    """Run the offline evaluation harness.

    Args:
        dataset_dir: Path to the ``eval_datasets/`` directory.
        router: Optional LLMRouter. When ``None``, only deterministic agents run.
        f1_threshold: Minimum acceptable overall F1. Harness fails if F1 < threshold.
        settings: Optional Settings override (useful in tests).

    Returns:
        A :class:`HarnessResult` with per-category metrics and case-level details.

    Raises:
        EvalThresholdError: When overall F1 is below ``f1_threshold``.
    """
    cases = load_eval_cases(dataset_dir)
    if not cases:
        raise ValueError(f"No eval cases found in {dataset_dir}")

    grader = FindingGrader(router=router)
    metrics: dict[str, CategoryMetrics] = {}
    case_results: list[CaseResult] = []

    for case in cases:
        review_id = str(uuid.uuid4())
        logger.info("harness.running_case: case_id=%s review_id=%s", case.id, review_id)

        actual_findings = await _run_pipeline(
            diff=case.diff,
            review_id=review_id,
            router=router,
            settings=settings,
        )

        verdicts, fps, fns = await _match_findings(
            actual_findings, case.expected_findings, grader
        )

        case_result = CaseResult(
            case_id=case.id,
            actual_findings=actual_findings,
            verdicts=verdicts,
            false_positives=fps,
            false_negatives=fns,
        )
        case_results.append(case_result)

        # Update per-category metrics
        # True positives: matched verdicts, keyed by expected category
        for _verdict in verdicts:
            # Find the matched expected finding's category
            # verdicts come from expected findings in order → rebuild mapping
            pass  # handled below via proper category attribution

        # Build category → TP/FP/FN from case-level data
        for exp in case.expected_findings:
            cat = exp.category
            if cat not in metrics:
                metrics[cat] = CategoryMetrics(category=cat)

        # Mark TPs: expected findings that were matched
        matched_expected_categories = _matched_expected_categories(
            case.expected_findings, fns
        )
        for cat in matched_expected_categories:
            if cat not in metrics:
                metrics[cat] = CategoryMetrics(category=cat)
            metrics[cat].true_positives += 1

        # Mark FNs
        for fn in fns:
            cat = fn.category
            if cat not in metrics:
                metrics[cat] = CategoryMetrics(category=cat)
            metrics[cat].false_negatives += 1

        # Mark FPs
        for fp in fps:
            cat = fp.category
            if cat not in metrics:
                metrics[cat] = CategoryMetrics(category=cat)
            metrics[cat].false_positives += 1

        logger.info(
            "harness.case_done: case_id=%s actual=%d tp=%d fp=%d fn=%d",
            case.id,
            len(actual_findings),
            len(verdicts),
            len(fps),
            len(fns),
        )

    # Compute overall F1 (macro average across categories)
    if metrics:
        overall_f1 = sum(m.f1 for m in metrics.values()) / len(metrics)
    else:
        overall_f1 = 0.0

    passed = overall_f1 >= f1_threshold

    result = HarnessResult(
        case_results=case_results,
        metrics_by_category=metrics,
        overall_f1=overall_f1,
        passed_threshold=passed,
        threshold=f1_threshold,
    )

    logger.info(
        "harness.complete: overall_f1=%.3f threshold=%.3f passed=%s categories=%s",
        overall_f1,
        f1_threshold,
        passed,
        list(metrics.keys()),
    )

    if not passed:
        raise EvalThresholdError(
            f"Overall F1 {overall_f1:.3f} is below threshold {f1_threshold:.3f}. "
            f"Category breakdown:\n{result.summary()}"
        )

    return result


def _matched_expected_categories(
    all_expected: list[ExpectedFinding],
    false_negatives: list[ExpectedFinding],
) -> list[str]:
    """Return category labels for all expected findings that were *not* false negatives."""
    fn_keys = {
        (fn.category, fn.file_path, fn.line_start) for fn in false_negatives
    }
    return [
        exp.category
        for exp in all_expected
        if (exp.category, exp.file_path, exp.line_start) not in fn_keys
    ]
