import unittest

import ipaddress

from src.ai_assistance.infrastructure.network.url_policy import (
    UnsafeUrlError,
    resolve_and_validate_url,
    validate_fetch_url,
    validate_resolved_addresses,
    validate_url_syntax,
)


class UrlPolicyTests(unittest.TestCase):
    def test_allows_public_http_url(self) -> None:
        self.assertEqual(validate_fetch_url("https://example.com/product"), "https://example.com/product")

    def test_explicit_port_must_be_in_the_allowed_range(self) -> None:
        for url in (
            "https://example.com:0/",
            "https://example.com:65536/",
            "https://example.com:invalid/",
            "https://example.com:/",
        ):
            with self.subTest(url=url), self.assertRaises(UnsafeUrlError):
                validate_url_syntax(url)

        for url, expected_port in (
            ("https://example.com:1/", 1),
            ("https://example.com:65535/", 65535),
        ):
            with self.subTest(url=url):
                self.assertEqual(validate_url_syntax(url).port, expected_port)

    def test_omitted_port_uses_http_defaults(self) -> None:
        self.assertEqual(validate_url_syntax("http://example.com/").port, 80)
        self.assertEqual(validate_url_syntax("https://example.com/").port, 443)

    def test_rejects_local_and_private_targets(self) -> None:
        for url in ("http://localhost/admin", "http://127.0.0.1", "http://192.168.1.10"):
            with self.assertRaises(UnsafeUrlError):
                validate_fetch_url(url)

    def test_rejects_credentials_and_non_http_schemes(self) -> None:
        for url in ("file:///etc/passwd", "https://user:pass@example.com/", "https://example.com:99999/"):
            with self.assertRaises(UnsafeUrlError):
                validate_fetch_url(url)

    def test_syntax_check_does_not_claim_dns_safety(self) -> None:
        self.assertEqual(validate_url_syntax("https://127.0.0.1.nip.io/").hostname, "127.0.0.1.nip.io")
        with self.assertRaises(UnsafeUrlError):
            resolve_and_validate_url("https://127.0.0.1.nip.io/", lambda _: ["127.0.0.1"])

    def test_dns_policy_checks_all_ipv4_and_ipv6_results(self) -> None:
        with self.assertRaises(UnsafeUrlError):
            validate_resolved_addresses(["2001:db8::1", "10.0.0.2"])
        self.assertEqual(validate_resolved_addresses([ipaddress.ip_address("8.8.8.8")]), ("8.8.8.8",))

