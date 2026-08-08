"""Static Analysis Agent — deterministic, no LLM required.

Runs ruff on Python files extracted from the diff, normalizes output to
the shared Finding schema.  This agent is the baseline: it never hallucinates,
never fails due to rate limits, and gives the LLM agents concrete anchors.
"""

from __future__ import annotations

import asyncio
import json
import re
import tempfile
from pathlib import Path

from argus.guardrails.schemas import Finding

_DIFF_HUNK_RE = re.compile(
    r"^\+\+\+ b/(.+?)\n(?:@@[^\n]+@@\n)((?:[^-+@]|\+[^\+]|\+\+[^\+]).*?(?=\n\+\+\+|\Z))",
    re.MULTILINE | re.DOTALL,
)
_ADDED_LINE_RE = re.compile(r"^\+(?!\+\+)(.*)$", re.MULTILINE)


def _extract_added_python_files(diff: str) -> dict[str, list[tuple[int, str]]]:
    """Extract added/modified lines per .py file from a unified diff.

    Returns: {file_path: [(line_number, content), ...]}
    """
    files: dict[str, list[tuple[int, str]]] = {}

    # Parse diff file by file
    file_blocks = re.split(r"(?=^diff --git)", diff, flags=re.MULTILINE)
    for block in file_blocks:
        # Extract filename
        m = re.search(r"^\+\+\+ b/(.+)$", block, re.MULTILINE)
        if not m:
            continue
        filepath = m.group(1)
        if not filepath.endswith(".py"):
            continue

        # Extract added lines with their line numbers
        hunk_headers = list(re.finditer(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", block, re.MULTILINE))
        if not hunk_headers:
            continue

        added_lines: list[tuple[int, str]] = []
        for i, hunk_match in enumerate(hunk_headers):
            start_line = int(hunk_match.group(1))
            hunk_start = hunk_match.end()
            hunk_end = hunk_headers[i + 1].start() if i + 1 < len(hunk_headers) else len(block)
            hunk_body = block[hunk_start:hunk_end]

            current_line = start_line
            for line in hunk_body.split("\n"):
                if line.startswith("+") and not line.startswith("+++"):
                    added_lines.append((current_line, line[1:]))
                    current_line += 1
                elif not line.startswith("-"):
                    current_line += 1

        if added_lines:
            files[filepath] = added_lines

    return files


async def _run_ruff(source: str, filename: str = "code.py") -> list[dict]:
    """Run ruff on `source` and return parsed JSON output."""
    with tempfile.NamedTemporaryFile(
        suffix=".py", mode="w", delete=False, encoding="utf-8"
    ) as f:
        f.write(source)
        tmp_path = f.name

    try:
        proc = await asyncio.create_subprocess_exec(
            "ruff", "check", "--output-format=json", "--no-cache", tmp_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if not stdout:
            return []
        try:
            return json.loads(stdout.decode())
        except json.JSONDecodeError:
            return []
    finally:
        Path(tmp_path).unlink(missing_ok=True)


class StaticAnalysisAgent:
    """Deterministic static analysis using ruff."""

    name = "static_analysis"

    async def run(self, diff: str, review_id: str) -> list[Finding]:
        py_files = _extract_added_python_files(diff)
        if not py_files:
            return []

        findings: list[Finding] = []

        for filepath, added_lines in py_files.items():
            if not added_lines:
                continue

            # Build a synthetic source for the added lines
            source = "\n".join(content for _, content in added_lines)
            raw_findings = await _run_ruff(source)

            for issue in raw_findings:
                # Map ruff line back to diff line number
                ruff_line = issue.get("location", {}).get("row", 1)
                # Offset by the first added line's number (approximate)
                base_line = added_lines[0][0] if added_lines else 1
                actual_line = max(1, base_line + ruff_line - 1)

                code = issue.get("code", "E999")
                message = issue.get("message", "Unknown issue")

                finding = Finding(
                    review_id=review_id,
                    agent_name=self.name,
                    category="style",
                    severity=_ruff_severity(code),
                    file_path=filepath,
                    line_start=actual_line,
                    description=f"[{code}] {message}",
                    confidence=1.0,  # deterministic → always confident
                )
                findings.append(finding)

        return findings


def _ruff_severity(code: str) -> str:
    """Map ruff error codes to Argus severity levels."""
    if code.startswith("E9") or code.startswith("F8"):
        return "high"
    if code.startswith("E") or code.startswith("F"):
        return "medium"
    return "low"
