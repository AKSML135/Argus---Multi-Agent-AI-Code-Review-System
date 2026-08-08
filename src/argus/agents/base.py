"""BaseAgent Protocol — the contract every worker agent must implement.

Using `Protocol` (structural subtyping) means:
- Agents don't need to inherit from a base class
- The registry works with any callable that matches the signature
- Testing is trivially easy (pass any mock that has `run`)
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from argus.guardrails.schemas import Finding


@runtime_checkable
class BaseAgent(Protocol):
    """Every worker agent must expose this interface."""

    name: str  # class-level agent identifier

    async def run(self, diff: str, review_id: str) -> list[Finding]:
        """Analyze `diff` and return zero or more findings.

        Args:
            diff: Unified diff of the changes under review.
            review_id: ID of the parent Review row — used for Finding.review_id.

        Returns:
            A (possibly empty) list of validated Finding objects.
        """
        ...
