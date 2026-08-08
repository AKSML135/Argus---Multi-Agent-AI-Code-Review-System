"""Plugin-style agent registry.

Agents self-register by calling `register()`.  The supervisor queries the
registry to build its fan-out list — no agent name is ever hardcoded in the
graph assembly code.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from argus.agents.base import BaseAgent

_registry: dict[str, BaseAgent] = {}


def register(agent: BaseAgent) -> BaseAgent:
    """Register an agent instance.  Returns the agent (decorator-friendly)."""
    _registry[agent.name] = agent
    return agent


def get(name: str) -> BaseAgent:
    """Retrieve a registered agent by name.  Raises KeyError if not found."""
    if name not in _registry:
        raise KeyError(f"Agent '{name}' is not registered. Available: {list(_registry)}")
    return _registry[name]


def all_agents() -> dict[str, BaseAgent]:
    """Return a snapshot of all registered agents."""
    return dict(_registry)


def clear() -> None:
    """Clear the registry (useful in tests)."""
    _registry.clear()
