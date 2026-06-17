"""Charlotte — goal-directed web navigation agent.

Published on PyPI as ``charlotte-crawler``. All public types, exceptions, and
streaming events are importable directly from this package:

    from charlotte import crawl, find_link, CrawlResult, CharlotteError, CrawlStarted
"""

__version__ = "1.2.0"

from charlotte.core.candidate_extractor import (
    CandidateExtractorProtocol,
    DefaultCandidateExtractor,
)
from charlotte.core.engine import crawl
from charlotte.core.find_link import find_link
from charlotte.core.goal_context_cache import AutoPreprocessor
from charlotte.core.goal_preprocessor import DeterministicPreprocessor, HybridPreprocessor
from charlotte.exceptions import (
    AdapterOutputError,
    CharlotteChallengeError,
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
    Candidate,
    CandidatesExtracted,
    CrawlComplete,
    CrawlResult,
    CrawlStarted,
    DestinationVerificationFailed,
    FailureMode,
    GoalContext,
    GoalPreprocessed,
    LinkResult,
    LinksRanked,
    ModelDecision,
    ModelEvaluating,
    ModelSkipped,
    PageFetched,
    PageSkipped,
    RankedLink,
    ResultContent,
    ResultContentMetadata,
    ResultFound,
    StreamEvent,
    TrustLevel,
    VerificationResult,
    VisitLogEntry,
)

__all__ = [
    # Public functions
    "crawl",
    "find_link",
    # Preprocessors
    "AutoPreprocessor",
    "DeterministicPreprocessor",
    "HybridPreprocessor",
    # Candidate extractor
    "CandidateExtractorProtocol",
    "DefaultCandidateExtractor",
    # Result types
    "CrawlResult",
    "GoalContext",
    "LinkResult",
    "VisitLogEntry",
    # v2 Phase C data types
    "Candidate",
    "FailureMode",
    "RankedLink",
    "ResultContent",
    "ResultContentMetadata",
    "VerificationResult",
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
    # v2 Phase C streaming events
    "CandidatesExtracted",
    "DestinationVerificationFailed",
    "GoalPreprocessed",
    "LinksRanked",
    "ModelSkipped",
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
    "CharlotteChallengeError",
    "AdapterOutputError",
    "CharlotteInternalError",
    # Version
    "__version__",
]
