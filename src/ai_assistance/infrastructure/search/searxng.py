import json
from datetime import datetime, timezone

from ...modules.price_checker.application.search_dto import (
    SearchCandidate,
    SearchFailure,
    SearchQuery,
    SearchResult,
    SearchStatus,
)
from ...modules.price_checker.application.search_ports import (
    SearchProvider,
    SearchProviderError,
)
from ..network.url_policy import UnsafeUrlError, validate_url_syntax
from .transport import (
    SearchHttpResponse,
    SearchTransport,
    TransportError,
    TransportTimeout,
)


class SearXNGProvider:
    """SearXNG JSON adapter.

    The adapter sends only the item name and unit. It never receives or sends
    the item description, run metadata, audit data or document content.
    """

    provider_name = "searxng"

    def __init__(
        self,
        *,
        base_url: str,
        transport: SearchTransport,
        timeout: float = 5.0,
        max_results: int = 10,
        max_query_length: int = 200,
        max_title_length: int = 300,
        max_snippet_length: int = 500,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.transport = transport
        self.timeout = timeout
        self.max_results = max_results
        self.max_query_length = max_query_length
        self.max_title_length = max_title_length
        self.max_snippet_length = max_snippet_length

    def search(self, query: SearchQuery) -> SearchResult:
        text = self._build_query_text(query)
        params = {
            "q": text,
            "format": "json",
            "language": query.language,
            "pageno": "1",
        }
        try:
            response = self.transport.get(
                f"{self.base_url}/search",
                params=params,
                headers={"Accept": "application/json"},
                timeout=self.timeout,
            )
        except TransportTimeout as exc:
            raise SearchProviderError(
                SearchFailure.TEMPORARY, "search provider timed out"
            ) from exc
        except TransportError as exc:
            raise SearchProviderError(
                SearchFailure.TEMPORARY, "search provider connection failed"
            ) from exc

        if response.status == 429:
            raise SearchProviderError(
                SearchFailure.RATE_LIMITED, "search provider rate limit"
            )
        if response.status in (401, 403):
            raise SearchProviderError(
                SearchFailure.CONFIGURATION, "search provider access denied"
            )
        if 400 <= response.status < 500:
            raise SearchProviderError(
                SearchFailure.PERMANENT, "search provider rejected the request"
            )
        if response.status >= 500:
            raise SearchProviderError(
                SearchFailure.TEMPORARY, "search provider server error"
            )
        if not 200 <= response.status < 300:
            raise SearchProviderError(
                SearchFailure.TEMPORARY, "search provider returned an unexpected status"
            )

        payload = self._parse_json(response)
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise SearchProviderError(
                SearchFailure.CONFIGURATION,
                "search provider response is missing results",
            )

        limit = min(query.limit, self.max_results)
        candidates: list[SearchCandidate] = []
        for index, raw in enumerate(payload["results"]):
            candidate = self._parse_candidate(raw, index)
            if candidate is None:
                continue
            candidates.append(candidate)
            if len(candidates) >= limit:
                break

        return SearchResult(
            provider=self.provider_name,
            status=SearchStatus.OK if candidates else SearchStatus.NO_RESULTS,
            candidates=tuple(candidates),
            searched_at=datetime.now(timezone.utc),
            query=text,
        )

    @staticmethod
    def _parse_json(response: SearchHttpResponse) -> object:
        try:
            return json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SearchProviderError(
                SearchFailure.TEMPORARY, "search provider returned invalid JSON"
            ) from exc

    def _build_query_text(self, query: SearchQuery) -> str:
        parts = [query.name.strip()]
        if query.unit and query.unit.strip():
            parts.append(query.unit.strip())
        return " ".join(parts)[: self.max_query_length]

    def _parse_candidate(self, raw: object, index: int) -> SearchCandidate | None:
        if not isinstance(raw, dict):
            return None
        url = raw.get("url")
        if not isinstance(url, str) or not url:
            return None
        try:
            validated = validate_url_syntax(url)
        except UnsafeUrlError:
            return None
        return SearchCandidate(
            url=validated.url,
            title=self._truncate(raw.get("title"), self.max_title_length),
            snippet=self._truncate(raw.get("content"), self.max_snippet_length),
            rank=index,
            provider=self.provider_name,
        )

    @staticmethod
    def _truncate(value: object, limit: int) -> str | None:
        if not isinstance(value, str):
            return None
        return value[:limit]