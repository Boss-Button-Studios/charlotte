"""Unit tests for CharlotteConfig environment variable handling (CHAR-002)."""

import pytest

from charlotte.config import CharlotteConfig


# ---------------------------------------------------------------------------
# default_adapter
# ---------------------------------------------------------------------------

def test_default_adapter_default(monkeypatch):
    monkeypatch.delenv("CHARLOTTE_DEFAULT_ADAPTER", raising=False)
    assert CharlotteConfig.default_adapter() == "groq"


def test_default_adapter_groq(monkeypatch):
    monkeypatch.setenv("CHARLOTTE_DEFAULT_ADAPTER", "groq")
    assert CharlotteConfig.default_adapter() == "groq"


def test_default_adapter_local(monkeypatch):
    monkeypatch.setenv("CHARLOTTE_DEFAULT_ADAPTER", "local")
    assert CharlotteConfig.default_adapter() == "local"


def test_default_adapter_case_insensitive(monkeypatch):
    monkeypatch.setenv("CHARLOTTE_DEFAULT_ADAPTER", "LOCAL")
    assert CharlotteConfig.default_adapter() == "local"


def test_default_adapter_invalid_falls_back_to_groq(monkeypatch):
    # A typo should not crash a caller — it silently uses the safe default.
    monkeypatch.setenv("CHARLOTTE_DEFAULT_ADAPTER", "openai")
    assert CharlotteConfig.default_adapter() == "groq"


# ---------------------------------------------------------------------------
# local_base_url
# ---------------------------------------------------------------------------

def test_local_base_url_default(monkeypatch):
    monkeypatch.delenv("CHARLOTTE_LOCAL_BASE_URL", raising=False)
    assert CharlotteConfig.local_base_url() == "http://localhost:11434"


def test_local_base_url_env(monkeypatch):
    monkeypatch.setenv("CHARLOTTE_LOCAL_BASE_URL", "http://localhost:1234")
    assert CharlotteConfig.local_base_url() == "http://localhost:1234"


# ---------------------------------------------------------------------------
# local_model
# ---------------------------------------------------------------------------

def test_local_model_default(monkeypatch):
    monkeypatch.delenv("CHARLOTTE_LOCAL_MODEL", raising=False)
    assert CharlotteConfig.local_model() == "llama3:8b"


def test_local_model_env(monkeypatch):
    monkeypatch.setenv("CHARLOTTE_LOCAL_MODEL", "phi3:mini")
    assert CharlotteConfig.local_model() == "phi3:mini"


# ---------------------------------------------------------------------------
# stream
# ---------------------------------------------------------------------------

def test_stream_default(monkeypatch):
    monkeypatch.delenv("CHARLOTTE_STREAM", raising=False)
    assert CharlotteConfig.stream() is True


def test_stream_true(monkeypatch):
    monkeypatch.setenv("CHARLOTTE_STREAM", "true")
    assert CharlotteConfig.stream() is True


def test_stream_false(monkeypatch):
    monkeypatch.setenv("CHARLOTTE_STREAM", "false")
    assert CharlotteConfig.stream() is False


def test_stream_case_insensitive(monkeypatch):
    monkeypatch.setenv("CHARLOTTE_STREAM", "FALSE")
    assert CharlotteConfig.stream() is False


def test_stream_invalid_uses_default(monkeypatch):
    # A typo like "yes" or "1" must not flip the default.
    monkeypatch.setenv("CHARLOTTE_STREAM", "yes")
    assert CharlotteConfig.stream() is True


# ---------------------------------------------------------------------------
# respect_robots
# ---------------------------------------------------------------------------

def test_respect_robots_default(monkeypatch):
    monkeypatch.delenv("CHARLOTTE_RESPECT_ROBOTS", raising=False)
    assert CharlotteConfig.respect_robots() is True


def test_respect_robots_false(monkeypatch):
    monkeypatch.setenv("CHARLOTTE_RESPECT_ROBOTS", "false")
    assert CharlotteConfig.respect_robots() is False


def test_respect_robots_invalid_uses_default(monkeypatch):
    monkeypatch.setenv("CHARLOTTE_RESPECT_ROBOTS", "0")
    assert CharlotteConfig.respect_robots() is True


# ---------------------------------------------------------------------------
# groq_api_key
# ---------------------------------------------------------------------------

def test_groq_api_key_none_when_unset(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    assert CharlotteConfig.groq_api_key() is None


def test_groq_api_key_returns_value(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test_key_12345")
    assert CharlotteConfig.groq_api_key() == "gsk_test_key_12345"


def test_groq_api_key_empty_string_is_none(monkeypatch):
    # An empty string is treated as "not set" — prevents accidental empty-key usage.
    monkeypatch.setenv("GROQ_API_KEY", "")
    assert CharlotteConfig.groq_api_key() is None
