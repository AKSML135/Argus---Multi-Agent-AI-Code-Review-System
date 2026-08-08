"""Shared FastAPI dependencies.

Extracted from app.py so all routers can import them without circular deps.
"""

from __future__ import annotations

from fastapi import Header, HTTPException, status

from argus.config import get_settings
from argus.llm.router import LLMRouter


def verify_api_key(x_api_key: str = Header(default="")) -> None:
    """Reject requests that don't carry the configured API key."""
    settings = get_settings()
    if x_api_key != settings.api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )


def get_llm_router() -> LLMRouter | None:
    """Return a real LLM router if API keys are configured, else None (for tests)."""
    settings = get_settings()
    if settings.groq_api_key or settings.gemini_api_key:
        try:
            return LLMRouter(settings=settings)
        except Exception:
            return None
    return None
