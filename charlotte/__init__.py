"""Charlotte — goal-directed web navigation agent.

Published on PyPI as `charlotte-crawler`. Import the two public functions:

    from charlotte import crawl, find_link

All public types, exceptions, and streaming events are re-exported here so
callers need only import from `charlotte`, not from internal submodules.
"""

__version__ = "0.1.0"

# Result types
from charlotte.models import CrawlResult, LinkResult, VisitLogEntry

# Streaming event types — stable public API across minor versions
from charlotte.models import (
    CrawlStarted,
    PageFetched,
    ModelDecision,
    ResultFound,
    PageSkipped,
    BudgetExhausted,
    CrawlComplete,
    StreamEvent,
)

# Trust level
from charlotte.models import TrustLevel

# Exceptions — callers may need to catch these
from charlotte.exceptions import (
    CharlotteError,
    CharlotteConfigError,
    CharlotteNetworkError,
    CharlotteTimeoutError,
    CharlotteRedirectError,
    RobotsError,
    AdapterOutputError,
    CharlotteInternalError,
)

# crawl() and find_link() will be exported here after CHAR-013 and CHAR-014.
# __all__ is deferred until those functions exist.
