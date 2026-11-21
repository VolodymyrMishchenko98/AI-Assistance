import http.client
import ipaddress
import socket
import ssl
from typing import Mapping, Protocol
from urllib.parse import urlencode, urljoin, urlsplit

from ..network.provider_endpoint import (
    ProviderEndpointError,
    validate_provider_base_url,
)
from .transport import (
    SearchHttpResponse,
    TransportConnectionError,
    TransportError,
    TransportTimeout,
)


class DnsResolver(Protocol):
    def resolve(self, host: str, port: int) -> tuple[str, ...]: ...


class ConnectionFactory(Protocol):
    def __call__(
        self,
        scheme: str,
        host: str,
        port: int,
        ip: str,
        read_timeout: float,
    ) -> http.client.HTTPConnection: ...


class SocketDnsResolver:
    def resolve(self, host: str, port: int) -> tuple[str, ...]:
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise TransportConnectionError("DNS resolution failed") from exc
        addresses: list[str] = []
        for info in infos:
            sockaddr = info[4]
            if sockaddr:
                addresses.append(sockaddr[0])
        return tuple(addresses)


class PinnedHTTPConnection(http.client.HTTPConnection):
    """HTTP connection that connects to a pre-validated IP literal."""

    def __init__(
        self,
        host: str,
        port: int,
        ip: str,
        connect_timeout: float,
        read_timeout: float,
    ) -> None:
        super().__init__(host, port, timeout=connect_timeout)
        self._ip = ip
        self._read_timeout = read_timeout

    def connect(self) -> None:
        sock = socket.create_connection((self._ip, self.port), self.timeout)
        try:
            sock.settimeout(self._read_timeout)
        except Exception:
            sock.close()
            raise
        self.sock = sock


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection pinned to a validated IP with original-hostname SNI."""

    def __init__(
        self,
        host: str,
        port: int,
        ip: str,
        connect_timeout: float,
        read_timeout: float,
        ssl_context: ssl.SSLContext,
        sni_host: str,
    ) -> None:
        super().__init__(host, port, timeout=connect_timeout, context=ssl_context)
        self._ip = ip
        self._read_timeout = read_timeout
        self._sni_host = sni_host

    def connect(self) -> None:
        raw = socket.create_connection((self._ip, self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(
                raw, server_hostname=self._sni_host
            )
        except Exception:
            raw.close()
            raise
        self.sock.settimeout(self._read_timeout)


_ALLOWED_REQUEST_HEADERS = {"accept", "accept-language"}
_FORBIDDEN_REQUEST_HEADERS = {
    "host",
    "cookie",
    "authorization",
    "proxy-authorization",
    "content-length",
    "transfer-encoding",
    "connection",
}
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}


def _is_loopback_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if address.version == 6 and address.ipv4_mapped is not None:
        return address.ipv4_mapped.is_loopback
    return address.is_loopback


class SafeHttpTransport:
    """Loopback-only HTTP transport for the configured SearXNG endpoint."""

    def __init__(
        self,
        *,
        resolver: DnsResolver | None = None,
        connect_timeout: float = 3.0,
        max_response_bytes: int = 1 * 1024 * 1024,
        max_header_count: int = 100,
        max_header_bytes: int = 64 * 1024,
        max_redirects: int = 0,
        user_agent: str = "LocalPriceChecker/0.1",
        ssl_context: ssl.SSLContext | None = None,
        connection_factory: ConnectionFactory | None = None,
    ) -> None:
        if connect_timeout <= 0:
            raise ValueError("connect_timeout must be positive")
        if max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be positive")
        if max_header_count <= 0:
            raise ValueError("max_header_count must be positive")
        if max_header_bytes <= 0:
            raise ValueError("max_header_bytes must be positive")
        if max_redirects < 0:
            raise ValueError("max_redirects must not be negative")
        self.resolver = resolver or SocketDnsResolver()
        self.connect_timeout = connect_timeout
        self.max_response_bytes = max_response_bytes
        self.max_header_count = max_header_count
        self.max_header_bytes = max_header_bytes
        self.max_redirects = max_redirects
        self.user_agent = user_agent
        self.ssl_context = ssl_context or ssl.create_default_context()
        if (
            not self.ssl_context.check_hostname
            or self.ssl_context.verify_mode != ssl.CERT_REQUIRED
        ):
            raise ValueError("TLS certificate verification must remain enabled")
        self.connection_factory = connection_factory or self._default_connection

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, str],
        headers: Mapping[str, str],
        timeout: float,
    ) -> SearchHttpResponse:
        if not isinstance(timeout, (int, float)) or timeout <= 0:
            raise TransportError("timeout must be positive")
        try:
            current = validate_provider_base_url(url)
        except ProviderEndpointError as exc:
            raise TransportError("invalid provider URL") from exc
        read_timeout = float(timeout)
        for attempt in range(self.max_redirects + 1):
            response = self._get_once(current, params, headers, read_timeout)
            if response.status not in _REDIRECT_STATUSES:
                return response
            if attempt >= self.max_redirects:
                raise TransportError("redirects are not allowed")
            location = response.headers.get("location")
            if not location:
                raise TransportError("redirect response is missing Location")
            current = self._validate_redirect_url(current, location)
            params = {}
        raise TransportError("too many redirects")

    def _default_connection(
        self,
        scheme: str,
        host: str,
        port: int,
        ip: str,
        read_timeout: float,
    ) -> http.client.HTTPConnection:
        connection_host = (
            f"[{host}]" if ":" in host and not host.startswith("[") else host
        )
        if scheme == "https":
            return PinnedHTTPSConnection(
                connection_host,
                port,
                ip,
                self.connect_timeout,
                read_timeout,
                self.ssl_context,
                sni_host=host,
            )
        return PinnedHTTPConnection(
            connection_host,
            port,
            ip,
            self.connect_timeout,
            read_timeout,
        )

    def _get_once(
        self,
        url: str,
        params: Mapping[str, str],
        headers: Mapping[str, str],
        read_timeout: float,
    ) -> SearchHttpResponse:
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        host = parts.hostname
        if not host:
            raise TransportError("provider URL must contain a host")
        try:
            port = parts.port or (443 if scheme == "https" else 80)
        except ValueError as exc:
            raise TransportError("provider URL has an invalid port") from exc
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"
        if params:
            encoded = urlencode(params, doseq=True)
            path = f"{path}&{encoded}" if "?" in path else f"{path}?{encoded}"
        addresses = self._resolve_addresses(host, port)
        connection = self._open_connection(scheme, host, port, addresses, read_timeout)
        try:
            safe_headers = self._sanitize_headers(headers or {})
            safe_headers["User-Agent"] = self.user_agent
            connection.request("GET", path, headers=safe_headers)
            response = connection.getresponse()
            response_headers = self._read_headers(response)
            self._check_content_length(response)
            self._check_content_encoding(response)
            body = self._read_body(response)
            return SearchHttpResponse(
                status=response.status,
                headers=response_headers,
                body=body,
            )
        except TransportError:
            raise
        except TimeoutError as exc:
            raise TransportTimeout("request timed out") from exc
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            raise TransportConnectionError("request failed") from exc
        finally:
            connection.close()

    def _resolve_addresses(self, host: str, port: int) -> tuple[str, ...]:
        try:
            addresses = self.resolver.resolve(host, port)
        except TransportError:
            raise
        except (socket.gaierror, OSError) as exc:
            raise TransportConnectionError("DNS resolution failed") from exc
        return self._validate_addresses(addresses)

    def _validate_addresses(self, addresses) -> tuple[str, ...]:
        if isinstance(addresses, str):
            addresses = (addresses,)
        validated: list[str] = []
        for value in addresses:
            try:
                address = ipaddress.ip_address(value)
            except ValueError as exc:
                raise TransportConnectionError(
                    "DNS returned an invalid address"
                ) from exc
            if not _is_loopback_address(address):
                raise TransportConnectionError("provider address must be loopback")
            text = str(address)
            if text not in validated:
                validated.append(text)
        if not validated:
            raise TransportConnectionError("DNS returned no addresses")
        return tuple(validated)

    def _open_connection(
        self,
        scheme: str,
        host: str,
        port: int,
        addresses: tuple[str, ...],
        read_timeout: float,
    ) -> http.client.HTTPConnection:
        last_error: BaseException | None = None
        for ip in addresses:
            try:
                connection = self.connection_factory(
                    scheme, host, port, ip, read_timeout
                )
            except TransportError:
                raise
            except TimeoutError as exc:
                raise TransportTimeout("connection timed out") from exc
            except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
                last_error = exc
                continue
            try:
                connection.connect()
                return connection
            except TransportError:
                connection.close()
                raise
            except TimeoutError as exc:
                connection.close()
                raise TransportTimeout("connection timed out") from exc
            except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
                connection.close()
                last_error = exc
                continue
        raise TransportConnectionError("connection failed") from last_error

    @staticmethod
    def _sanitize_headers(headers: Mapping[str, str]) -> dict[str, str]:
        safe: dict[str, str] = {}
        for name, value in headers.items():
            lower = name.lower()
            if lower in _ALLOWED_REQUEST_HEADERS and lower not in _FORBIDDEN_REQUEST_HEADERS:
                safe[name] = value
        return safe

    def _read_headers(self, response) -> dict[str, str]:
        headers = response.getheaders()
        if len(headers) > self.max_header_count:
            raise TransportError("response headers exceed the count limit")
        total = sum(len(name) + len(value) for name, value in headers)
        if total > self.max_header_bytes:
            raise TransportError("response headers exceed the size limit")
        return {name.lower(): value for name, value in headers}

    def _check_content_length(self, response) -> None:
        value = response.getheader("Content-Length")
        if value is None:
            return
        try:
            length = int(value)
        except (TypeError, ValueError) as exc:
            raise TransportError("invalid Content-Length") from exc
        if length < 0 or length > self.max_response_bytes:
            raise TransportError("response exceeds the size limit")

    def _check_content_encoding(self, response) -> None:
        value = response.getheader("Content-Encoding")
        if value is not None and value.strip().lower() not in ("", "identity"):
            raise TransportError("unsupported Content-Encoding")

    def _read_body(self, response) -> bytes:
        body = bytearray()
        while True:
            remaining = self.max_response_bytes - len(body)
            chunk = response.read(min(64 * 1024, remaining + 1))
            if not chunk:
                break
            body.extend(chunk)
            if len(body) > self.max_response_bytes:
                raise TransportError("response exceeds the size limit")
        return bytes(body)

    @staticmethod
    def _validate_redirect_url(current: str, location: str) -> str:
        if not isinstance(location, str) or not location or location != location.strip():
            raise TransportError("malformed redirect Location")
        if any(ord(character) < 33 or ord(character) == 127 for character in location):
            raise TransportError("malformed redirect Location")
        try:
            current_parts = urlsplit(current)
            next_parts = urlsplit(urljoin(current, location))
            next_port = next_parts.port
            current_port = current_parts.port
        except ValueError as exc:
            raise TransportError("malformed redirect Location") from exc
        if next_parts.scheme.lower() not in {"http", "https"}:
            raise TransportError("redirect must use HTTP or HTTPS")
        if next_parts.username or next_parts.password:
            raise TransportError("redirect must not contain credentials")
        if not next_parts.hostname:
            raise TransportError("redirect must contain a host")
        current_scheme = current_parts.scheme.lower()
        next_scheme = next_parts.scheme.lower()
        current_host = (current_parts.hostname or "").rstrip(".").lower()
        next_host = next_parts.hostname.rstrip(".").lower()
        current_port = current_port or (443 if current_scheme == "https" else 80)
        next_port = next_port or (443 if next_scheme == "https" else 80)
        if next_scheme != current_scheme:
            raise TransportError("redirect scheme change is not allowed")
        if next_host != current_host:
            raise TransportError("redirect host change is not allowed")
        if next_port != current_port:
            raise TransportError("redirect port change is not allowed")
        try:
            literal = ipaddress.ip_address(next_host)
        except ValueError:
            literal = None
        if literal is not None:
            if not _is_loopback_address(literal):
                raise TransportError("redirect must remain loopback")
        elif next_host != "localhost":
            raise TransportError("redirect must remain loopback")
        return urljoin(current, location)