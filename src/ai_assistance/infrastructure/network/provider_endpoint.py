import ipaddress
from urllib.parse import urlsplit


class ProviderEndpointError(ValueError):
    """The configured search provider endpoint is not allowed."""


def validate_provider_base_url(url: str) -> str:
    """Validate the trusted, configuration-controlled SearXNG endpoint.

    The provider endpoint must be local: ``localhost``, an IPv4 loopback
    address, or an IPv6 loopback address. Public hosts, public IP addresses,
    non-loopback private IP literals and link-local addresses are rejected.
    Credentials, query strings, fragments and non-HTTP(S) schemes are also
    rejected.
    """
    if not isinstance(url, str) or not url or len(url) > 2048:
        raise ProviderEndpointError("provider URL is empty or too long")
    if any(ord(character) < 33 or ord(character) == 127 for character in url):
        raise ProviderEndpointError("provider URL contains control characters")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ProviderEndpointError("provider URL has an invalid port") from exc
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ProviderEndpointError("provider URL must use HTTP or HTTPS")
    if parsed.username or parsed.password:
        raise ProviderEndpointError("provider URL must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ProviderEndpointError("provider URL must not contain a query or fragment")
    if not parsed.hostname:
        raise ProviderEndpointError("provider URL must contain a host")
    if port is not None and not 1 <= port <= 65535:
        raise ProviderEndpointError("provider port must be between 1 and 65535")

    hostname = parsed.hostname.rstrip(".").lower()
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None

    if literal is not None:
        if not literal.is_loopback:
            raise ProviderEndpointError("provider address must be loopback")
    elif hostname != "localhost":
        raise ProviderEndpointError("provider host must be loopback")

    if parsed.path in ("", "/"):
        return url.rstrip("/")
    return url