"""Tests for M4: BaseAgent protocol and registry."""

import pytest

from argus.agents import registry
from argus.agents.base import BaseAgent
from argus.guardrails.schemas import Finding


# ---------------------------------------------------------------------------
# Dummy agent implementations
# ---------------------------------------------------------------------------

class DummyAgent:
    """A minimal agent conforming to BaseAgent Protocol."""

    name = "dummy_agent"

    async def run(self, diff: str, review_id: str) -> list[Finding]:
        return []


class AnotherAgent:
    name = "another_agent"

    async def run(self, diff: str, review_id: str) -> list[Finding]:
        return []


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------

def test_dummy_agent_satisfies_protocol():
    assert isinstance(DummyAgent(), BaseAgent)


def test_class_without_run_does_not_satisfy_protocol():
    class BadAgent:
        name = "bad"

    assert not isinstance(BadAgent(), BaseAgent)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clear_registry():
    registry.clear()
    yield
    registry.clear()


def test_register_and_retrieve():
    agent = DummyAgent()
    registry.register(agent)
    retrieved = registry.get("dummy_agent")
    assert retrieved is agent


def test_register_second_agent_does_not_affect_first():
    a1 = DummyAgent()
    a2 = AnotherAgent()
    registry.register(a1)
    registry.register(a2)

    assert registry.get("dummy_agent") is a1
    assert registry.get("another_agent") is a2


def test_get_unregistered_raises():
    with pytest.raises(KeyError, match="not registered"):
        registry.get("nonexistent")


def test_all_agents_returns_snapshot():
    a1 = DummyAgent()
    a2 = AnotherAgent()
    registry.register(a1)
    registry.register(a2)

    all_a = registry.all_agents()
    assert "dummy_agent" in all_a
    assert "another_agent" in all_a
    # Snapshot — modifications don't affect registry
    all_a["dummy_agent"] = None  # type: ignore
    assert registry.get("dummy_agent") is a1


def test_register_is_decorator_friendly():
    @registry.register
    class DecoratedAgent:
        name = "decorated_agent"

        async def run(self, diff, review_id):
            return []

    assert registry.get("decorated_agent").name == "decorated_agent"
