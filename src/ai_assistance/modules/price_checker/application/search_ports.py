from typing import Protocol

from .search_dto import SearchFailure, SearchQuery, SearchResult


class SearchProviderError(Exception):
    """A typed provider failure; never an empty successful result."""

    def __init__(self, failure: SearchFailure, message: str) -> None:
        super().__init__(message)
        self.failure = failure
        self.message = message

    @property
    def retryable(self) -> bool:
        return self.failure in (SearchFailure.RATE_LIMITED, SearchFailure.TEMPORARY)


class SearchProvider(Protocol):
    def search(self, query: SearchQuery) -> SearchResult: ...