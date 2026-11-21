from dataclasses import dataclass
from typing import Mapping, Protocol


class TransportError(Exception):
    """Base error for the search transport."""


class TransportTimeout(TransportError):
    """The provider did not respond in time."""


class TransportConnectionError(TransportError):
    """The provider could not be reached or the connection failed."""


@dataclass(frozen=True, slots=True)
class SearchHttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class SearchTransport(Protocol):
    def get(
        self,
        url: str,
        *,
        params: Mapping[str, str],
        headers: Mapping[str, str],
        timeout: float,
    ) -> SearchHttpResponse: ...