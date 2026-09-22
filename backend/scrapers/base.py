from abc import ABC, abstractmethod

from backend.models.car import MarketDiscoveryRequest, SearchRequest
from backend.models.listing import CarListing, SourceDiagnostics


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


class HttpAutomationLimitedError(SourceBlockedError):
    """Direct automated HTTP access was limited; this is not proof of an IP ban."""


class BrowserAccessLimitedError(SourceBlockedError):
    """An isolated browser also could not access the marketplace page."""


class RobotsRestrictedError(SourceBlockedError):
    """The marketplace explicitly restricts automated read-only access."""


class UnsupportedQueryError(ScraperError):
    """The source does not support the requested query shape."""


class ScraperParseError(ScraperError):
    """The marketplace response did not contain the required listing data."""


class BaseScraper(ABC):
    source: str
    diagnostics: dict[str, SourceDiagnostics]

    def reset_diagnostics(
        self, operation: str, resolved_url: str | None = None
    ) -> SourceDiagnostics:
        if not hasattr(self, "diagnostics"):
            self.diagnostics = {}
        value = SourceDiagnostics(resolved_url=resolved_url)
        self.diagnostics[operation] = value
        return value

    def combined_diagnostics(self) -> SourceDiagnostics:
        values = getattr(self, "diagnostics", {}).values()
        rejected: dict[str, int] = {}
        for item in values:
            for reason, count in item.rejected.items():
                rejected[reason] = rejected.get(reason, 0) + count
        return SourceDiagnostics(
            pages_scanned=sum(item.pages_scanned for item in values),
            raw_count=sum(item.raw_count for item in values),
            parsed_count=sum(item.parsed_count for item in values),
            accepted_count=sum(item.accepted_count for item in values),
            detail_requests=sum(item.detail_requests for item in values),
            browser_fallbacks=sum(item.browser_fallbacks for item in values),
            elapsed_seconds=max((item.elapsed_seconds for item in values), default=0),
            rejected=rejected,
            resolved_url=next((item.resolved_url for item in values if item.resolved_url), None),
            resolved_urls=[url for item in values for url in item.resolved_urls],
            degraded=any(item.degraded for item in values),
            notes=[note for item in values for note in item.notes],
        )

    @abstractmethod
    async def search(self, query: SearchRequest) -> list[CarListing]:
        raise NotImplementedError

    async def discover(self, query: MarketDiscoveryRequest) -> list[CarListing]:
        """Compatibility fallback for adapters/tests while sources migrate to broad search."""
        return await self.search(query.source_vehicle)
