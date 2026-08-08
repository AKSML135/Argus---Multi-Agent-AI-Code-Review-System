"""Argus CLI — review a diff file from the command line or via the API."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import httpx
import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table

# Module-level imports so tests can patch `argus.cli.StaticAnalysisAgent` etc.
from argus.agents.aggregator import AggregatorAgent
from argus.agents.code_quality.agent import CodeQualityAgent
from argus.agents.documentation.agent import DocumentationAgent
from argus.agents.logic.agent import LogicAgent
from argus.agents.report_generator import ReportGeneratorAgent
from argus.agents.security.supervisor import SecuritySupervisor
from argus.agents.static_analysis.agent import StaticAnalysisAgent
from argus.guardrails.input import InputGuardrailError, check_input
from argus.guardrails.output import check_output
from argus.observability.logging import configure_logging

app = typer.Typer(name="argus", help="Argus — AI-powered code review")
console = Console()
err_console = Console(stderr=True)

# Severity order — lower index = higher severity
_SEVERITY_ORDER = ["critical", "high", "medium", "low", "info"]


# ---------------------------------------------------------------------------
# Public helpers (importable by tests)
# ---------------------------------------------------------------------------

def _exit_code_for_severity(max_severity: str | None, fail_on: str | None) -> int:
    """Return 1 if ``max_severity`` meets or exceeds ``fail_on``, else 0.

    Args:
        max_severity: The highest severity found in the review, or None.
        fail_on: The threshold severity from ``--fail-on``, or None (no gating).

    Returns:
        1 if the review has a finding at or above the ``fail_on`` threshold, 0 otherwise.
    """
    if fail_on is None or max_severity is None:
        return 0
    try:
        actual_rank = _SEVERITY_ORDER.index(max_severity)
        threshold_rank = _SEVERITY_ORDER.index(fail_on)
    except ValueError:
        return 0
    # Lower rank = higher severity; non-zero exit when actual ≤ threshold (at/above threshold)
    return 1 if actual_rank <= threshold_rank else 0


def _parse_comment_command(comment_body: str) -> dict | None:
    """Parse a GitHub PR comment body for an ``/argus <command>`` directive.

    Returns a dict with ``{"command": str, "action": str}`` when a known command
    is found, or ``None`` when no command is present.

    Recognised commands:
        /argus approve  → action="approve"
        /argus reject   → action="reject"
    """
    _COMMAND_MAP = {
        "approve": "approve",
        "reject": "reject",
    }
    for line in comment_body.splitlines():
        stripped = line.strip()
        if stripped.startswith("/argus "):
            parts = stripped.split()
            if len(parts) >= 2:
                cmd = parts[1].lower()
                if cmd in _COMMAND_MAP:
                    return {"command": cmd, "action": _COMMAND_MAP[cmd]}
    return None


def _poll_for_approval(
    api_url: str,
    review_id: str,
    api_key: str,
    timeout_seconds: int = 300,
    poll_interval: int = 5,
) -> str:
    """Poll ``GET /reviews/{id}`` until status leaves ``awaiting_human``.

    Returns the final status string when the review is no longer waiting.
    Raises ``typer.Exit(code=3)`` on timeout — fail-safe blocks the merge.
    """
    headers = {"X-API-Key": api_key}
    url = f"{api_url.rstrip('/')}/reviews/{review_id}"
    deadline = time.monotonic() + timeout_seconds

    while time.monotonic() < deadline:
        try:
            resp = httpx.get(url, headers=headers, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                status = data.get("status", "unknown")
                if status != "awaiting_human":
                    return status
        except Exception:
            pass
        time.sleep(poll_interval)

    raise typer.Exit(code=3)


# ---------------------------------------------------------------------------
# Shared diff reader
# ---------------------------------------------------------------------------

def _read_diff(diff_path: Path | None, diff_stdin: bool) -> str:
    if diff_stdin or diff_path is None:
        if sys.stdin.isatty():
            err_console.print("[red]Error:[/red] Provide --diff or pipe diff via stdin")
            raise typer.Exit(code=1)
        return sys.stdin.read()
    if not diff_path.exists():
        err_console.print(f"[red]Error:[/red] File not found: {diff_path}")
        raise typer.Exit(code=1)
    return diff_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Local pipeline runner (no-wait / no-API mode)
# ---------------------------------------------------------------------------

async def _run_review_local(
    diff_text: str,
    review_id: str,
    repo: str,
    pr: int | None,
    json_output: bool,
    fail_on: str | None,
) -> int:
    """Run agents locally (no LLM unless keys present). Returns the exit code."""
    from argus.config import get_settings
    settings = get_settings()

    # Input guardrail
    try:
        check_input(diff_text, review_id, max_lines=settings.max_diff_lines)
    except InputGuardrailError as exc:
        err_console.print(f"[red]Blocked by guardrail:[/red] {exc}")
        return 2

    # Router — optional, only when API keys are configured
    router = None
    try:
        from argus.llm.provider import GroqProvider
        from argus.llm.router import LLMRouter
        key = settings.groq_api_key
        if key:
            router = LLMRouter(primary=GroqProvider(api_key=key), max_retries=settings.max_retries)
    except Exception:
        pass

    agents = [
        StaticAnalysisAgent(),
        SecuritySupervisor(router=router),
        LogicAgent(router=router),
        CodeQualityAgent(router=router, complexity_threshold=settings.complexity_threshold),
        DocumentationAgent(router=router),
    ]

    with console.status("[bold green]Running agents…"):
        results = await asyncio.gather(*[a.run(diff_text, review_id) for a in agents])

    raw_findings = [f for findings in results for f in findings]

    # Output guardrail
    output_result = check_output(raw_findings, diff_text, review_id)

    # Aggregate
    aggregator = AggregatorAgent(router=router, max_iterations=settings.max_refine_iterations)
    agg = await aggregator.run(output_result.findings, review_id)

    # Report
    report_agent = ReportGeneratorAgent(router=router)
    report = await report_agent.run(agg, review_id)

    if json_output:
        output = {
            "review_id": review_id,
            "max_severity": agg.max_severity,
            "finding_count": len(agg.findings),
            "findings": [
                {
                    "severity": f.severity,
                    "category": f.category,
                    "file_path": f.file_path,
                    "line_start": f.line_start,
                    "description": f.description,
                    "agent": f.agent_name,
                    "confidence": f.confidence,
                }
                for f in agg.findings
            ],
            "report": report.content_markdown,
        }
        console.print_json(json.dumps(output))
        return _exit_code_for_severity(agg.max_severity, fail_on)

    # Rich table output
    if agg.findings:
        table = Table(title=f"Findings ({len(agg.findings)} total)", show_lines=True)
        table.add_column("Severity", style="bold", min_width=8)
        table.add_column("File", style="cyan")
        table.add_column("Line", justify="right")
        table.add_column("Category")
        table.add_column("Description", max_width=60)

        sev_colors = {
            "critical": "red", "high": "orange3",
            "medium": "yellow", "low": "green", "info": "blue",
        }
        for f in agg.findings:
            color = sev_colors.get(f.severity, "white")
            table.add_row(
                f"[{color}]{f.severity.upper()}[/{color}]",
                f.file_path,
                str(f.line_start),
                f.category,
                f.description[:80],
            )
        console.print(table)
    else:
        console.print("[green]✓ No findings — diff looks clean![/green]")

    console.print()
    console.print(Markdown(report.content_markdown))

    return _exit_code_for_severity(agg.max_severity, fail_on)


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

@app.command("review")
def review(
    diff: Path | None = typer.Option(None, "--diff", "-d", help="Path to unified diff file"),  # noqa: B008
    stdin: bool = typer.Option(False, "--stdin", help="Read diff from stdin"),
    repo: str = typer.Option("", "--repo", help="Repository name (e.g. owner/repo)"),
    pr: int | None = typer.Option(None, "--pr", help="PR number"),
    json_output: bool = typer.Option(False, "--json", help="Output findings as JSON"),
    no_wait: bool = typer.Option(False, "--no-wait", help="Run locally without HITL / API"),
    wait_for_approval: bool = typer.Option(False, "--wait-for-approval", help="Poll API until approved"),
    fail_on: str | None = typer.Option(
        None, "--fail-on",
        help="Exit non-zero if findings >= this severity (critical|high|medium|low|info)",
    ),
    api_url: str = typer.Option("http://localhost:8000", "--api-url", help="Argus API base URL"),
    api_key: str = typer.Option("dev-secret-key", "--api-key", help="Argus API key"),
    timeout: int = typer.Option(300, "--timeout", help="Approval poll timeout in seconds"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Run a code review on a unified diff."""
    configure_logging(level="DEBUG" if verbose else "WARNING")

    diff_text = _read_diff(diff, stdin)

    import uuid
    review_id = str(uuid.uuid4())

    console.print(f"[bold cyan]Argus Code Review[/bold cyan]  review_id={review_id}")

    if wait_for_approval:
        # Submit to API, then poll
        _run_wait_for_approval(
            diff_text=diff_text,
            review_id=review_id,
            api_url=api_url,
            api_key=api_key,
            timeout_seconds=timeout,
            fail_on=fail_on,
        )
    else:
        # Local mode (--no-wait is implicit when --wait-for-approval is absent)
        exit_code = asyncio.run(_run_review_local(
            diff_text=diff_text,
            review_id=review_id,
            repo=repo,
            pr=pr,
            json_output=json_output,
            fail_on=fail_on,
        ))
        raise typer.Exit(code=exit_code)


def _run_wait_for_approval(
    diff_text: str,
    review_id: str,
    api_url: str,
    api_key: str,
    timeout_seconds: int,
    fail_on: str | None,
) -> None:
    """Submit a review to the API then block until it's approved or timeout."""
    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
    submit_url = f"{api_url.rstrip('/')}/reviews"

    try:
        resp = httpx.post(
            submit_url,
            json={"diff": diff_text, "review_id": review_id},
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        server_review_id = data.get("review_id", review_id)
    except Exception as exc:
        err_console.print(f"[red]Failed to submit review:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    console.print(f"Review submitted: [cyan]{server_review_id}[/cyan]  polling for approval…")

    final_status = _poll_for_approval(
        api_url=api_url,
        review_id=server_review_id,
        api_key=api_key,
        timeout_seconds=timeout_seconds,
    )

    if final_status in ("published",):
        console.print(f"[green]✓ Review approved — status: {final_status}[/green]")
        raise typer.Exit(code=0)
    else:
        console.print(f"[red]Review ended with status: {final_status}[/red]")
        raise typer.Exit(code=1)


@app.command("parse-comment")
def parse_comment(
    comment: str = typer.Argument(..., help="PR comment body to parse for /argus commands"),
):
    """Parse a GitHub PR comment for /argus approve or /argus reject directives.

    Outputs a JSON object: {"command": str|null, "action": str|null}.
    Exits 0 always — the caller decides what to do with the result.
    """
    result = _parse_comment_command(comment)
    if result:
        console.print_json(json.dumps(result))
    else:
        console.print_json(json.dumps({"command": None, "action": None}))


@app.command("serve")
def serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port"),
    reload: bool = typer.Option(False, "--reload"),
):
    """Start the Argus FastAPI server."""
    import uvicorn
    configure_logging()
    uvicorn.run(
        "argus.api.app:app",
        host=host,
        port=port,
        reload=reload,
    )


def main():
    app()


if __name__ == "__main__":
    main()
