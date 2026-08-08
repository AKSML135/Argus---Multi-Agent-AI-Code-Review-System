"""Tests for M1: Settings / config loading."""

import pytest

from argus.config import Settings


def test_defaults_load_without_env():
    """Settings() must succeed with no .env — API keys optional at load time."""
    s = Settings()
    assert s.max_diff_lines == 2000
    assert s.max_retries == 3
    assert s.primary_provider == "groq"
    assert s.fallback_provider == "gemini"


def test_missing_groq_key_raises_only_on_demand():
    s = Settings()
    s.groq_api_key = None
    with pytest.raises(ValueError, match="GROQ_API_KEY"):
        s.require_groq_key()


def test_missing_gemini_key_raises_only_on_demand():
    s = Settings()
    s.gemini_api_key = None
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        s.require_gemini_key()


def test_present_key_returned():
    s = Settings()
    s.groq_api_key = "test-key"
    assert s.require_groq_key() == "test-key"


def test_override_via_env(monkeypatch):
    monkeypatch.setenv("ARGUS_MAX_DIFF_LINES", "500")
    s = Settings()
    assert s.max_diff_lines == 500
