from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class SearchStatus(StrEnum):
    OK = "ok"
    NO_RESULTS = "no_results"


class SearchFailure(StrEnum):
    RATE_LIMITED = "rate_limited"
    TEMPORARY = "temporary"
    PERMANENT = "permanent"
    CONFIGURATION = "configuration"


@dataclass(frozen=True, slots=True)
class SearchQuery:
    item_id: UUID
    run_id: UUID
    name: str
    unit: str | None
    language: str
    limit: int
    correlation_id: UUID

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("search item name must not be empty")
        if len(self.name) > 500:
            raise ValueError("search item name is too long")
        if self.unit is not None:
            if not isinstance(self.unit, str):
                raise ValueError("search item unit must be a string")
            if len(self.unit) > 100:
                raise ValueError("search item unit is too long")
        if not isinstance(self.language, str) or not self.language.strip():
            raise ValueError("search language must not be empty")
        if not isinstance(self.limit, int) or not 1 <= self.limit <= 100:
            raise ValueError("search limit must be between 1 and 100")


@dataclass(frozen=True, slots=True)
class SearchCandidate:
    url: str
    title: str | None
    snippet: str | None
    rank: int
    provider: str


@dataclass(frozen=True, slots=True)
class SearchResult:
    provider: str
    status: SearchStatus
    candidates: tuple[SearchCandidate, ...]
    searched_at: datetime
    query: str