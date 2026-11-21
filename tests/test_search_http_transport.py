import http.client
import logging
import os
import socket
import ssl
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from src.ai_assistance.infrastructure.search.http_transport import (
    PinnedHTTPSConnection,
    SafeHttpTransport,
    SocketDnsResolver,
)
from src.ai_assistance.infrastructure.search.transport import (
    SearchHttpResponse,
    TransportConnectionError,
    TransportError,
    TransportTimeout,
)


class FakeResolver:
    def __init__(self, addresses=(), error=None) -> None:
        self.addresses = addresses
        self.error = error
        self.calls: list[tuple[str, int]] = []

    def resolve(self, host: str, port: int) -> tuple[str, ...]:
        self.calls.append((host, port))
        if self.error is not None:
            raise self.error
        return self.addresses


class FakeResponse:
    def __init__(self, status=200, headers=None, body=b"", read_error=None) -> None:
        self.status = status
        self._headers = list(headers or [])
        self._body = body
        self._read_error = read_error
        self._position = 0

    def getheaders(self):
        return list(self._headers)

    def getheader(self, name):
        for key, value in self._headers:
            if key.lower() == name.lower():
                return value
        return None

    def read(self, size):
        if self._read_error is not None:
            raise self._read_error
        chunk = self._body[self._position : self._position + size]
        self._position += len(chunk)
        return chunk


class FakeConnection:
    def __init__(self, response, error=None) -> None:
        self.response = response
        self.error = error
        self.closed = False
        self.requests: list[tuple[str, str, dict]] = []

    def connect(self):
        if self.error is not None:
            raise self.error

    def request(self, method, path, headers=None):
        self.requests.append((method, path, dict(headers or {})))
        if self.error is not None:
            raise self.error

    def getresponse(self):
        if self.error is not None:
            raise self.error
        return self.response

    def close(self):
        self.closed = True


class FakeConnectionFactory:
    def __init__(self, connections) -> None:
        self.connections = list(connections)
        self.calls: list[tuple] = []

    def __call__(self, scheme, host, port, ip, read_timeout):
        self.calls.append((scheme, host, port, ip, read_timeout))
        if not self.connections:
            raise AssertionError("no fake connection available")
        return self.connections.pop(0)


class FakeSslContext:
    check_hostname = True
    verify_mode = ssl.CERT_REQUIRED

    def __init__(self) -> None:
        self.calls: list[str] = []

    def wrap_socket(self, sock, server_hostname):
        self.calls.append(server_hostname)
        return sock


class FakeRawSocket:
    def __init__(self) -> None:
        self.timeout = None
        self.closed = False

    def settimeout(self, value):
        self.timeout = value

    def close(self):
        self.closed = True


class _RecordingHandler(BaseHTTPRequestHandler):
    hosts: list[str | None] = []

    def do_GET(self):
        type(self).hosts.append(self.headers.get("Host"))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


class _SlowHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        time.sleep(1.0)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


class _V6Server(ThreadingHTTPServer):
    address_family = socket.AF_INET6


class SafeHttpTransportTests(unittest.TestCase):
    def _transport(self, connections, **overrides) -> tuple[SafeHttpTransport, FakeConnectionFactory]:
        factory = FakeConnectionFactory(connections)
        values = {
            "resolver": FakeResolver(["127.0.0.1"]),
            "connection_factory": factory,
            "max_response_bytes": 1024,
            "max_header_count": 100,
            "max_header_bytes": 64 * 1024,
            "max_redirects": 0,
        }
        values.update(overrides)
        return SafeHttpTransport(**values), factory

    def _get(self, transport, url="http://localhost:8888/search", params=None, timeout=1.0):
        return transport.get(
            url,
            params=params or {},
            headers={"Accept": "application/json"},
            timeout=timeout,
        )

    # ------------------------------------------------------------------
    # Address validation
    # ------------------------------------------------------------------

    def test_loopback_ipv4_and_ipv6_addresses_are_accepted(self) -> None:
        transport = SafeHttpTransport()
        self.assertEqual(
            transport._validate_addresses(["127.0.0.1", "::1"]),
            ("127.0.0.1", "::1"),
        )

    def test_invalid_provider_url_maps_to_transport_error(self) -> None:
        transport = SafeHttpTransport(
            connection_factory=FakeConnectionFactory([])
        )
        with self.assertRaises(TransportError):
            transport.get(
                "https://example.com/search",
                params={},
                headers={},
                timeout=1.0,
            )

    def test_public_dns_answer_is_rejected(self) -> None:
        transport = SafeHttpTransport()
        with self.assertRaises(TransportConnectionError):
            transport._validate_addresses(["8.8.8.8"])

    def test_mixed_loopback_and_public_dns_answer_is_rejected(self) -> None:
        transport = SafeHttpTransport()
        with self.assertRaises(TransportConnectionError):
            transport._validate_addresses(["127.0.0.1", "8.8.8.8"])

    def test_private_and_link_local_addresses_are_rejected(self) -> None:
        transport = SafeHttpTransport()
        for address in ("192.168.1.1", "10.0.0.1", "169.254.1.1", "fe80::1"):
            with self.subTest(address=address), self.assertRaises(
                TransportConnectionError
            ):
                transport._validate_addresses([address])

    def test_dns_resolution_failure_maps_to_connection_error(self) -> None:
        resolver = FakeResolver(error=socket.gaierror("no dns"))
        transport = SafeHttpTransport(resolver=resolver)
        with self.assertRaises(TransportConnectionError):
            self._get(transport)

    def test_socket_dns_resolver_extracts_addresses(self) -> None:
        infos = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 8888)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 8888, 0, 0)),
        ]
        original = socket.getaddrinfo
        socket.getaddrinfo = lambda *args, **kwargs: infos
        try:
            resolver = SocketDnsResolver()
            self.assertEqual(
                resolver.resolve("localhost", 8888),
                ("127.0.0.1", "::1"),
            )
        finally:
            socket.getaddrinfo = original

    # ------------------------------------------------------------------
    # Connection pinning and DNS
    # ------------------------------------------------------------------

    def test_connection_uses_validated_ip_not_hostname(self) -> None:
        transport, factory = self._transport(
            [FakeConnection(FakeResponse(body=b"{}"))]
        )
        self._get(transport, url="http://localhost:8888/search")
        self.assertEqual(
            factory.calls[0],
            ("http", "localhost", 8888, "127.0.0.1", 1.0),
        )

    def test_no_second_dns_lookup(self) -> None:
        resolver = FakeResolver(["127.0.0.1"])
        factory = FakeConnectionFactory([FakeConnection(FakeResponse(body=b"{}"))])
        transport = SafeHttpTransport(
            resolver=resolver,
            connection_factory=factory,
        )
        original = socket.getaddrinfo

        def fail(*args, **kwargs):
            raise AssertionError("unexpected second DNS lookup")

        socket.getaddrinfo = fail
        try:
            self._get(transport, url="http://localhost:8888/search")
        finally:
            socket.getaddrinfo = original
        self.assertEqual(len(resolver.calls), 1)

    def test_connection_timeout_maps_to_transport_timeout(self) -> None:
        connection = FakeConnection(
            FakeResponse(), error=TimeoutError("connect timeout")
        )
        transport, _ = self._transport([connection])
        with self.assertRaises(TransportTimeout):
            self._get(transport)

    def test_connection_failure_maps_to_transport_connection_error(self) -> None:
        connection = FakeConnection(
            FakeResponse(), error=ConnectionRefusedError("refused")
        )
        transport, _ = self._transport([connection])
        with self.assertRaises(TransportConnectionError):
            self._get(transport)

    def test_read_timeout_maps_to_transport_timeout(self) -> None:
        response = FakeResponse(body=b"", read_error=TimeoutError("read timeout"))
        transport, _ = self._transport([FakeConnection(response)])
        with self.assertRaises(TransportTimeout):
            self._get(transport)

    # ------------------------------------------------------------------
    # TLS configuration
    # ------------------------------------------------------------------

    def test_tls_sni_and_hostname_verification_configuration(self) -> None:
        context = FakeSslContext()
        connection = PinnedHTTPSConnection(
            "localhost",
            8443,
            "127.0.0.1",
            3.0,
            5.0,
            context,
            sni_host="localhost",
        )
        raw = FakeRawSocket()
        original = socket.create_connection
        socket.create_connection = lambda *args, **kwargs: raw
        try:
            connection.connect()
        finally:
            socket.create_connection = original
        self.assertEqual(context.calls, ["localhost"])
        self.assertEqual(connection.sock.timeout, 5.0)

    def test_certificate_verification_remains_enabled(self) -> None:
        context = ssl.create_default_context()
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        disabled = ssl.create_default_context()
        disabled.check_hostname = False
        disabled.verify_mode = ssl.CERT_NONE
        with self.assertRaises(ValueError):
            SafeHttpTransport(
                ssl_context=disabled,
                connection_factory=FakeConnectionFactory([]),
            )

    # ------------------------------------------------------------------
    # Response handling
    # ------------------------------------------------------------------

    def test_http_status_and_body_are_preserved(self) -> None:
        response = FakeResponse(status=418, body=b"teapot")
        transport, _ = self._transport([FakeConnection(response)])
        result = self._get(transport)
        self.assertEqual(result.status, 418)
        self.assertEqual(result.body, b"teapot")

    def test_response_body_within_limit(self) -> None:
        transport, _ = self._transport(
            [FakeConnection(FakeResponse(body=b"{}"))],
            max_response_bytes=1024,
        )
        result = self._get(transport)
        self.assertEqual(result.body, b"{}")

    def test_oversized_content_length_is_rejected(self) -> None:
        response = FakeResponse(headers=[("Content-Length", "100")], body=b"")
        transport, _ = self._transport(
            [FakeConnection(response)], max_response_bytes=10
        )
        with self.assertRaises(TransportError):
            self._get(transport)

    def test_oversized_streamed_body_is_rejected(self) -> None:
        response = FakeResponse(headers=[], body=b"x" * 100)
        transport, _ = self._transport(
            [FakeConnection(response)], max_response_bytes=10
        )
        with self.assertRaises(TransportError):
            self._get(transport)

    def test_excessive_header_count_is_rejected(self) -> None:
        response = FakeResponse(
            headers=[(f"X-{index}", "v") for index in range(10)], body=b""
        )
        transport, _ = self._transport(
            [FakeConnection(response)], max_header_count=5
        )
        with self.assertRaises(TransportError):
            self._get(transport)

    def test_excessive_header_bytes_are_rejected(self) -> None:
        response = FakeResponse(headers=[("X-Big", "x" * 100)], body=b"")
        transport, _ = self._transport(
            [FakeConnection(response)], max_header_bytes=10
        )
        with self.assertRaises(TransportError):
            self._get(transport)

    def test_unsupported_content_encoding_is_rejected(self) -> None:
        response = FakeResponse(
            headers=[("Content-Encoding", "gzip")], body=b"not compressed"
        )
        transport, _ = self._transport([FakeConnection(response)])
        with self.assertRaises(TransportError):
            self._get(transport)

    # ------------------------------------------------------------------
    # Redirects
    # ------------------------------------------------------------------

    def test_redirects_are_rejected_by_default(self) -> None:
        response = FakeResponse(
            status=302, headers=[("Location", "/next")], body=b""
        )
        transport, _ = self._transport([FakeConnection(response)])
        with self.assertRaises(TransportError):
            self._get(transport)

    def test_same_origin_redirect_is_followed(self) -> None:
        first = FakeResponse(status=302, headers=[("Location", "/next")], body=b"")
        second = FakeResponse(status=200, body=b"ok")
        transport, factory = self._transport(
            [FakeConnection(first), FakeConnection(second)], max_redirects=1
        )
        result = self._get(transport, params={"q": "pump"})
        self.assertEqual(result.status, 200)
        self.assertEqual(result.body, b"ok")
        self.assertEqual(len(factory.calls), 2)

    def test_cross_origin_redirect_is_rejected(self) -> None:
        response = FakeResponse(
            status=302, headers=[("Location", "http://example.com/")], body=b""
        )
        transport, factory = self._transport(
            [FakeConnection(response), FakeConnection(FakeResponse())],
            max_redirects=1,
        )
        with self.assertRaises(TransportError):
            self._get(transport)
        self.assertEqual(len(factory.calls), 1)

    def test_https_downgrade_redirect_is_rejected(self) -> None:
        with self.assertRaises(TransportError):
            SafeHttpTransport._validate_redirect_url(
                "https://localhost:8443/search",
                "http://localhost:8443/search",
            )

    def test_cross_host_redirect_is_rejected(self) -> None:
        with self.assertRaises(TransportError):
            SafeHttpTransport._validate_redirect_url(
                "http://localhost:8888/search",
                "http://127.0.0.1:8888/search",
            )

    def test_redirect_loop_is_bounded(self) -> None:
        response = FakeResponse(
            status=302, headers=[("Location", "/search")], body=b""
        )
        transport, _ = self._transport(
            [FakeConnection(response), FakeConnection(response)], max_redirects=1
        )
        with self.assertRaises(TransportError):
            self._get(transport)

    # ------------------------------------------------------------------
    # Cleanup and headers
    # ------------------------------------------------------------------

    def test_connection_is_closed_after_success(self) -> None:
        connection = FakeConnection(FakeResponse(body=b"{}"))
        transport, _ = self._transport([connection])
        self._get(transport)
        self.assertTrue(connection.closed)

    def test_connection_is_closed_after_error(self) -> None:
        connection = FakeConnection(FakeResponse(body=b"x" * 100))
        transport, _ = self._transport([connection], max_response_bytes=10)
        with self.assertRaises(TransportError):
            self._get(transport)
        self.assertTrue(connection.closed)

    def test_forbidden_request_headers_are_dropped(self) -> None:
        connection = FakeConnection(FakeResponse(body=b"{}"))
        transport, _ = self._transport([connection])
        transport.get(
            "http://localhost:8888/search",
            params={},
            headers={
                "Accept": "application/json",
                "Cookie": "session=secret",
                "Authorization": "Bearer secret",
                "Host": "evil.example.com",
                "Connection": "keep-alive",
            },
            timeout=1.0,
        )
        sent = connection.requests[0][2]
        self.assertEqual(sent.get("Accept"), "application/json")
        self.assertNotIn("Cookie", sent)
        self.assertNotIn("Authorization", sent)
        self.assertNotIn("Host", sent)
        self.assertNotIn("Connection", sent)
        self.assertTrue(sent.get("User-Agent", "").startswith("LocalPriceChecker"))

    def test_search_query_is_not_logged(self) -> None:
        records: list[str] = []

        class Handler(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        logger = logging.getLogger()
        handler = Handler()
        logger.addHandler(handler)
        try:
            transport, _ = self._transport([FakeConnection(FakeResponse(body=b"{}"))])
            self._get(transport, params={"q": "Pumps"})
        finally:
            logger.removeHandler(handler)
        self.assertFalse(any("Pumps" in message for message in records))

    # ------------------------------------------------------------------
    # Local loopback integration
    # ------------------------------------------------------------------

    def _start_server(self, handler=_RecordingHandler, ipv6=False):
        server_class = _V6Server if ipv6 else ThreadingHTTPServer
        address = ("::1" if ipv6 else "127.0.0.1", 0)
        server = server_class(address, handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    def _stop_server(self, server, thread):
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)

    def test_host_header_is_preserved(self) -> None:
        _RecordingHandler.hosts = []
        server, thread = self._start_server()
        try:
            port = server.server_address[1]
            transport = SafeHttpTransport(resolver=FakeResolver(["127.0.0.1"]))
            result = self._get(
                transport, url=f"http://localhost:{port}/search", timeout=2.0
            )
            self.assertEqual(result.status, 200)
            self.assertEqual(_RecordingHandler.hosts[-1], f"localhost:{port}")
        finally:
            self._stop_server(server, thread)

    def test_read_timeout_with_local_server(self) -> None:
        server, thread = self._start_server(_SlowHandler)
        try:
            port = server.server_address[1]
            transport = SafeHttpTransport(resolver=FakeResolver(["127.0.0.1"]))
            with self.assertRaises(TransportTimeout):
                self._get(
                    transport,
                    url=f"http://127.0.0.1:{port}/search",
                    timeout=0.2,
                )
        finally:
            self._stop_server(server, thread)

    def test_proxy_environment_variables_are_ignored(self) -> None:
        server, thread = self._start_server()
        keys = (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
        )
        original = {key: os.environ.get(key) for key in keys}
        try:
            for key in keys:
                os.environ[key] = "http://127.0.0.1:1"
            port = server.server_address[1]
            transport = SafeHttpTransport(resolver=FakeResolver(["127.0.0.1"]))
            result = self._get(
                transport, url=f"http://127.0.0.1:{port}/search", timeout=2.0
            )
            self.assertEqual(result.status, 200)
        finally:
            for key, value in original.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            self._stop_server(server, thread)

    def test_ipv6_loopback_connection(self) -> None:
        try:
            server, thread = self._start_server(ipv6=True)
        except OSError:
            self.skipTest("IPv6 loopback is not available")
        try:
            port = server.server_address[1]
            transport = SafeHttpTransport(resolver=FakeResolver(["::1"]))
            result = self._get(
                transport, url=f"http://[::1]:{port}/search", timeout=2.0
            )
            self.assertEqual(result.status, 200)
        finally:
            self._stop_server(server, thread)


if __name__ == "__main__":
    unittest.main()