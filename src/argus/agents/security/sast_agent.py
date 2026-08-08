"""SAST Agent — LLM-based security vulnerability analysis.

Detects OWASP Top-10 class vulnerabilities, injection flaws, insecure
dependencies, and authentication/authorization gaps.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from argus.guardrails.schemas import Finding

SYSTEM_PROMPT = """\
You are an expert application security engineer performing static analysis.

Given a unified diff, identify:
- Injection vulnerabilities (SQL, command, LDAP, XPath injection)
- Cross-Site Scripting (XSS) — reflected, stored, or DOM-based
- Insecure deserialization (pickle.loads, yaml.load without Loader, etc.)
- Path traversal / directory traversal vulnerabilities
- Use of weak cryptographic primitives (MD5, SHA1 for passwords, ECB mode)
- Missing authentication or authorization checks
- Insecure direct object references (IDOR)
- Server-Side Request Forgery (SSRF)
- XML External Entity (XXE) vulnerabilities
- Use of dangerous functions (eval, exec, os.system with user input)

For each finding, map to the appropriate OWASP category in the description.
Focus ONLY on security — not style, logic bugs, or documentation.
"""

USER_PROMPT_TEMPLATE = """\
Perform security analysis on the following diff.

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
    severity: str = "high"
    confidence: float = Field(ge=0.0, le=1.0, default=0.85)


class SastResponse(BaseModel):
    findings: list[RawFinding] = Field(default_factory=list)


class SastAgent:
    """LLM-based SAST — detects OWASP-class vulnerabilities."""

    name = "sast"

    def __init__(self, router=None):
        self._router = router

    async def run(self, diff: str, review_id: str) -> list[Finding]:
        if self._router is None:
            return []

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": USER_PROMPT_TEMPLATE.format(diff=diff)},
        ]
        response: SastResponse = await self._router.complete_structured(
            messages, SastResponse
        )
        findings: list[Finding] = []
        for raw in response.findings:
            severity = raw.severity if raw.severity in (
                "critical", "high", "medium", "low", "info"
            ) else "high"
            findings.append(Finding(
                review_id=review_id,
                agent_name=self.name,
                category="security_flaw",
                severity=severity,  # type: ignore[arg-type]
                file_path=raw.file_path,
                line_start=raw.line_start,
                line_end=raw.line_end,
                description=raw.description,
                confidence=raw.confidence,
            ))
        return findings
