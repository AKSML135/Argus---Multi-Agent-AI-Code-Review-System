"""LLM Router — primary provider with fallback + tenacity retry + rate limiting.

Agents call `LLMRouter.complete()` or `LLMRouter.complete_structured()`.
They never know which provider answered.
"""

from __future__ import annotations

import logging
from typing import Any, TypeVar

from pydantic import BaseModel
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from argus.llm.provider import LLMError, LLMOutputError, LLMProvider, LLMProviderError
from argus.llm.rate_limiter import RateLimiter, RateLimitExceeded

T = TypeVar("T", bound=BaseModel)
logger = logging.getLogger(__name__)


class LLMRouter:
    """Routes LLM calls through primary → fallback, with retry and rate limiting."""

    def __init__(
        self,
        primary: LLMProvider,
        fallback: LLMProvider | None = None,
        rate_limiter: RateLimiter | None = None,
        max_retries: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 30.0,
    ):
        self._primary = primary
        self._fallback = fallback
        self._rate_limiter = rate_limiter
        self._max_retries = max_retries
        self._base_delay = base_delay
        self._max_delay = max_delay

    def _make_retry(self, provider: LLMProvider):
        """Build a tenacity-decorated wrapper for a given provider's complete()."""

        @retry(
            retry=retry_if_exception_type(LLMProviderError),
            stop=stop_after_attempt(self._max_retries),
            wait=wait_exponential_jitter(
                initial=self._base_delay, max=self._max_delay
            ),
            reraise=True,
        )
        async def _with_retry(messages: list[dict], **kwargs) -> str:
            return await provider.complete(messages, **kwargs)

        return _with_retry

    async def _acquire_rate_limit(self, provider: LLMProvider) -> None:
        if self._rate_limiter:
            try:
                await self._rate_limiter.acquire(provider.name, block=False)
            except RateLimitExceeded:
                raise LLMProviderError(
                    provider.name,
                    "Rate limit exceeded",
                    retryable=False,
                ) from None

    async def complete(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        """Try primary, fall back to secondary on retryable error."""
        try:
            await self._acquire_rate_limit(self._primary)
            retry_fn = self._make_retry(self._primary)
            return await retry_fn(messages, **kwargs)
        except LLMProviderError as primary_exc:
            if not primary_exc.retryable or self._fallback is None:
                raise
            logger.warning(
                "Primary provider %s failed (%s), falling back to %s",
                self._primary.name,
                primary_exc,
                self._fallback.name,
            )

        try:
            await self._acquire_rate_limit(self._fallback)  # type: ignore[arg-type]
            retry_fn = self._make_retry(self._fallback)  # type: ignore[arg-type]
            return await retry_fn(messages, **kwargs)
        except LLMProviderError as fallback_exc:
            raise LLMError(
                f"Both providers failed. Primary: {self._primary.name}. "
                f"Fallback: {fallback_exc}"
            ) from fallback_exc

    async def complete_structured(
        self,
        messages: list[dict[str, str]],
        schema: type[T],
        **kwargs: Any,
    ) -> T:
        """Structured output — validated against Pydantic schema.

        Raises LLMOutputError if neither provider returns valid JSON.
        """
        try:
            await self._acquire_rate_limit(self._primary)
            return await self._primary.complete_structured(messages, schema, **kwargs)
        except LLMOutputError:
            raise  # schema errors are not retried via fallback
        except LLMProviderError as primary_exc:
            if not primary_exc.retryable or self._fallback is None:
                raise
            logger.warning(
                "Primary provider failed for structured call, falling back",
            )

        await self._acquire_rate_limit(self._fallback)  # type: ignore[arg-type]
        return await self._fallback.complete_structured(messages, schema, **kwargs)  # type: ignore[union-attr]
