"""Charlotte — goal-directed web navigation agent.

Published on PyPI as ``charlotte-crawler``. All public types, exceptions, and
streaming events are importable directly from this package:

    from charlotte import crawl, find_link, CrawlResult, CharlotteError, CrawlStarted
"""

__version__ = "1.1.0"

from charlotte.core.engine import crawl
from charlotte.core.find_link import find_link
from charlotte.exceptions import (
    AdapterOutputError,
    CharlotteConfigError,
    CharlotteError,
    CharlotteInternalError,
    CharlotteNetworkError,
    CharlotteRedirectError,
    CharlotteResponseTooLargeError,
    CharlotteSSRFError,
    CharlotteTimeoutError,
    RobotsError,
)
from charlotte.models import (
    BudgetExhausted,
    CrawlComplete,
    CrawlResult,
    CrawlStarted,
    GoalContext,
    LinkResult,
    ModelDecision,
    ModelEvaluating,
    PageFetched,
    PageSkipped,
    ResultFound,
    StreamEvent,
    TrustLevel,
    VisitLogEntry,
)

__all__ = [
    # Public functions
    "crawl",
    "find_link",
    # Result types
    "CrawlResult",
    "GoalContext",
    "LinkResult",
    "VisitLogEntry",
    # Streaming events
    "CrawlStarted",
    "PageFetched",
    "ModelEvaluating",
    "ModelDecision",
    "ResultFound",
    "PageSkipped",
    "BudgetExhausted",
    "CrawlComplete",
    "StreamEvent",
    # Trust level
    "TrustLevel",
    # Exceptions
    "CharlotteError",
    "CharlotteConfigError",
    "CharlotteNetworkError",
    "CharlotteTimeoutError",
    "CharlotteRedirectError",
    "CharlotteSSRFError",
    "CharlotteResponseTooLargeError",
    "RobotsError",
    "AdapterOutputError",
    "CharlotteInternalError",
    # Version
    "__version__",
]
