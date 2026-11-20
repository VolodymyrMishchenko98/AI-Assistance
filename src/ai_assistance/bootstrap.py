from .config import Settings
from .modules.price_checker.application.services import PriceCheckerApplication
from .modules.price_checker.infrastructure.persistence.sqlite import (
    SqliteDatabase,
    SqliteUnitOfWork,
)
from .presentation.http.server import serve


def build_application(settings: Settings | None = None) -> tuple[PriceCheckerApplication, SqliteDatabase]:
    resolved = settings or Settings.from_environment()
    database = SqliteDatabase(resolved.database_path)
    return (
        PriceCheckerApplication(
            lambda: SqliteUnitOfWork(database),
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

