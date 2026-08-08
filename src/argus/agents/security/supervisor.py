"""Security Supervisor — runs SecretScanner then SAST in sequence.

The security subgraph is a self-contained unit: it fans in from the main
supervisor and fans out two security agents, then merges results. Structuring
it as a separate async function means the main graph can await it as a single
node — keeping the top-level graph clean.
"""

from __future__ import annotations

import asyncio

from argus.agents.security.sast_agent import SastAgent
from argus.agents.security.secret_scanner import SecretScannerAgent
from argus.guardrails.schemas import Finding


class SecuritySupervisor:
    """Orchestrates the security subgraph: SecretScanner (deterministic) then SAST (LLM)."""

    name = "security_supervisor"

    def __init__(self, router=None):
        self._secret_scanner = SecretScannerAgent()
        self._sast = SastAgent(router=router)

    async def run(self, diff: str, review_id: str) -> list[Finding]:
        """Run secret scanner and SAST concurrently, merge findings."""
        secret_task = asyncio.create_task(
            self._secret_scanner.run(diff, review_id)
        )
        sast_task = asyncio.create_task(
            self._sast.run(diff, review_id)
        )
        secret_findings, sast_findings = await asyncio.gather(
            secret_task, sast_task
        )
        return list(secret_findings) + list(sast_findings)
