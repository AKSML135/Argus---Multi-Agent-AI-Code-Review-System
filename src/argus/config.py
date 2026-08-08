"""Argus configuration — loaded from environment / .env file.

API keys are declared as Optional[str] so that Settings() can be
instantiated without a .env file. Components that actually need a key
raise at construction time, not at import time.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ARGUS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- LLM provider keys (optional at load time) ---
    groq_api_key: str | None = None
    gemini_api_key: str | None = None

    # --- Provider routing ---
    primary_provider: Literal["groq", "gemini"] = "groq"
    fallback_provider: Literal["groq", "gemini"] = "gemini"

    # --- Persistence paths ---
    db_path: str = "data/argus.db"
    checkpoints_db_path: str = "data/checkpoints.db"

    # --- Rate limiting (requests per minute per provider) ---
    rate_limit_rpm: int = 30

    # --- Retry ---
    max_retries: int = 3
    retry_base_delay: float = 1.0
    retry_max_delay: float = 30.0

    # --- Review limits ---
    max_diff_lines: int = 2000

    # --- Static analysis ---
    complexity_threshold: int = 10

    # --- API security ---
    api_key: str = "dev-secret-key"

    # --- Critic loop ---
    max_refine_iterations: int = 3
    max_report_iterations: int = 3

    @field_validator("db_path", "checkpoints_db_path", mode="before")
    @classmethod
    def ensure_parent_dirs(cls, v: str) -> str:
        Path(v).parent.mkdir(parents=True, exist_ok=True)
        return v

    def require_groq_key(self) -> str:
        if not self.groq_api_key:
            raise ValueError("GROQ_API_KEY is required but not set")
        return self.groq_api_key

    def require_gemini_key(self) -> str:
        if not self.gemini_api_key:
            raise ValueError("GEMINI_API_KEY is required but not set")
        return self.gemini_api_key


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
