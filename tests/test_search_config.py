import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from src.ai_assistance.bootstrap import build_application, build_search_provider
from src.ai_assistance.config import Settings
from src.ai_assistance.infrastructure.network.provider_endpoint import (
    ProviderEndpointError,
    validate_provider_base_url,
)
from src.ai_assistance.infrastructure.search.searxng import SearXNGProvider
from src.ai_assistance.infrastructure.search.transport import SearchHttpResponse


class FakeTransport:
    def get(self, url, *, params, headers, timeout):
        return SearchHttpResponse(status=200, headers={}, body=b"{}")


class ProviderEndpointValidationTests(unittest.TestCase):
    def test_allows_loopback_hosts(self) -> None:
        for url in (
            "http://127.0.0.1:8888",
            "http://127.0.0.2:8888",
            "http://localhost:8888/",
            "http://localhost.:8888",
            "http://[::1]:8888",
        ):
            with self.subTest(url=url):
                self.assertTrue(validate_provider_base_url(url))

    def test_rejects_public_hostnames(self) -> None:
        for url in (
            "https://searx.example.com",
            "http://example.com:8888",
            "http://searx.example.com/searxng/",
        ):
            with self.subTest(url=url), self.assertRaises(ProviderEndpointError):
                validate_provider_base_url(url)

    def test_rejects_public_ip_literals(self) -> None:
        for url in (
            "http://8.8.8.8:8888",
            "http://1.1.1.1:8888",
            "http://[2001:4860:4860::8888]:8888",
        ):
            with self.subTest(url=url), self.assertRaises(ProviderEndpointError):
                validate_provider_base_url(url)

    def test_rejects_private_and_link_local_literals(self) -> None:
        for url in (
            "http://192.168.1.10:8888",
            "http://10.0.0.1:8888",
            "http://172.16.0.1:8888",
            "http://169.254.1.1:8888",
            "http://[fe80::1]:8888",
        ):
            with self.subTest(url=url), self.assertRaises(ProviderEndpointError):
                validate_provider_base_url(url)

    def test_rejects_credentials_query_and_fragment(self) -> None:
        for url in (
            "http://user:pass@127.0.0.1:8888",
            "http://127.0.0.1:8888/path?x=1",
            "http://127.0.0.1:8888/#fragment",
        ):
            with self.subTest(url=url), self.assertRaises(ProviderEndpointError):
                validate_provider_base_url(url)

    def test_rejects_bad_scheme_port_and_host(self) -> None:
        for url in (
            "ftp://127.0.0.1",
            "file:///etc/passwd",
            "http://127.0.0.1:0",
            "http://127.0.0.1:65536",
            "http://",
        ):
            with self.subTest(url=url), self.assertRaises(ProviderEndpointError):
                validate_provider_base_url(url)


class SettingsTests(unittest.TestCase):
    def _settings(self, **overrides) -> Settings:
        values = {"database_path": Path("app.sqlite3")}
        values.update(overrides)
        return Settings(**values)

    def test_search_is_disabled_by_default(self) -> None:
        settings = self._settings()
        self.assertIsNone(settings.search_provider)
        self.assertIsNone(settings.search_base_url)

    def test_searxng_settings_are_accepted(self) -> None:
        settings = self._settings(
            search_provider="searxng",
            search_base_url="http://127.0.0.1:8888",
        )
        self.assertEqual(settings.search_provider, "searxng")
        self.assertEqual(settings.search_base_url, "http://127.0.0.1:8888")

    def test_provider_requires_base_url(self) -> None:
        with self.assertRaises(ValueError):
            self._settings(search_provider="searxng")

    def test_unsupported_provider_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self._settings(
                search_provider="google",
                search_base_url="https://example.com",
            )

    def test_numeric_ranges_are_validated(self) -> None:
        for overrides in (
            {"search_timeout_seconds": 0},
            {"search_timeout_seconds": 61},
            {"search_max_results": 0},
            {"search_max_results": 101},
            {"search_max_query_length": 0},
            {"search_max_snippet_length": 0},
            {"search_language": ""},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self._settings(**overrides)


class BuildSearchProviderTests(unittest.TestCase):
    def _settings(self, **overrides) -> Settings:
        values = {
            "database_path": Path("app.sqlite3"),
            "search_provider": "searxng",
            "search_base_url": "http://127.0.0.1:8888/",
        }
        values.update(overrides)
        return Settings(**values)

    def test_no_provider_returns_none(self) -> None:
        settings = Settings(database_path=Path("app.sqlite3"))
        self.assertIsNone(build_search_provider(settings, FakeTransport()))

    def test_searxng_provider_is_built(self) -> None:
        provider = build_search_provider(self._settings(), FakeTransport())
        self.assertIsInstance(provider, SearXNGProvider)
        self.assertEqual(provider.base_url, "http://127.0.0.1:8888")

    def test_missing_transport_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_search_provider(self._settings(), None)

    def test_invalid_base_url_is_rejected(self) -> None:
        settings = self._settings(search_base_url="http://192.168.1.10:8888")
        with self.assertRaises(ProviderEndpointError):
            build_search_provider(settings, FakeTransport())

    def test_public_endpoint_is_rejected(self) -> None:
        settings = self._settings(search_base_url="https://searx.example.com")
        with self.assertRaises(ProviderEndpointError):
            build_search_provider(settings, FakeTransport())

    def test_public_ip_endpoint_is_rejected(self) -> None:
        settings = self._settings(search_base_url="http://8.8.8.8:8888")
        with self.assertRaises(ProviderEndpointError):
            build_search_provider(settings, FakeTransport())

    def test_credentials_in_base_url_are_rejected(self) -> None:
        settings = self._settings(search_base_url="http://user:pass@127.0.0.1:8888")
        with self.assertRaises(ProviderEndpointError):
            build_search_provider(settings, FakeTransport())


class BuildApplicationWiringTests(unittest.TestCase):
    def test_default_application_has_no_search_provider(self) -> None:
        with TemporaryDirectory() as directory:
            settings = Settings(database_path=Path(directory) / "app.sqlite3")
            application, database = build_application(settings)
            try:
                self.assertIsNone(application._search_provider)
            finally:
                database.close()

    def test_searxng_application_receives_provider(self) -> None:
        with TemporaryDirectory() as directory:
            settings = Settings(
                database_path=Path(directory) / "app.sqlite3",
                search_provider="searxng",
                search_base_url="http://127.0.0.1:8888",
            )
            application, database = build_application(settings, transport=FakeTransport())
            try:
                self.assertIsInstance(application._search_provider, SearXNGProvider)
            finally:
                database.close()

    def test_configured_provider_without_transport_fails_before_db_creation(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "app.sqlite3"
            settings = Settings(
                database_path=path,
                search_provider="searxng",
                search_base_url="http://127.0.0.1:8888",
            )
            with self.assertRaises(ValueError):
                build_application(settings)
            self.assertFalse(path.exists())

    def test_public_endpoint_fails_before_db_creation(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "app.sqlite3"
            settings = Settings(
                database_path=path,
                search_provider="searxng",
                search_base_url="https://searx.example.com",
            )
            with self.assertRaises(ProviderEndpointError):
                build_application(settings, transport=FakeTransport())
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()