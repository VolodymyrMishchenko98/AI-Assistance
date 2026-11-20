from .config import Settings
from .modules.price_checker.application.services import PriceCheckerApplication
from .modules.price_checker.infrastructure.persistence.sqlite import (
    SqliteDatabase,
    SqliteUnitOfWork,
)
from .modules.price_checker.infrastructure.xlsx_reader import OpenpyxlXlsxReader
from .presentation.http.server import serve


def build_application(settings: Settings | None = None) -> tuple[PriceCheckerApplication, SqliteDatabase]:
    resolved = settings or Settings.from_environment()
    database = SqliteDatabase(resolved.database_path)
    return (
        PriceCheckerApplication(
            lambda: SqliteUnitOfWork(database),
            OpenpyxlXlsxReader(),
            write_unit_of_work=lambda: SqliteUnitOfWork(database, immediate=True),
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

