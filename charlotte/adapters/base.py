"""
Adapter Protocol — the contract every navigator model adapter must satisfy.

An adapter is any async callable that accepts page context and returns a raw
structured navigation decision. Charlotte ships GroqAdapter and LocalAdapter;
custom adapters can target any provider. Output schema validation is Charlotte's
responsibility — adapters are not required to validate their own output.
See spec §6.3.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class AdapterProtocol(Protocol):
    """Callable interface for navigator model adapters.

    Any object implementing async __call__ with this signature satisfies the
    contract. Adapters handle prompt construction, API communication, and
    returning a raw dict. The engine validates the dict before acting on it.
    See spec §6.3.
    """

    async def __call__(
        self,
        *,
        goal: str,
        navigation_hint: str | None,
        page_title: str,
        page_url: str,
        page_summary: str,
        available_links: list[dict[str, str]],
        visit_history: list[str],
        results_so_far: int,
        schema_hint: str | None = None,
    ) -> dict[str, object]:
        """Evaluate a page and return a raw navigation decision.

        Args:
            goal: Natural language navigation goal from the caller.
            navigation_hint: Optional refinement hint from the caller.
            page_title: Title of the current page.
            page_url: URL of the current page.
            page_summary: Extracted visible text from the sanitized page.
            available_links: List of {text, url} dicts for links on the page,
                filtered to allowed_domains.
            visit_history: Brief list of URLs already visited this crawl.
            results_so_far: Count of results found in this crawl so far.
            schema_hint: Optional schema reminder injected by the engine on a
                retry after a validation failure. Adapters should include this
                prominently in the prompt when present.

        Returns:
            Raw dict with keys: found, confidence, result_url,
            links_to_follow, reasoning, answer. Values are not validated by
            the adapter — the engine applies §6.5 validation before acting.
            The ``answer`` field is optional: a verbatim fact string for
            fact-extraction goals (phone, address, price, name), null for
            navigation goals. See spec §6.2, §6.5.
        """
        ...
