import unittest
from datetime import datetime, timezone
from uuid import uuid4

from src.ai_assistance.modules.price_checker.application.search_dto import (
    SearchCandidate,
    SearchFailure,
    SearchQuery,
    SearchResult,
    SearchStatus,
)
from src.ai_assistance.modules.price_checker.application.search_ports import (
    SearchProvider,
    SearchProviderError,
)


class SearchContractTests(unittest.TestCase):
    def _query(self, **overrides) -> SearchQuery:
        values = {
            "item_id": uuid4(),
            "run_id": uuid4(),
            "name": "Насос",
            "unit": "шт.",
            "language": "uk-UA",
            "limit": 10,
            "correlation_id": uuid4(),
        }
        values.update(overrides)
        return SearchQuery(**values)

    def test_query_accepts_name_and_unit_only(self) -> None:
        query = self._query()
        self.assertEqual(query.name, "Насос")
        self.assertEqual(query.unit, "шт.")
        self.assertFalse(hasattr(query, "description"))
        self.assertFalse(hasattr(query, "text"))

    def test_query_rejects_empty_name(self) -> None:
        with self.assertRaises(ValueError):
            self._query(name="   ")

    def test_query_rejects_overlong_name(self) -> None:
        with self.assertRaises(ValueError):
            self._query(name="x" * 501)

    def test_query_rejects_invalid_unit(self) -> None:
        with self.assertRaises(ValueError):
            self._query(unit=123)

    def test_query_rejects_empty_language(self) -> None:
        with self.assertRaises(ValueError):
            self._query(language="")

    def test_query_rejects_limit_out_of_range(self) -> None:
        for limit in (0, -1, 101):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                self._query(limit=limit)

    def test_search_failure_values_are_stable(self) -> None:
        self.assertEqual(SearchFailure.RATE_LIMITED.value, "rate_limited")
        self.assertEqual(SearchFailure.TEMPORARY.value, "temporary")
        self.assertEqual(SearchFailure.PERMANENT.value, "permanent")
        self.assertEqual(SearchFailure.CONFIGURATION.value, "configuration")

    def test_search_status_values_are_stable(self) -> None:
        self.assertEqual(SearchStatus.OK.value, "ok")
        self.assertEqual(SearchStatus.NO_RESULTS.value, "no_results")

    def test_provider_error_retryable_mapping(self) -> None:
        self.assertTrue(SearchProviderError(SearchFailure.RATE_LIMITED, "x").retryable)
        self.assertTrue(SearchProviderError(SearchFailure.TEMPORARY, "x").retryable)
        self.assertFalse(SearchProviderError(SearchFailure.PERMANENT, "x").retryable)
        self.assertFalse(SearchProviderError(SearchFailure.CONFIGURATION, "x").retryable)

    def test_provider_error_preserves_cause_and_message(self) -> None:
        cause = RuntimeError("boom")
        error = SearchProviderError(SearchFailure.TEMPORARY, "temporary failure")
        error.__cause__ = cause
        self.assertIs(error.__cause__, cause)
        self.assertEqual(error.message, "temporary failure")
        self.assertEqual(str(error), "temporary failure")

    def test_fake_provider_satisfies_contract(self) -> None:
        class FakeProvider:
            def search(self, query: SearchQuery) -> SearchResult:
                return SearchResult(
                    provider="fake",
                    status=SearchStatus.OK,
                    candidates=(
                        SearchCandidate(
                            url="https://example.com/offer",
                            title="Offer",
                            snippet="Snippet",
                            rank=0,
                            provider="fake",
                        ),
                    ),
                    searched_at=datetime.now(timezone.utc),
                    query=query.name,
                )

        provider: SearchProvider = FakeProvider()
        result = provider.search(self._query())
        self.assertEqual(result.status, SearchStatus.OK)
        self.assertEqual(result.candidates[0].url, "https://example.com/offer")


if __name__ == "__main__":
    unittest.main()