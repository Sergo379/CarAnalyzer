from abc import ABC, abstractmethod

from backend.models.car import MarketDiscoveryRequest, SearchRequest
from backend.models.listing import CarListing


class ScraperError(RuntimeError):
    """Base exception for an unavailable or invalid marketplace source."""


class CaptchaRequiredError(ScraperError):
    """The marketplace requires a manual anti-bot check."""


class AuthenticationRequiredError(ScraperError):
    """The marketplace redirected the request to authentication."""


class SourceBlockedError(ScraperError):
    """The marketplace rejected this network address or request."""


class Http429Error(SourceBlockedError):
    """The marketplace rate-limited this client."""


class Http403Error(SourceBlockedError):
    """The marketplace denied this request with HTTP 403."""


class RobotsRestrictedError(SourceBlockedError):
    """The marketplace explicitly restricts automated read-only access."""


class UnsupportedQueryError(ScraperError):
    """The source does not support the requested query shape."""


class ScraperParseError(ScraperError):
    """The marketplace response did not contain the required listing data."""


class BaseScraper(ABC):
    source: str

    @abstractmethod
    async def search(self, query: SearchRequest) -> list[CarListing]:
        raise NotImplementedError

    async def discover(self, query: MarketDiscoveryRequest) -> list[CarListing]:
        """Compatibility fallback for adapters/tests while sources migrate to broad search."""
        return await self.search(query.source_vehicle)
