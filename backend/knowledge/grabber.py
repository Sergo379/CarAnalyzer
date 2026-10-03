"""Conservative, bounded technical-source discovery and document loading."""

import asyncio
import ipaddress
import re
from dataclasses import dataclass, replace
from time import monotonic
from typing import Protocol
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import httpx

from backend.knowledge.identity import KnowledgeIdentity, normalize_display_text
from backend.knowledge.processing import CleanDocument, process_html, process_pdf


@dataclass(frozen=True, slots=True)
class SourceCandidate:
    url: str
    title: str
    source_type: str
    identity: KnowledgeIdentity
    discovery_provider: str = "unknown"
    discovery_query: str = ""
    snippet: str = ""


@dataclass(frozen=True, slots=True)
class DiscoveryAttempt:
    provider: str
    query: str
    status: str
    result_count: int = 0
    error_type: str | None = None
    error_message: str = ""
    language: str = ""


@dataclass(frozen=True, slots=True)
class ProviderDiscoveryResult:
    candidates: list[SourceCandidate]
    attempts: list[DiscoveryAttempt]


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    candidates: list[SourceCandidate]
    attempts: list[DiscoveryAttempt]


@dataclass(frozen=True, slots=True)
class LoadedSource:
    candidate: SourceCandidate
    document: CleanDocument | None
    status: str


class DiscoveryProvider(Protocol):
    name: str

    async def discover(
        self, identity: KnowledgeIdentity, client: httpx.AsyncClient, limit: int
    ) -> ProviderDiscoveryResult | list[SourceCandidate]: ...


_TRACKING_PARAMETERS = {"fbclid", "gclid", "mc_cid", "mc_eid"}

_TECHNICAL_INTENTS: tuple[tuple[str, str, str], ...] = (
    ("common problems known issues reliability", "common_problems", "en"),
    ("типичные проблемы надежность неисправности", "common_problems", "ru"),
    ("body corrosion water ingress seals buying inspection", "body", "en"),
    ("подвеска рулевое стуки износ осмотр", "suspension_steering", "ru"),
    ("engine cold start noise cooling leaks", "engine_cooling", "en"),
    ("трансмиссия привод рывки вибрации тест драйв", "drivetrain", "ru"),
    ("brakes electronics infotainment recurring faults", "brakes_electronics", "en"),
    ("регламент ТО интервалы обслуживания расходники история", "maintenance", "ru"),
    ("service bulletin recall maintenance schedule", "recall", "en"),
    ("проверка перед покупкой форум владельцев", "inspection_forum", "ru"),
)


class ProviderSearchError(RuntimeError):
    def __init__(self, status: str, error_type: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.error_type = error_type


def concise_error_message(exc: BaseException) -> str:
    message = normalize_display_text(str(exc)) or type(exc).__name__
    message = re.sub(r"https?://([^/?#\s]+)[^\s]*", r"https://\1/…", message)
    message = re.sub(r"(?i)(bearer|api[_ -]?key|token)\s*[=:]\s*\S+", r"\1=[redacted]", message)
    return message[:300]


def technical_search_queries(
    identity: KnowledgeIdentity, max_queries: int = 8
) -> list[tuple[str, str, str]]:
    """Build generic multilingual technical queries from canonical display metadata."""
    if max_queries <= 0:
        return []
    vehicle = " ".join(
        f'"{normalize_display_text(value)}"'
        for value in (identity.brand, identity.model, identity.generation, identity.engine)
        if value and value.strip()
    )
    if not vehicle:
        vehicle = f'"{identity.canonical_brand_id}" "{identity.canonical_model_id}"'
    return [
        (f"{vehicle} {terms}", intent, language)
        for terms, intent, language in _TECHNICAL_INTENTS[:max_queries]
    ]


def classify_source(url: str, title: str, intent: str, brand: str = "") -> str:
    text = f"{url} {title}".casefold()
    if any(word in text for word in ("forum", "ownersclub", "owners-forum", "форум", "drive2")):
        return "owner_forum"
    if intent == "recall" or any(word in text for word in ("recall", "bulletin", "отзыв")):
        return "recall"
    host = urlsplit(url).hostname or ""
    compact_brand = re.sub(r"\W", "", brand.casefold())
    compact_host = re.sub(r"\W", "", host.casefold())
    if (
        compact_brand
        and compact_brand in compact_host
        and any(word in text for word in ("manual", "service", "support", "техобслуж"))
    ):
        return "official"
    if any(word in text for word in ("repair", "service", "workshop", "ремонт", "сервис")):
        return "repair"
    return "technical_article"


class WebSearchDiscovery:
    """Bounded no-key metasearch adapter. DDGS can be replaced behind this interface."""

    name = "ddgs_web_search"

    def __init__(
        self,
        *,
        max_queries: int = 10,
        results_per_query: int = 3,
        query_timeout_seconds: float = 8.0,
        total_timeout_seconds: float = 28.0,
        backend: str = "auto",
        name: str = "ddgs_web_search",
    ) -> None:
        self.name = name
        self.max_queries = max_queries
        self.results_per_query = results_per_query
        self.query_timeout_seconds = query_timeout_seconds
        self.total_timeout_seconds = total_timeout_seconds
        self.backend = backend

    @staticmethod
    def _search(
        query: str, language: str, limit: int, backend: str, timeout_seconds: float
    ) -> list[dict[str, str]]:
        try:
            from ddgs import DDGS
            from ddgs.exceptions import DDGSException, RatelimitException, TimeoutException
        except ImportError as exc:
            raise ProviderSearchError(
                "dependency_missing", type(exc).__name__, "The ddgs package is not installed"
            ) from exc
        region = "ru-ru" if language == "ru" else "us-en"
        try:
            return list(
                DDGS(timeout=max(1, int(timeout_seconds))).text(
                    query,
                    region=region,
                    safesearch="moderate",
                    max_results=limit,
                    backend=backend,
                )
            )
        except RatelimitException as exc:
            raise ProviderSearchError(
                "rate_limited", type(exc).__name__, concise_error_message(exc)
            ) from exc
        except TimeoutException as exc:
            raise ProviderSearchError(
                "timeout", type(exc).__name__, concise_error_message(exc)
            ) from exc
        except DDGSException as exc:
            message = concise_error_message(exc)
            folded = message.casefold()
            if "no results" in folded:
                return []
            status = (
                "rate_limited"
                if "429" in folded or "rate" in folded
                else "blocked"
                if "403" in folded or "blocked" in folded
                else "provider_unavailable"
            )
            raise ProviderSearchError(status, type(exc).__name__, message) from exc
        except OSError as exc:
            raise ProviderSearchError(
                "network_error", type(exc).__name__, concise_error_message(exc)
            ) from exc

    async def discover(
        self, identity: KnowledgeIdentity, client: httpx.AsyncClient, limit: int
    ) -> ProviderDiscoveryResult:
        del client
        candidates: list[SourceCandidate] = []
        attempts: list[DiscoveryAttempt] = []
        deadline = monotonic() + self.total_timeout_seconds
        queries = technical_search_queries(identity, self.max_queries)
        per_query_limit = min(
            self.results_per_query,
            max(1, limit // max(len(queries), 1)),
        )
        for query, intent, language in queries:
            if len(candidates) >= limit:
                break
            remaining = deadline - monotonic()
            if remaining <= 0:
                attempts.append(
                    DiscoveryAttempt(
                        self.name,
                        query,
                        "timeout",
                        error_type="ProviderBudgetTimeout",
                        error_message="Web-search provider time budget was exhausted",
                        language=language,
                    )
                )
                break
            result_limit = min(per_query_limit, limit - len(candidates))
            query_timeout = min(self.query_timeout_seconds, remaining)
            try:
                rows = await asyncio.wait_for(
                    asyncio.to_thread(
                        self._search,
                        query,
                        language,
                        result_limit,
                        self.backend,
                        query_timeout,
                    ),
                    timeout=query_timeout,
                )
            except TimeoutError:
                attempts.append(
                    DiscoveryAttempt(
                        self.name,
                        query,
                        "timeout",
                        error_type="QueryTimeout",
                        error_message=f"Query exceeded {query_timeout:.1f}s timeout",
                        language=language,
                    )
                )
                continue
            except ProviderSearchError as exc:
                attempts.append(
                    DiscoveryAttempt(
                        self.name,
                        query,
                        exc.status,
                        error_type=exc.error_type,
                        error_message=concise_error_message(exc),
                        language=language,
                    )
                )
                if exc.status == "dependency_missing":
                    break
                continue
            except Exception as exc:
                status = "network_error" if isinstance(exc, OSError) else "provider_unavailable"
                attempts.append(
                    DiscoveryAttempt(
                        self.name,
                        query,
                        status,
                        error_type=type(exc).__name__,
                        error_message=concise_error_message(exc),
                        language=language,
                    )
                )
                continue
            accepted = 0
            for row in rows:
                url = str(row.get("href") or row.get("url") or "")
                title = normalize_display_text(row.get("title") or "")
                snippet = normalize_display_text(row.get("body") or row.get("snippet") or "")
                if not url:
                    continue
                candidates.append(
                    SourceCandidate(
                        url,
                        title,
                        classify_source(url, title, intent, identity.brand),
                        identity,
                        self.name,
                        query,
                        snippet,
                    )
                )
                accepted += 1
            attempts.append(
                DiscoveryAttempt(
                    self.name,
                    query,
                    "success" if accepted else "zero_results",
                    accepted,
                    language=language,
                )
            )
        return ProviderDiscoveryResult(candidates, attempts)


def canonical_url(url: str) -> str:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").casefold()
    if parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
        raise ValueError("Only public HTTP(S) source URLs are supported")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if host == "localhost" or host.endswith(".localhost") or "." not in host:
            raise ValueError("Local source URLs are not supported") from None
    else:
        if not address.is_global:
            raise ValueError("Private network source URLs are not supported")
    port = f":{parsed.port}" if parsed.port and parsed.port not in (80, 443) else ""
    path = parsed.path or "/"
    parameters = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.casefold() not in _TRACKING_PARAMETERS and not key.casefold().startswith("utm_")
    ]
    return urlunsplit((parsed.scheme.casefold(), host + port, path, urlencode(parameters), ""))


class WikipediaDiscovery:
    """Public MediaWiki API; discovered pages are conservatively model-wide."""

    name = "mediawiki"

    def __init__(self, languages: tuple[str, ...] = ("en", "ru")) -> None:
        self.languages = languages

    async def discover(
        self, identity: KnowledgeIdentity, client: httpx.AsyncClient, limit: int
    ) -> ProviderDiscoveryResult:
        results: list[SourceCandidate] = []
        attempts: list[DiscoveryAttempt] = []
        model_identity = identity.fallback()[-1]
        query = f'"{identity.brand}" "{identity.model}" automobile'
        for language in self.languages:
            if len(results) >= limit:
                break
            before = len(results)
            endpoint = f"https://{language}.wikipedia.org/w/api.php"
            direct_query = f"{identity.brand} {identity.model}".strip()
            try:
                direct = await client.get(
                    endpoint,
                    params={
                        "action": "query",
                        "titles": direct_query,
                        "redirects": 1,
                        "format": "json",
                        "formatversion": 2,
                    },
                )
                direct.raise_for_status()
                pages = direct.json().get("query", {}).get("pages", [])
                for page in pages:
                    title = str(page.get("title", ""))
                    compact_title = re.sub(r"\W", "", title.casefold())
                    compact_identity = re.sub(
                        r"\W", "", f"{identity.brand}{identity.model}".casefold()
                    )
                    if "missing" not in page and compact_title == compact_identity:
                        results.append(
                            SourceCandidate(
                                f"https://{language}.wikipedia.org/wiki/"
                                f"{quote(title.replace(' ', '_'))}",
                                title,
                                "encyclopedia",
                                model_identity,
                                self.name,
                                direct_query,
                            )
                        )
                        break
                attempts.append(
                    DiscoveryAttempt(
                        self.name,
                        direct_query,
                        "success" if len(results) > before else "zero_results",
                        len(results) - before,
                        language=language,
                    )
                )
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                attempts.append(
                    DiscoveryAttempt(
                        self.name,
                        direct_query,
                        (
                            "network_error"
                            if isinstance(exc, httpx.HTTPError)
                            else "provider_unavailable"
                        ),
                        error_type=type(exc).__name__,
                        error_message=concise_error_message(exc),
                        language=language,
                    )
                )
            if len(results) > before:
                continue
            try:
                response = await client.get(
                    endpoint,
                    params={
                        "action": "query",
                        "list": "search",
                        "srsearch": query,
                        "srlimit": min(10, max(5, limit * 3)),
                        "format": "json",
                    },
                )
                response.raise_for_status()
                items = response.json().get("query", {}).get("search", [])
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                attempts.append(
                    DiscoveryAttempt(
                        self.name,
                        query,
                        (
                            "network_error"
                            if isinstance(exc, httpx.HTTPError)
                            else "provider_unavailable"
                        ),
                        error_type=type(exc).__name__,
                        error_message=concise_error_message(exc),
                        language=language,
                    )
                )
                continue
            accepted = 0
            for item in items:
                title = str(item.get("title", ""))
                folded = title.casefold()
                compact_title = re.sub(r"\W", "", folded)
                compact_brand = re.sub(r"\W", "", identity.brand.casefold())
                compact_model = re.sub(r"\W", "", identity.model.casefold())
                # A page for a specific generation is not model-wide evidence.
                if not title or compact_title != compact_brand + compact_model:
                    continue
                # A search hit is only a candidate, never evidence on its own.
                results.append(
                    SourceCandidate(
                        f"https://{language}.wikipedia.org/wiki/{quote(title.replace(' ', '_'))}",
                        title,
                        "encyclopedia",
                        model_identity,
                        self.name,
                        query,
                    )
                )
                accepted += 1
                if len(results) >= limit:
                    break
            attempts.append(
                DiscoveryAttempt(
                    self.name,
                    query,
                    "success" if accepted else "zero_results",
                    accepted,
                    language=language,
                )
            )
        return ProviderDiscoveryResult(results, attempts)


class TechnicalGrabber:
    def __init__(
        self,
        providers: list[DiscoveryProvider] | None = None,
        max_sources: int = 6,
        per_domain: int = 3,
        timeout_seconds: float = 12.0,
        max_bytes: int = 1_000_000,
        total_discovery_timeout_seconds: float = 30.0,
    ) -> None:
        self.providers = (
            providers
            if providers is not None
            else [
                WebSearchDiscovery(backend="brave,duckduckgo,mojeek", name="ddgs_primary"),
                WebSearchDiscovery(backend="google,startpage,yahoo", name="ddgs_secondary"),
                WikipediaDiscovery(),
            ]
        )
        self.max_sources = max_sources
        self.per_domain = per_domain
        self.timeout_seconds = timeout_seconds
        self.max_bytes = max_bytes
        self.total_discovery_timeout_seconds = total_discovery_timeout_seconds

    async def discover(
        self, identity: KnowledgeIdentity, client: httpx.AsyncClient
    ) -> list[SourceCandidate]:
        return (await self.discover_with_report(identity, client)).candidates

    async def discover_with_report(
        self, identity: KnowledgeIdentity, client: httpx.AsyncClient
    ) -> DiscoveryResult:
        results: list[SourceCandidate] = []
        attempts: list[DiscoveryAttempt] = []
        seen: set[str] = set()
        seen_titles: set[tuple[str, str]] = set()
        domains: dict[str, int] = {}

        async def call_provider(
            provider: DiscoveryProvider,
        ) -> tuple[list[SourceCandidate], list[DiscoveryAttempt]]:
            provider_name = getattr(provider, "name", type(provider).__name__)
            try:
                outcome = await asyncio.wait_for(
                    provider.discover(identity, client, self.max_sources),
                    timeout=self.total_discovery_timeout_seconds,
                )
                if isinstance(outcome, ProviderDiscoveryResult):
                    return outcome.candidates, outcome.attempts
                return outcome, []
            except TimeoutError:
                return [], [
                    DiscoveryAttempt(
                        provider_name,
                        "",
                        "timeout",
                        error_type="ProviderTimeout",
                        error_message=(
                            f"Provider exceeded {self.total_discovery_timeout_seconds:.1f}s timeout"
                        ),
                    )
                ]
            except Exception as exc:
                status = (
                    "network_error"
                    if isinstance(exc, (httpx.HTTPError, OSError))
                    else "provider_unavailable"
                )
                return [], [
                    DiscoveryAttempt(
                        provider_name,
                        "",
                        status,
                        error_type=type(exc).__name__,
                        error_message=concise_error_message(exc),
                    )
                ]

        outcomes = await asyncio.gather(*(call_provider(provider) for provider in self.providers))
        for candidates, provider_attempts in outcomes:
            attempts.extend(provider_attempts)
            for candidate in candidates:
                try:
                    url = canonical_url(candidate.url)
                except ValueError:
                    continue
                domain = urlsplit(url).hostname or ""
                title_key = (domain, re.sub(r"\W+", "", candidate.title.casefold()))
                if (
                    url in seen
                    or (title_key[1] and title_key in seen_titles)
                    or domains.get(domain, 0) >= self.per_domain
                    or len(results) >= self.max_sources
                ):
                    continue
                seen.add(url)
                seen_titles.add(title_key)
                domains[domain] = domains.get(domain, 0) + 1
                results.append(
                    SourceCandidate(
                        url,
                        candidate.title,
                        candidate.source_type,
                        candidate.identity,
                        candidate.discovery_provider,
                        candidate.discovery_query,
                        candidate.snippet,
                    )
                )
        unique_attempts: dict[tuple[str, str, str], DiscoveryAttempt] = {}
        for attempt in attempts:
            key = (
                attempt.provider,
                normalize_display_text(attempt.query),
                attempt.language.casefold(),
            )
            unique_attempts.setdefault(key, attempt)
        return DiscoveryResult(results, list(unique_attempts.values()))

    async def load(self, candidate: SourceCandidate, client: httpx.AsyncClient) -> LoadedSource:
        try:
            url = canonical_url(candidate.url)
            response = await client.get(url, follow_redirects=False, timeout=self.timeout_seconds)
            if response.status_code in {403, 429}:
                return LoadedSource(candidate, None, "blocked")
            response.raise_for_status()
            if len(response.content) > self.max_bytes:
                return LoadedSource(candidate, None, "unsupported")
            content_type = response.headers.get("content-type", "").casefold()
            if "html" in content_type:
                document = process_html(response.content, candidate.title)
            elif "pdf" in content_type:
                try:
                    document = process_pdf(response.content, candidate.title)
                except Exception:
                    return LoadedSource(candidate, None, "parse_error")
            else:
                return LoadedSource(candidate, None, "unsupported")
            if len(document.content.split()) < 30:
                return LoadedSource(candidate, None, "empty_document")
            return LoadedSource(candidate, document, "complete")
        except httpx.TimeoutException:
            return LoadedSource(candidate, None, "timeout")
        except (httpx.HTTPError, ValueError):
            return LoadedSource(candidate, None, "parse_error")


_YEAR_RE = re.compile(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)")
_VEHICLE_YEAR_CONTEXT = re.compile(
    r"problem|issue|recall|model[ -]?year|generation|поколен|неисправ|отзыв",
    re.IGNORECASE,
)


def _fold(value: str) -> str:
    return re.sub(r"[^\w]+", "", value.casefold(), flags=re.UNICODE)


def _generation_tokens(value: str) -> set[str]:
    return {
        token.casefold()
        for token in re.findall(r"[A-Za-zА-Яа-яЁё]*\d+[A-Za-zА-Яа-яЁё\d-]*", value)
        if len(token) >= 2
    }


def source_title_is_incompatible(identity: KnowledgeIdentity, title: str) -> bool:
    """Return True only for an explicit title-level year mismatch."""
    lower = identity.generation_year_from
    upper = identity.generation_year_to
    if not lower or not upper:
        return False
    years = {int(value) for value in _YEAR_RE.findall(title)}
    if not years or not (_VEHICLE_YEAR_CONTEXT.search(title) or len(years) >= 2):
        return False
    return not any(lower <= year <= upper for year in years)


def evidence_scope_identity(
    requested: KnowledgeIdentity, title: str, content: str
) -> KnowledgeIdentity | None:
    """Resolve evidence to exact, model-wide, or incompatible without guessing."""
    if not requested.canonical_generation_id:
        return requested
    if source_title_is_incompatible(requested, title):
        return None
    combined = f"{title} {content}"
    expected_tokens = _generation_tokens(requested.generation)
    folded = combined.casefold()
    if expected_tokens and any(token in folded for token in expected_tokens):
        return requested
    years = {int(value) for value in _YEAR_RE.findall(title)}
    lower = requested.generation_year_from
    upper = requested.generation_year_to
    if (
        lower
        and upper
        and years
        and _VEHICLE_YEAR_CONTEXT.search(title)
        and any(lower <= year <= upper for year in years)
    ):
        return requested
    return requested.fallback()[-1]


def validate_source_scope(requested: KnowledgeIdentity, loaded: LoadedSource) -> LoadedSource:
    """Reject clear mismatches and downgrade unknown generic pages to model scope."""
    if loaded.document is None or not requested.canonical_generation_id:
        return loaded
    candidate = loaded.candidate
    # Providers may deliberately return a verified model-wide source (for example MediaWiki).
    if candidate.identity.canonical_generation_id is None:
        return loaded
    title_text = " ".join((candidate.title, candidate.snippet, loaded.document.title))
    body_text = loaded.document.content[:20_000]
    combined = f"{title_text} {body_text}"
    brand = _fold(requested.brand)
    model = _fold(requested.model)
    folded = _fold(combined)
    if (brand and brand not in folded) or (model and model not in folded):
        return LoadedSource(candidate, None, "incompatible_scope")

    evidence_identity = evidence_scope_identity(requested, title_text, body_text)
    if evidence_identity is None:
        return LoadedSource(candidate, None, "incompatible_scope")
    if evidence_identity.canonical_generation_id:
        return loaded

    # No positive generation signal is UNKNOWN, not MISMATCH: retain as model-wide fallback.
    return replace(loaded, candidate=replace(candidate, identity=evidence_identity))
