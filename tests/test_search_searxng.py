import json
import unittest
from datetime import datetime
from uuid import uuid4

from src.ai_assistance.infrastructure.network.url_policy import UnsafeUrlError
from src.ai_assistance.infrastructure.search.searxng import SearXNGProvider
from src.ai_assistance.infrastructure.search.transport import (
    SearchHttpResponse,
    TransportConnectionError,
    TransportTimeout,
)
from src.ai_assistance.modules.price_checker.application.search_dto import (
    SearchFailure,
    SearchQuery,
    SearchStatus,
)
from src.ai_assistance.modules.price_checker.application.search_ports import (
    SearchProviderError,
)


class FakeTransport:
    def __init__(self, response=None, error=None) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict] = []

    def get(self, url, *, params, headers, timeout):
        self.calls.append(
            {
                "url": url,
                "params": dict(params),
                "headers": dict(headers),
                "timeout": timeout,
            }
        )
        if self.error is not None:
            raise self.error
        return self.response


def make_query(name="Насос", unit="шт.", limit=10) -> SearchQuery:
    return SearchQuery(
        item_id=uuid4(),
        run_id=uuid4(),
        name=name,
        unit=unit,
        language="uk-UA",
        limit=limit,
        correlation_id=uuid4(),
    )


def make_response(status=200, payload=None, body=None) -> SearchHttpResponse:
    if body is None:
        body = json.dumps(payload if payload is not None else {}).encode("utf-8")
    return SearchHttpResponse(
        status=status,
        headers={"Content-Type": "application/json"},
        body=body,
    )


class SearXNGProviderTests(unittest.TestCase):
    def _provider(self, transport, **overrides) -> SearXNGProvider:
        values = {
            "base_url": "http://127.0.0.1:8888",
            "transport": transport,
            "timeout": 5.0,
            "max_results": 10,
            "max_query_length": 200,
            "max_title_length": 300,
            "max_snippet_length": 500,
        }
        values.update(overrides)
        return SearXNGProvider(**values)

    def test_valid_response_returns_candidates_in_provider_order(self) -> None:
        payload = {
            "results": [
                {"url": "https://shop1.example/1", "title": "Насос 1", "content": "Опис 1"},
                {"url": "https://shop2.example/2", "title": "Насос 2", "content": "Опис 2"},
                {"url": "https://shop3.example/3", "title": "Насос 3", "content": "Опис 3"},
            ]
        }
        transport = FakeTransport(response=make_response(payload=payload))
        result = self._provider(transport).search(make_query())

        self.assertIs(result.status, SearchStatus.OK)
        self.assertEqual(result.provider, "searxng")
        self.assertIsInstance(result.searched_at, datetime)
        self.assertEqual(result.query, "Насос шт.")
        self.assertEqual(
            [candidate.url for candidate in result.candidates],
            [
                "https://shop1.example/1",
                "https://shop2.example/2",
                "https://shop3.example/3",
            ],
        )
        self.assertEqual([candidate.rank for candidate in result.candidates], [0, 1, 2])
        self.assertEqual(
            [candidate.title for candidate in result.candidates],
            ["Насос 1", "Насос 2", "Насос 3"],
        )

    def test_empty_results_is_no_results_not_error(self) -> None:
        transport = FakeTransport(response=make_response(payload={"results": []}))
        result = self._provider(transport).search(make_query())
        self.assertIs(result.status, SearchStatus.NO_RESULTS)
        self.assertEqual(result.candidates, ())

    def test_missing_results_key_is_configuration_error(self) -> None:
        transport = FakeTransport(response=make_response(payload={}))
        with self.assertRaises(SearchProviderError) as context:
            self._provider(transport).search(make_query())
        self.assertIs(context.exception.failure, SearchFailure.CONFIGURATION)

    def test_results_not_a_list_is_configuration_error(self) -> None:
        transport = FakeTransport(response=make_response(payload={"results": {}}))
        with self.assertRaises(SearchProviderError) as context:
            self._provider(transport).search(make_query())
        self.assertIs(context.exception.failure, SearchFailure.CONFIGURATION)

    def test_invalid_json_is_temporary_error(self) -> None:
        transport = FakeTransport(response=make_response(body=b"not json"))
        with self.assertRaises(SearchProviderError) as context:
            self._provider(transport).search(make_query())
        self.assertIs(context.exception.failure, SearchFailure.TEMPORARY)

    def test_http_429_is_rate_limited(self) -> None:
        transport = FakeTransport(response=make_response(status=429))
        with self.assertRaises(SearchProviderError) as context:
            self._provider(transport).search(make_query())
        self.assertIs(context.exception.failure, SearchFailure.RATE_LIMITED)

    def test_http_500_is_temporary(self) -> None:
        transport = FakeTransport(response=make_response(status=500))
        with self.assertRaises(SearchProviderError) as context:
            self._provider(transport).search(make_query())
        self.assertIs(context.exception.failure, SearchFailure.TEMPORARY)

    def test_http_403_is_configuration(self) -> None:
        transport = FakeTransport(response=make_response(status=403))
        with self.assertRaises(SearchProviderError) as context:
            self._provider(transport).search(make_query())
        self.assertIs(context.exception.failure, SearchFailure.CONFIGURATION)

    def test_http_400_is_permanent(self) -> None:
        transport = FakeTransport(response=make_response(status=400))
        with self.assertRaises(SearchProviderError) as context:
            self._provider(transport).search(make_query())
        self.assertIs(context.exception.failure, SearchFailure.PERMANENT)

    def test_timeout_is_temporary(self) -> None:
        transport = FakeTransport(error=TransportTimeout("timeout"))
        with self.assertRaises(SearchProviderError) as context:
            self._provider(transport).search(make_query())
        self.assertIs(context.exception.failure, SearchFailure.TEMPORARY)
        self.assertTrue(context.exception.retryable)

    def test_connection_error_is_temporary(self) -> None:
        transport = FakeTransport(error=TransportConnectionError("connection failed"))
        with self.assertRaises(SearchProviderError) as context:
            self._provider(transport).search(make_query())
        self.assertIs(context.exception.failure, SearchFailure.TEMPORARY)

    def test_query_contains_only_name_and_unit(self) -> None:
        transport = FakeTransport(response=make_response(payload={"results": []}))
        query = make_query(name="Насос", unit="шт.")
        self._provider(transport).search(query)

        call = transport.calls[0]
        self.assertEqual(
            call["params"],
            {
                "q": "Насос шт.",
                "format": "json",
                "language": "uk-UA",
                "pageno": "1",
            },
        )
        self.assertEqual(call["url"], "http://127.0.0.1:8888/search")
        self.assertEqual(call["headers"]["Accept"], "application/json")

    def test_query_length_is_bounded(self) -> None:
        transport = FakeTransport(response=make_response(payload={"results": []}))
        query = make_query(name="Насос " * 50, unit="шт.")
        result = self._provider(transport, max_query_length=20).search(query)
        self.assertEqual(len(result.query), 20)
        self.assertEqual(len(transport.calls[0]["params"]["q"]), 20)

    def test_result_limit_is_bounded_by_query_and_settings(self) -> None:
        payload = {
            "results": [
                {"url": f"https://shop.example/{index}"} for index in range(5)
            ]
        }
        transport = FakeTransport(response=make_response(payload=payload))
        result = self._provider(transport).search(make_query(limit=2))
        self.assertEqual(len(result.candidates), 2)

        transport = FakeTransport(response=make_response(payload=payload))
        result = self._provider(transport, max_results=1).search(make_query(limit=10))
        self.assertEqual(len(result.candidates), 1)

    def test_title_and_snippet_are_truncated(self) -> None:
        payload = {
            "results": [
                {
                    "url": "https://shop.example/1",
                    "title": "T" * 50,
                    "content": "S" * 50,
                }
            ]
        }
        transport = FakeTransport(response=make_response(payload=payload))
        result = self._provider(
            transport, max_title_length=3, max_snippet_length=4
        ).search(make_query())
        self.assertEqual(result.candidates[0].title, "TTT")
        self.assertEqual(result.candidates[0].snippet, "SSSS")

    def test_invalid_url_schemes_are_skipped(self) -> None:
        payload = {
            "results": [
                {"url": "file:///etc/passwd"},
                {"url": "ftp://example.com/file"},
                {"url": "javascript:alert(1)"},
                {"url": "data:text/plain,hello"},
            ]
        }
        transport = FakeTransport(response=make_response(payload=payload))
        result = self._provider(transport).search(make_query())
        self.assertIs(result.status, SearchStatus.NO_RESULTS)
        self.assertEqual(result.candidates, ())

    def test_public_candidate_url_is_accepted(self) -> None:
        payload = {"results": [{"url": "https://shop.example/offer"}]}
        transport = FakeTransport(response=make_response(payload=payload))
        result = self._provider(transport).search(make_query())
        self.assertEqual(result.candidates[0].url, "https://shop.example/offer")

    def test_loopback_and_private_candidate_urls_are_skipped(self) -> None:
        payload = {
            "results": [
                {"url": "http://127.0.0.1/"},
                {"url": "http://localhost/"},
                {"url": "http://192.168.1.1/"},
                {"url": "http://10.0.0.1/"},
                {"url": "http://[::1]/"},
            ]
        }
        transport = FakeTransport(response=make_response(payload=payload))
        result = self._provider(transport).search(make_query())
        self.assertIs(result.status, SearchStatus.NO_RESULTS)

    def test_credentials_in_candidate_url_are_skipped(self) -> None:
        payload = {"results": [{"url": "https://user:pass@example.com/"}]}
        transport = FakeTransport(response=make_response(payload=payload))
        result = self._provider(transport).search(make_query())
        self.assertIs(result.status, SearchStatus.NO_RESULTS)

    def test_malformed_result_objects_are_skipped(self) -> None:
        payload = {
            "results": [
                "not a dict",
                {"title": "missing url"},
                {"url": 123},
                {"url": "https://ok.example/"},
            ]
        }
        transport = FakeTransport(response=make_response(payload=payload))
        result = self._provider(transport).search(make_query())
        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(result.candidates[0].url, "https://ok.example/")
        self.assertEqual(result.candidates[0].rank, 3)

    def test_all_invalid_results_is_no_results(self) -> None:
        payload = {"results": ["bad", {"url": "ftp://example.com"}]}
        transport = FakeTransport(response=make_response(payload=payload))
        result = self._provider(transport).search(make_query())
        self.assertIs(result.status, SearchStatus.NO_RESULTS)

    def test_candidate_url_validation_uses_url_policy(self) -> None:
        try:
            from src.ai_assistance.infrastructure.network.url_policy import (
                validate_url_syntax,
            )

            validate_url_syntax("http://127.0.0.1/")
        except UnsafeUrlError:
            pass
        else:
            self.fail("url policy should reject loopback")


if __name__ == "__main__":
    unittest.main()