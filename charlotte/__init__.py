"""Charlotte — goal-directed web navigation agent.

Published on PyPI as `charlotte-crawler`. Import the two public functions:

    from charlotte import crawl, find_link

All public types, exceptions, and streaming events are re-exported here so
callers need only import from `charlotte`, not from internal submodules.
"""

__version__ = "0.1.0"

from charlotte.exceptions import (
    AdapterOutputError,
    CharlotteConfigError,
    CharlotteError,
    CharlotteInternalError,
    CharlotteNetworkError,
    CharlotteRedirectError,
    CharlotteTimeoutError,
    RobotsError,
)
from charlotte.models import (
    BudgetExhausted,
    CrawlComplete,
    CrawlResult,
    CrawlStarted,
    LinkResult,
    ModelDecision,
    PageFetched,
    PageSkipped,
    ResultFound,
    StreamEvent,
    TrustLevel,
    VisitLogEntry,
)

# crawl() and find_link() will be added here after CHAR-013 and CHAR-014.

__all__ = [
    # Public functions (coming in CHAR-013/014)
    # "crawl",
    # "find_link",
    # Result types
    "CrawlResult",
    "LinkResult",
    "VisitLogEntry",
    # Streaming events
    "CrawlStarted",
    "PageFetched",
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
    "RobotsError",
    "AdapterOutputError",
    "CharlotteInternalError",
    # Version
    "__version__",
]
