"""Tests for M2: LLM Gateway — router, rate limiter, fallback, structured output."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import BaseModel

from argus.llm.provider import (
    GeminiProvider,
    GroqProvider,
    LLMOutputError,
    LLMProvider,
    LLMProviderError,
)
from argus.llm.rate_limiter import RateLimitExceeded, RateLimiter, TokenBucket
from argus.llm.router import LLMError, LLMRouter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_mock_provider(name: str, response: str = "ok") -> LLMProvider:
    provider = MagicMock(spec=LLMProvider)
    provider.name = name
    provider.complete = AsyncMock(return_value=response)
    provider.complete_structured = AsyncMock()
    return provider


def make_failing_provider(name: str, retryable: bool = True) -> LLMProvider:
    provider = MagicMock(spec=LLMProvider)
    provider.name = name
    provider.complete = AsyncMock(
        side_effect=LLMProviderError(name, "Upstream error", retryable=retryable)
    )
    provider.complete_structured = AsyncMock(
        side_effect=LLMProviderError(name, "Upstream error", retryable=retryable)
    )
    return provider


# ---------------------------------------------------------------------------
# Router: fallback behaviour
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_router_uses_primary_when_available():
    primary = make_mock_provider("groq", "answer from groq")
    fallback = make_mock_provider("gemini", "answer from gemini")
    router = LLMRouter(primary, fallback, max_retries=1)

    result = await router.complete([{"role": "user", "content": "hi"}])
    assert result == "answer from groq"
    primary.complete.assert_called_once()
    fallback.complete.assert_not_called()


@pytest.mark.asyncio
async def test_router_falls_back_when_primary_fails():
    primary = make_failing_provider("groq", retryable=True)
    fallback = make_mock_provider("gemini", "fallback answer")
    router = LLMRouter(primary, fallback, max_retries=1)

    result = await router.complete([{"role": "user", "content": "hi"}])
    assert result == "fallback answer"
    primary.complete.assert_called()
    fallback.complete.assert_called_once()


@pytest.mark.asyncio
async def test_router_raises_when_both_providers_fail():
    primary = make_failing_provider("groq")
    fallback = make_failing_provider("gemini")
    router = LLMRouter(primary, fallback, max_retries=1)

    with pytest.raises(LLMError):
        await router.complete([{"role": "user", "content": "hi"}])


@pytest.mark.asyncio
async def test_router_no_fallback_raises_on_primary_failure():
    primary = make_failing_provider("groq")
    router = LLMRouter(primary, fallback=None, max_retries=1)

    with pytest.raises(LLMProviderError):
        await router.complete([{"role": "user", "content": "hi"}])


# ---------------------------------------------------------------------------
# Router: structured output
# ---------------------------------------------------------------------------

class EchoSchema(BaseModel):
    value: str


@pytest.mark.asyncio
async def test_structured_output_returns_validated_model():
    primary = make_mock_provider("groq")
    primary.complete_structured = AsyncMock(return_value=EchoSchema(value="hello"))
    router = LLMRouter(primary, max_retries=1)

    result = await router.complete_structured(
        [{"role": "user", "content": "q"}], EchoSchema
    )
    assert isinstance(result, EchoSchema)
    assert result.value == "hello"


@pytest.mark.asyncio
async def test_structured_output_raises_on_malformed_output():
    primary = make_mock_provider("groq")
    primary.complete_structured = AsyncMock(
        side_effect=LLMOutputError("groq", "garbage", "EchoSchema")
    )
    router = LLMRouter(primary, max_retries=1)

    with pytest.raises(LLMOutputError):
        await router.complete_structured(
            [{"role": "user", "content": "q"}], EchoSchema
        )


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rate_limiter_allows_within_budget():
    limiter = RateLimiter({"groq": 60})
    # Should not raise
    await limiter.acquire("groq", block=False)


@pytest.mark.asyncio
async def test_rate_limiter_rejects_when_bucket_empty():
    # Very small bucket: 1 RPM so first call drains it
    bucket = TokenBucket(provider="groq", rpm=1)
    bucket._tokens = 0.0  # manually drain
    with pytest.raises(RateLimitExceeded):
        await bucket.acquire(block=False)


@pytest.mark.asyncio
async def test_rate_limiter_rejects_via_router():
    """Router raises LLMProviderError when rate limit is exceeded."""
    primary = make_mock_provider("groq")
    limiter = RateLimiter({"groq": 1})
    # Drain the bucket
    limiter.get_bucket("groq")._tokens = 0.0

    router = LLMRouter(primary, rate_limiter=limiter, max_retries=1)
    with pytest.raises(LLMProviderError, match="Rate limit"):
        await router.complete([{"role": "user", "content": "hi"}])


# ---------------------------------------------------------------------------
# Retry / backoff
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_router_retries_before_failing(mocker):
    """Primary is called max_retries times before giving up."""
    primary = make_failing_provider("groq", retryable=True)
    # patch sleep so tests don't actually wait
    mocker.patch("asyncio.sleep", new_callable=AsyncMock)

    router = LLMRouter(primary, fallback=None, max_retries=2, base_delay=0.01)
    with pytest.raises(LLMProviderError):
        await router.complete([{"role": "user", "content": "hi"}])

    # tenacity calls the function max_retries times
    assert primary.complete.call_count == 2


# ---------------------------------------------------------------------------
# Provider: complete_structured base implementation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_provider_complete_structured_validates_json():
    """The base LLMProvider.complete_structured validates JSON correctly."""
    class FakeProvider(LLMProvider):
        name = "fake"
        async def complete(self, messages, **kwargs):
            return '{"value": "test"}'

    provider = FakeProvider()
    result = await provider.complete_structured(
        [{"role": "user", "content": "give me json"}], EchoSchema
    )
    assert result.value == "test"


@pytest.mark.asyncio
async def test_provider_complete_structured_raises_on_bad_json():
    class FakeProvider(LLMProvider):
        name = "fake"
        async def complete(self, messages, **kwargs):
            return "not json at all"

    provider = FakeProvider()
    with pytest.raises(LLMOutputError):
        await provider.complete_structured(
            [{"role": "user", "content": "q"}], EchoSchema
        )
