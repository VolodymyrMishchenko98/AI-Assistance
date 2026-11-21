from .config import Settings
from .infrastructure.network.provider_endpoint import validate_provider_base_url
from .infrastructure.search.searxng import SearXNGProvider
from .infrastructure.search.transport import SearchTransport
from .modules.price_checker.application.services import PriceCheckerApplication
from .modules.price_checker.infrastructure.persistence.sqlite import (
    SqliteDatabase,
    SqliteUnitOfWork,
)
from .modules.price_checker.infrastructure.xlsx_reader import OpenpyxlXlsxReader
from .presentation.http.server import serve


def build_search_provider(
    settings: Settings, transport: SearchTransport | None
) -> SearXNGProvider | None:
    if settings.search_provider is None:
        return None
    if not settings.search_base_url:
        raise ValueError("search base URL is required")
    base_url = validate_provider_base_url(settings.search_base_url)
    if transport is None:
        raise ValueError("search transport is not configured")
    return SearXNGProvider(
        base_url=base_url,
        transport=transport,
        timeout=settings.search_timeout_seconds,
        max_results=settings.search_max_results,
        max_query_length=settings.search_max_query_length,
        max_title_length=settings.search_max_title_length,
        max_snippet_length=settings.search_max_snippet_length,
    )


def build_application(
    settings: Settings | None = None,
    *,
    transport: SearchTransport | None = None,
) -> tuple[PriceCheckerApplication, SqliteDatabase]:
    resolved = settings or Settings.from_environment()
    search_provider = build_search_provider(resolved, transport)
    database = SqliteDatabase(resolved.database_path)
    return (
        PriceCheckerApplication(
            lambda: SqliteUnitOfWork(database),
            OpenpyxlXlsxReader(),
            write_unit_of_work=lambda: SqliteUnitOfWork(database, immediate=True),
            search_provider=search_provider,
        ),
        database,
    )


def main() -> None:
    settings = Settings.from_environment()
    application, database = build_application(settings)
    try:
        serve(application, settings.host, settings.port)
    finally:
        database.close()


if __name__ == "__main__":
    main()

