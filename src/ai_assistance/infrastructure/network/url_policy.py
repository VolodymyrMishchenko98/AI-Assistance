import ipaddress
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from urllib.parse import SplitResult, urlsplit, urlunsplit


class UnsafeUrlError(ValueError):
    """The URL or resolved destination violates the fetch policy."""


@dataclass(frozen=True, slots=True)
class ParsedFetchUrl:
    url: str
    scheme: str
    hostname: str
    port: int


def _is_forbidden_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    mapped = address.ipv4_mapped if isinstance(address, ipaddress.IPv6Address) else None
    candidates = (address, mapped) if mapped is not None else (address,)
    return any(
        candidate.is_private
        or candidate.is_loopback
        or candidate.is_link_local
        or candidate.is_multicast
        or candidate.is_reserved
        or candidate.is_unspecified
        for candidate in candidates
    )


def _normalized_hostname(parsed: SplitResult) -> str:
    if not parsed.hostname:
        raise UnsafeUrlError("URL must contain a host")
    try:
        return parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise UnsafeUrlError("hostname is not a valid IDN") from exc


def validate_url_syntax(url: str) -> ParsedFetchUrl:
    """Validate syntax only; this does not prove that a fetch is safe."""
    if not isinstance(url, str) or any(ord(char) < 32 for char in url) or len(url) > 2048:
        raise UnsafeUrlError("URL contains invalid characters or is too long")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise UnsafeUrlError("URL has an invalid port or host") from exc
    if parsed.scheme.lower() not in {"http", "https"}:
        raise UnsafeUrlError("only HTTP and HTTPS URLs are allowed")
    if parsed.username or parsed.password:
        raise UnsafeUrlError("URL must not contain credentials")
    hostname = _normalized_hostname(parsed)
    if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(".localhost"):
        raise UnsafeUrlError("local hostnames are not allowed")
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None and _is_forbidden_address(literal):
        raise UnsafeUrlError("private or local network addresses are not allowed")
    if port is None and parsed.netloc.endswith(":"):
        raise UnsafeUrlError("URL has an invalid port")
    if port is None:
        port = 443 if parsed.scheme.lower() == "https" else 80
    elif not 1 <= port <= 65535:
        raise UnsafeUrlError("URL port must be between 1 and 65535")
    normalized = urlunsplit((parsed.scheme.lower(), parsed.netloc, parsed.path, parsed.query, parsed.fragment))
    return ParsedFetchUrl(normalized, parsed.scheme.lower(), hostname, port)


def validate_resolved_addresses(addresses: Iterable[str | ipaddress._BaseAddress]) -> tuple[str, ...]:
    """Reject every DNS result that is not a public unicast address."""
    normalized: list[str] = []
    for value in addresses:
        try:
            address = value if isinstance(value, (ipaddress.IPv4Address, ipaddress.IPv6Address)) else ipaddress.ip_address(value)
        except ValueError as exc:
            raise UnsafeUrlError("DNS returned an invalid IP address") from exc
        if _is_forbidden_address(address):
            raise UnsafeUrlError("DNS returned a private or local network address")
        normalized.append(str(address))
    if not normalized:
        raise UnsafeUrlError("DNS returned no addresses")
    return tuple(normalized)


def resolve_and_validate_url(
    url: str,
    resolver: Callable[[str], Iterable[str | ipaddress._BaseAddress]],
) -> tuple[ParsedFetchUrl, tuple[str, ...]]:
    """Validate syntax and one DNS resolution; the transport must pin these IPs.

    This function intentionally does not perform HTTP, redirects, or socket I/O.
    A future fetcher must resolve immediately before connecting, validate every
    A/AAAA result, connect to one validated address, and repeat this process for
    every redirect.
    """
    parsed = validate_url_syntax(url)
    try:
        literal = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        literal = None
    addresses = (parsed.hostname,) if literal is not None else resolver(parsed.hostname)
    return parsed, validate_resolved_addresses(addresses)


def validate_fetch_url(url: str) -> str:
    """Backward-compatible syntax check; not a complete safe-fetch decision."""
    return validate_url_syntax(url).url
