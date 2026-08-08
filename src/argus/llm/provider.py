"""LLM provider interface and implementations.

Agents never import Groq/Gemini SDKs directly — they go through LLMProvider.
This makes provider-level fallback, rate limiting, and test mocking a single
concern in router.py, not scattered across every agent.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    """Typed base for all LLM gateway errors."""


class LLMProviderError(LLMError):
    """Error from the upstream provider (HTTP, auth, rate limit, etc.)."""

    def __init__(self, provider: str, message: str, retryable: bool = True):
        super().__init__(f"[{provider}] {message}")
        self.provider = provider
        self.retryable = retryable


class LLMOutputError(LLMError):
    """Provider returned a response that failed schema validation."""

    def __init__(self, provider: str, raw: str, schema_name: str):
        super().__init__(f"[{provider}] Output failed {schema_name} validation")
        self.provider = provider
        self.raw = raw
        self.schema_name = schema_name


class LLMProvider(ABC):
    """Common interface every provider must implement."""

    name: str = "base"

    @abstractmethod
    async def complete(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        """Return the assistant text response."""

    async def complete_structured(
        self,
        messages: list[dict[str, str]],
        schema: type[T],
        **kwargs: Any,
    ) -> T:
        """Return a Pydantic-validated structured response.

        Appends a JSON-format instruction to the last user message and
        validates the response against `schema`.  Raises `LLMOutputError`
        on validation failure — never returns None.
        """
        # Augment system prompt to request JSON
        augmented = list(messages)
        json_instruction = (
            f"\n\nRespond with ONLY a JSON object matching this schema (no markdown, "
            f"no extra text):\n{json.dumps(schema.model_json_schema(), indent=2)}"
        )
        # Append to last user message
        for i in reversed(range(len(augmented))):
            if augmented[i]["role"] == "user":
                augmented[i] = {
                    **augmented[i],
                    "content": augmented[i]["content"] + json_instruction,
                }
                break

        raw = await self.complete(augmented, **kwargs)
        # Strip markdown fences if present
        clean = raw.strip()
        if clean.startswith("```"):
            clean = "\n".join(clean.split("\n")[1:])
            clean = clean.rsplit("```", 1)[0].strip()

        try:
            return schema.model_validate_json(clean)
        except Exception as exc:
            raise LLMOutputError(self.name, raw, schema.__name__) from exc


class GroqProvider(LLMProvider):
    """Groq provider using the official SDK."""

    name = "groq"
    DEFAULT_MODEL = "llama3-8b-8192"

    def __init__(self, api_key: str, model: str | None = None):
        try:
            from groq import AsyncGroq
        except ImportError as e:
            raise ImportError("groq package is required: pip install groq") from e
        self._client = AsyncGroq(api_key=api_key)
        self._model = model or self.DEFAULT_MODEL

    async def complete(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=messages,  # type: ignore[arg-type]
                **kwargs,
            )
            return response.choices[0].message.content or ""
        except Exception as exc:
            raise LLMProviderError(self.name, str(exc)) from exc


class GeminiProvider(LLMProvider):
    """Gemini provider using the google-generativeai SDK."""

    name = "gemini"
    DEFAULT_MODEL = "gemini-1.5-flash"

    def __init__(self, api_key: str, model: str | None = None):
        try:
            import google.generativeai as genai
        except ImportError as e:
            raise ImportError(
                "google-generativeai package is required"
            ) from e
        genai.configure(api_key=api_key)
        self._genai = genai
        self._model_name = model or self.DEFAULT_MODEL

    async def complete(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        import asyncio

        # Convert OpenAI-style messages to Gemini format
        # Gemini uses a flat prompt for simplicity in the gateway abstraction
        prompt = "\n".join(
            f"{m['role'].upper()}: {m['content']}" for m in messages
        )
        try:
            model = self._genai.GenerativeModel(self._model_name)
            # Run sync call in thread pool to keep async interface
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None, lambda: model.generate_content(prompt)
            )
            return response.text or ""
        except Exception as exc:
            raise LLMProviderError(self.name, str(exc)) from exc
