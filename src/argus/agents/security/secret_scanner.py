"""Secret Scanner Agent — deterministic regex-based credential detection.

Runs before the SAST agent and flags any hardcoded secrets in the diff.
Uses the same pattern library as the output guardrail but reports findings
rather than redacting them.
"""

from __future__ import annotations

import math
import re

from argus.guardrails.schemas import Finding

# ---------------------------------------------------------------------------
# Pattern definitions
# ---------------------------------------------------------------------------

_SECRET_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("AWS Access Key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("OpenAI API Key", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("Groq API Key", re.compile(r"gsk_[A-Za-z0-9]{20,}")),
    ("Google API Key", re.compile(r"AIza[0-9A-Za-z_-]{35}")),
    ("GitHub PAT", re.compile(r"ghp_[A-Za-z0-9]{36}")),
    ("Slack Token", re.compile(r"xox[baprs]-[A-Za-z0-9-]+")),
    ("Generic Secret", re.compile(
        r"(?i)(?:api[_-]?key|secret|password|token|passwd)\s*[:=]\s*['\"]?([A-Za-z0-9+/=_-]{8,})['\"]?"
    )),
    ("Private Key Header", re.compile(r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----")),
]


def _high_entropy(s: str, threshold: float = 4.5) -> bool:
    if len(s) < 16:
        return False
    freq: dict[str, int] = {}
    for c in s:
        freq[c] = freq.get(c, 0) + 1
    entropy = -sum((v / len(s)) * math.log2(v / len(s)) for v in freq.values())
    return entropy >= threshold


def _parse_diff_added_lines(diff: str) -> list[tuple[str, int, str]]:
    """Yield (filepath, line_number, line_content) for each added line."""
    result: list[tuple[str, int, str]] = []
    current_file = ""
    current_line = 0

    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[6:]
        elif line.startswith("@@ "):
            m = re.search(r"\+(\d+)", line)
            current_line = int(m.group(1)) if m else 1
        elif line.startswith("+") and not line.startswith("+++"):
            result.append((current_file, current_line, line[1:]))
            current_line += 1
        elif not line.startswith("-"):
            current_line += 1

    return result


class SecretScannerAgent:
    """Deterministic secret/credential scanner — no LLM."""

    name = "secret_scanner"

    async def run(self, diff: str, review_id: str) -> list[Finding]:
        findings: list[Finding] = []
        seen: set[str] = set()

        for filepath, line_num, content in _parse_diff_added_lines(diff):
            if not filepath:
                continue

            for rule_name, pattern in _SECRET_RULES:
                for _match in pattern.finditer(content):
                    dedup_key = f"{filepath}:{line_num}:{rule_name}"
                    if dedup_key in seen:
                        continue
                    seen.add(dedup_key)

                    findings.append(Finding(
                        review_id=review_id,
                        agent_name=self.name,
                        category="leaked_secret",
                        severity="critical",
                        file_path=filepath,
                        line_start=line_num,
                        description=(
                            f"{rule_name} detected. Remove from source and "
                            "rotate the credential immediately."
                        ),
                        confidence=1.0,
                    ))

            # High-entropy standalone token check
            for token in re.findall(r"\b[A-Za-z0-9+/=_-]{20,}\b", content):
                if _high_entropy(token):
                    dedup_key = f"{filepath}:{line_num}:entropy:{token[:8]}"
                    if dedup_key in seen:
                        continue
                    seen.add(dedup_key)
                    findings.append(Finding(
                        review_id=review_id,
                        agent_name=self.name,
                        category="leaked_secret",
                        severity="high",
                        file_path=filepath,
                        line_start=line_num,
                        description=(
                            "High-entropy string detected — possible hardcoded secret."
                        ),
                        confidence=0.8,
                    ))

        return findings
