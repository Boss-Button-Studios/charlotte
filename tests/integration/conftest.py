"""
Shared helpers for Charlotte integration tests (CHAR-017, spec §19).

All integration tests run against the public crawl() / find_link() API with:
  - respect_robots=False  (unless the test is specifically about robots)
  - default_delay=0       (suppress polite delay for speed)
  - respx mocked HTTP     (no live network)
  - sequential model stub  (deterministic responses, no real LLM)
"""

from __future__ import annotations

from typing import Any


# 60 words — above the thin-content threshold (THIN_CONTENT_WORD_THRESHOLD = 50).
# Always used as the body text so confidence-spike plausibility never fires.
BODY = " ".join(["word"] * 60)


def page(body: str = BODY, links: list[tuple[str, str]] | None = None) -> str:
    """Build minimal HTML with optional anchor links (text, absolute-URL)."""
    anchors = "".join(f'<a href="{url}">{text}</a>' for text, url in (links or []))
    return f"<html><body><p>{body}</p>{anchors}</body></html>"


def nav(
    *,
    found: bool,
    confidence: float,
    result_url: str | None,
    links: list[str],
    reason: str = "navigation decision",
) -> dict[str, Any]:
    """Build a valid model response dict that passes schema validation."""
    return {
        "found": found,
        "confidence": confidence,
        "result_url": result_url,
        "links_to_follow": links,
        "reasoning": reason,
    }


def seq(*responses: "dict[str, Any] | BaseException"):
    """
    Return an async adapter callable that pops responses in order.

    Each item is either a dict (returned as-is) or an exception instance
    (raised when that response's slot is reached).
    """
    queue: list[dict[str, Any] | BaseException] = list(responses)

    async def _adapter(*, schema_hint: str | None = None, **_kw: Any) -> dict[str, Any]:
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    return _adapter


async def collect(gen: Any) -> list:
    """Drain an async generator into a list."""
    events: list = []
    async for event in gen:
        events.append(event)
    return events
