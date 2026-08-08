"""Token-bucket rate limiter per provider.

Each provider gets its own bucket refilled at a configured rate (RPM).
Calls that exceed the budget either wait (async sleep) or raise immediately
based on the `block` flag.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field


class RateLimitExceeded(Exception):
    """Raised when the token bucket is empty and blocking is disabled."""

    def __init__(self, provider: str):
        super().__init__(f"Rate limit exceeded for provider '{provider}'")
        self.provider = provider


@dataclass
class TokenBucket:
    """Simple token-bucket implementation."""

    provider: str
    rpm: int  # requests per minute
    _tokens: float = field(init=False)
    _last_refill: float = field(init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    def __post_init__(self) -> None:
        self._tokens = float(self.rpm)
        self._last_refill = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        refill = elapsed * (self.rpm / 60.0)
        self._tokens = min(float(self.rpm), self._tokens + refill)
        self._last_refill = now

    async def acquire(self, block: bool = True) -> None:
        """Consume one token, optionally waiting until one is available."""
        async with self._lock:
            self._refill()
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return
            if not block:
                raise RateLimitExceeded(self.provider)
            # Calculate wait time and sleep outside the lock
            wait = (1.0 - self._tokens) * 60.0 / self.rpm

        await asyncio.sleep(wait)
        async with self._lock:
            self._refill()
            self._tokens = max(0.0, self._tokens - 1.0)


class RateLimiter:
    """Registry of per-provider token buckets."""

    def __init__(self, rpm_per_provider: dict[str, int]):
        self._buckets: dict[str, TokenBucket] = {
            provider: TokenBucket(provider=provider, rpm=rpm)
            for provider, rpm in rpm_per_provider.items()
        }

    def get_bucket(self, provider: str) -> TokenBucket:
        if provider not in self._buckets:
            # Default to a permissive bucket for unknown providers
            self._buckets[provider] = TokenBucket(provider=provider, rpm=60)
        return self._buckets[provider]

    async def acquire(self, provider: str, block: bool = True) -> None:
        await self.get_bucket(provider).acquire(block=block)
