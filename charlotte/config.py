"""
Environment variable configuration for Charlotte.

Reads all CHARLOTTE_* environment variables with documented defaults.
Precedence (highest to lowest): direct parameter → environment variable → default.
This module implements the environment variable layer only. The crawl() and
find_link() functions apply caller-supplied parameters on top. See spec §6.3.
"""

import os

# Sent as the User-Agent header in every outbound HTTP request made by Charlotte
# (page fetches and robots.txt checks). One value shared across both HTTP clients
# so spec §11 user-agent matching and server-side logs stay consistent.
HTTP_USER_AGENT: str = "CareNavigator/0.1"


def _bool_env(key: str, default: bool) -> bool:
    """Parse a CHARLOTTE_* boolean environment variable.

    Accepts "true" or "false" (case-insensitive). Any other value — including
    an empty string or a missing variable — returns the default. This is
    intentionally strict: a typo in the env var should not silently flip behavior.
    """
    val = os.environ.get(key, "").strip().lower()
    if val == "true":
        return True
    if val == "false":
        return False
    return default


class CharlotteConfig:
    """Reads CHARLOTTE_* environment variables with documented defaults.

    All methods are static — there is no instance state. Call them at the point
    where the default is needed, not at import time, so that tests can monkeypatch
    environment variables reliably.
    """

    @staticmethod
    def default_adapter() -> str:
        """Return 'groq' or 'local'. Selects the shipped default adapter.

        Invalid values (anything other than 'groq' or 'local') fall back to
        'groq' rather than raising — a misconfigured env var should not crash
        a caller that intended to rely on the default.
        """
        val = os.environ.get("CHARLOTTE_DEFAULT_ADAPTER", "groq").strip().lower()
        if val not in ("groq", "local"):
            return "groq"
        return val

    @staticmethod
    def local_base_url() -> str:
        """Base URL for the LocalAdapter. Default: http://localhost:11434 (Ollama)."""
        return os.environ.get("CHARLOTTE_LOCAL_BASE_URL", "http://localhost:11434").strip()

    @staticmethod
    def local_model() -> str:
        """Model name for the LocalAdapter. Default: deepseek-r1:14b."""
        return os.environ.get("CHARLOTTE_LOCAL_MODEL", "deepseek-r1:14b").strip()

    @staticmethod
    def stream() -> bool:
        """Whether to stream navigation events by default. Default: True."""
        return _bool_env("CHARLOTTE_STREAM", True)

    @staticmethod
    def respect_robots() -> bool:
        """Whether to respect robots.txt by default. Default: True."""
        return _bool_env("CHARLOTTE_RESPECT_ROBOTS", True)

    @staticmethod
    def groq_api_key() -> str | None:
        """Groq API key from GROQ_API_KEY. Returns None if not set or empty."""
        return os.environ.get("GROQ_API_KEY") or None
