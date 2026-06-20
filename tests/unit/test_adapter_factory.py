"""Tests for scripts/adapter_factory — the field-suite adapter selector.

adapter_factory lives in scripts/ (run as `python3 scripts/<suite>.py`), so it is not
importable as a package. Add scripts/ to the path the same way the scripts do, then
exercise the pure, untrusted-input-facing helpers: env_float and the provider switch.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import adapter_factory  # noqa: E402  (path is set up just above)
from charlotte.exceptions import CharlotteConfigError  # noqa: E402


def test_env_float_returns_default_when_unset(monkeypatch):
    monkeypatch.delenv("CHARLOTTE_X", raising=False)
    assert adapter_factory.env_float("CHARLOTTE_X", 2.0) == 2.0
    assert adapter_factory.env_float("CHARLOTTE_X", None) is None


def test_env_float_treats_blank_as_unset(monkeypatch):
    monkeypatch.setenv("CHARLOTTE_X", "   ")
    assert adapter_factory.env_float("CHARLOTTE_X", 3.0) == 3.0


def test_env_float_parses_a_valid_number(monkeypatch):
    monkeypatch.setenv("CHARLOTTE_X", "12.5")
    assert adapter_factory.env_float("CHARLOTTE_X", 1.0) == 12.5


def test_env_float_rejects_malformed_input_with_named_error(monkeypatch):
    """Malformed operator input must fail as a named CharlotteConfigError naming the
    variable — never a raw ValueError that bypasses the project's error contract."""
    monkeypatch.setenv("CHARLOTTE_X", "ten")
    with pytest.raises(CharlotteConfigError, match="CHARLOTTE_X"):
        adapter_factory.env_float("CHARLOTTE_X", 1.0)


def test_build_adapter_rejects_unknown_provider_with_named_error(monkeypatch):
    """An unknown CHARLOTTE_ADAPTER is a configuration error, not a raw ValueError."""
    monkeypatch.setenv("CHARLOTTE_ADAPTER", "openai")
    with pytest.raises(CharlotteConfigError, match="CHARLOTTE_ADAPTER"):
        adapter_factory.build_adapter()
