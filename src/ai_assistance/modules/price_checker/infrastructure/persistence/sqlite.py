import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Iterator
from uuid import UUID
from typing import Any

from ...application.ports import PriceCheckerUnitOfWork
from ...application.imports import (
    ImportOperationStatus,
    ImportPreviewDto,
    ImportRowDto,
    ImportRowStatus,
    ImportSheetDto,
)
from ...domain.models import (
    AuditEvent,
    ProcurementItem,
    ProcurementItemStatus,
    ReviewPolicySnapshot,
    ReviewRun,
    ReviewRunStatus,
    ValidationError,
)


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _json_value(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


class SqliteDatabase:
    def __init__(self, path: Path, migrations_dir: Path | None = None) -> None:
        self.path = path
        self.migrations_dir = migrations_dir or Path(__file__).with_name("migrations")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _migrate(self) -> None:
        connection = self._connect()
        try:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            connection.commit()
            for migration in sorted(self.migrations_dir.glob("*.sql")):
                marked = connection.execute(
                    "SELECT 1 FROM schema_migrations WHERE name = ?", (migration.name,)
                ).fetchone()
                required_columns = {
                    "review_runs": {"id", "status", "policy_version", "policy_required_fields", "created_at", "updated_at"},
                    "procurement_items": {"id", "run_id", "name", "description", "quantity", "unit", "status", "created_at"},
                    "audit_events": {"id", "event_type", "aggregate_type", "aggregate_id", "data_json", "occurred_at"},
                    "import_operations": {"id", "run_id", "idempotency_key", "source_fingerprint", "final_fingerprint", "status",
                                          "selected_sheet", "header_row", "sheets_json", "created_item_ids_json",
                                          "created_at", "updated_at", "expires_at", "confirmed_at"},
                    "import_preview_rows": {"id", "operation_id", "excel_row_number", "raw_values_json", "name",
                                            "quantity", "unit", "description", "errors_json", "warnings_json", "status"},
                }

                def table_exists(name: str) -> bool:
                    return connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
                    ).fetchone() is not None

                def table_columns(name: str) -> set[str]:
                    if not table_exists(name):
                        return set()
                    return {row["name"] for row in connection.execute(f"PRAGMA table_info({name})").fetchall()}

                def require_tables(tables: dict[str, set[str]]) -> None:
                    for table, required in tables.items():
                        if not table_exists(table):
                            raise RuntimeError(f"несумісний стан схеми: відсутня таблиця {table}")
                        missing = required - table_columns(table)
                        if missing:
                            raise RuntimeError(
                                f"несумісний стан схеми: у таблиці {table} відсутні колонки: {', '.join(sorted(missing))}"
                            )

                if marked:
                    if migration.name == "001_initial.sql":
                        require_tables({name: required_columns[name] for name in
                                        ("review_runs", "procurement_items", "audit_events")})
                    elif migration.name == "002_import_staging.sql":
                        require_tables({"import_operations": required_columns["import_operations"],
                                        "import_preview_rows": required_columns["import_preview_rows"]})
                    elif migration.name == "003_import_diagnostics.sql":
                        require_tables({"import_operations": required_columns["import_operations"] | {"diagnostic"}})
                    continue

                if migration.name == "002_import_staging.sql":
                    for table in ("import_operations", "import_preview_rows"):
                        if table_exists(table):
                            missing = required_columns[table] - table_columns(table)
                            if missing:
                                raise RuntimeError(
                                    f"несумісний стан схеми: таблиця {table} існує частково; "
                                    f"відсутні колонки: {', '.join(sorted(missing))}"
                                )
                elif migration.name == "001_initial.sql":
                    existing = [name for name in ("review_runs", "procurement_items", "audit_events") if table_exists(name)]
                    if existing:
                        require_tables({name: required_columns[name] for name in ("review_runs", "procurement_items", "audit_events")})
                elif migration.name == "003_import_diagnostics.sql":
                    if not table_exists("import_operations"):
                        raise RuntimeError("несумісний стан схеми: відсутня таблиця import_operations")
                    if "diagnostic" in table_columns("import_operations"):
                        raise RuntimeError("несумісний стан схеми: diagnostic вже існує, але миграція 003 не позначена")
                try:
                    connection.execute("BEGIN")
                    statement = ""
                    for line in migration.read_text(encoding="utf-8").splitlines(True):
                        statement += line
                        if sqlite3.complete_statement(statement):
                            connection.execute(statement)
                            statement = ""
                    if statement.strip():
                        connection.execute(statement)
                    connection.execute(
                        "INSERT INTO schema_migrations(name, applied_at) VALUES (?, ?)",
                        (migration.name, datetime.now(timezone.utc).isoformat()),
                    )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def close(self) -> None:
        """Connections belong to transactions and are closed by their owner."""


class _RunRepository:
    def __init__(self, database: SqliteDatabase) -> None:
        self.db = database

    def save(self, run: ReviewRun) -> None:
        self.db.execute(
            """INSERT INTO review_runs
            (id, status, policy_version, policy_required_fields, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (
                str(run.id), run.status.value, run.policy.version, json.dumps(run.policy.required_fields),
                run.created_at.isoformat(), run.updated_at.isoformat(),
            ),
        )

    def get(self, run_id: UUID) -> ReviewRun | None:
        row = self.db.execute("SELECT * FROM review_runs WHERE id = ?", (str(run_id),)).fetchone()
        if row is None:
            return None
        try:
            status = ReviewRunStatus(row["status"])
        except ValueError as exc:
            raise ValidationError("stored review run has an unknown status") from exc
        return ReviewRun._reconstitute(
            id=UUID(row["id"]), status=status,
            policy=ReviewPolicySnapshot(row["policy_version"], tuple(json.loads(row["policy_required_fields"]))),
            created_at=_dt(row["created_at"]), updated_at=_dt(row["updated_at"]),
        )

    def list(self) -> list[ReviewRun]:
        rows = self.db.execute("SELECT id FROM review_runs ORDER BY created_at DESC").fetchall()
        return [run for row in rows if (run := self.get(UUID(row["id"]))) is not None]


class _ItemRepository:
    def __init__(self, database: SqliteDatabase) -> None:
        self.db = database

    def save(self, item: ProcurementItem) -> None:
        self.db.execute(
            """INSERT INTO procurement_items
            (id, run_id, name, description, quantity, unit, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(item.id), str(item.run_id), item.name, item.description,
                str(item.quantity) if item.quantity is not None else None, item.unit,
                item.status.value, item.created_at.isoformat(),
            ),
        )

    def list_for_run(self, run_id: UUID) -> list[ProcurementItem]:
        rows = self.db.execute(
            "SELECT * FROM procurement_items WHERE run_id = ? ORDER BY created_at", (str(run_id),)
        ).fetchall()
        restored = []
        for row in rows:
            try:
                status = ProcurementItemStatus(row["status"])
            except ValueError as exc:
                raise ValidationError("stored procurement item has an unknown status") from exc
            restored.append(
                ProcurementItem._reconstitute(
                    id=UUID(row["id"]), run_id=UUID(row["run_id"]), name=row["name"],
                    description=row["description"],
                    quantity=Decimal(row["quantity"]) if row["quantity"] is not None else None,
                    unit=row["unit"], status=status, created_at=_dt(row["created_at"]),
                )
            )
        return restored


class _AuditRepository:
    def __init__(self, database: SqliteDatabase) -> None:
        self.db = database

    def append(self, event: AuditEvent) -> None:
        self.db.execute(
            """INSERT INTO audit_events
            (id, event_type, aggregate_type, aggregate_id, data_json, occurred_at)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (str(event.id), event.event_type, event.aggregate_type, str(event.aggregate_id),
             event.to_json(), event.occurred_at.isoformat()),
        )


class _ImportRepository:
    def __init__(self, database: sqlite3.Connection) -> None:
        self.db = database

    def create(self, preview: ImportPreviewDto) -> None:
        self.db.execute(
            """INSERT INTO import_operations
            (id, run_id, idempotency_key, source_fingerprint, final_fingerprint, status,
             selected_sheet, header_row, sheets_json, created_item_ids_json,
             created_at, updated_at, expires_at, confirmed_at, diagnostic)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(preview.id), str(preview.run_id), preview.idempotency_key,
                preview.source_fingerprint, preview.final_fingerprint, preview.status.value,
                preview.selected_sheet, preview.header_row,
                json.dumps([{"name": s.name, "max_row": s.max_row, "max_column": s.max_column} for s in preview.sheets]),
                json.dumps([str(item_id) for item_id in preview.created_item_ids]),
                preview.created_at.isoformat(), preview.created_at.isoformat(), preview.expires_at.isoformat(), None, preview.diagnostic,
            ),
        )
        for row in preview.rows:
            self._insert_row(preview.id, row)

    def _insert_row(self, preview_id: UUID, row: ImportRowDto) -> None:
        self.db.execute(
            """INSERT INTO import_preview_rows
            (id, operation_id, excel_row_number, raw_values_json, name, quantity, unit,
             description, errors_json, warnings_json, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(row.id), str(preview_id), row.excel_row_number,
                json.dumps(row.raw_values, default=_json_value, ensure_ascii=False), row.name,
                str(row.quantity) if row.quantity is not None else None, row.unit, row.description,
                json.dumps(row.errors, ensure_ascii=False), json.dumps(row.warnings, ensure_ascii=False), row.status.value,
            ),
        )

    def get(self, preview_id: UUID) -> ImportPreviewDto | None:
        operation = self.db.execute("SELECT * FROM import_operations WHERE id = ?", (str(preview_id),)).fetchone()
        if operation is None:
            return None
        rows = self.db.execute(
            "SELECT * FROM import_preview_rows WHERE operation_id = ? ORDER BY excel_row_number", (str(preview_id),)
        ).fetchall()
        return ImportPreviewDto(
            id=UUID(operation["id"]), run_id=UUID(operation["run_id"]),
            status=ImportOperationStatus(operation["status"]),
            sheets=tuple(ImportSheetDto(**sheet) for sheet in json.loads(operation["sheets_json"])),
            selected_sheet=operation["selected_sheet"], header_row=operation["header_row"],
            rows=tuple(self._row(row) for row in rows), source_fingerprint=operation["source_fingerprint"],
            final_fingerprint=operation["final_fingerprint"], idempotency_key=operation["idempotency_key"],
            created_at=_dt(operation["created_at"]), expires_at=_dt(operation["expires_at"]),
            diagnostic=operation["diagnostic"],
            created_item_ids=tuple(UUID(item_id) for item_id in json.loads(operation["created_item_ids_json"])),
        )

    def get_for_run(self, preview_id: UUID, run_id: UUID) -> ImportPreviewDto | None:
        operation = self.db.execute(
            "SELECT id FROM import_operations WHERE id=? AND run_id=?",
            (str(preview_id), str(run_id)),
        ).fetchone()
        return self.get(preview_id) if operation else None

    def find_by_key(self, run_id: UUID, idempotency_key: str) -> ImportPreviewDto | None:
        row = self.db.execute(
            "SELECT id FROM import_operations WHERE run_id=? AND idempotency_key=?",
            (str(run_id), idempotency_key),
        ).fetchone()
        return self.get(UUID(row["id"])) if row else None

    @staticmethod
    def _row(row: sqlite3.Row) -> ImportRowDto:
        from decimal import Decimal
        return ImportRowDto(
            id=UUID(row["id"]), excel_row_number=row["excel_row_number"],
            raw_values=json.loads(row["raw_values_json"]), name=row["name"],
            quantity=Decimal(row["quantity"]) if row["quantity"] is not None else None,
            unit=row["unit"], description=row["description"],
            errors=tuple(json.loads(row["errors_json"])), warnings=tuple(json.loads(row["warnings_json"])),
            status=ImportRowStatus(row["status"]),
        )

    def update_row(self, preview_id: UUID, row: ImportRowDto, expires_at: datetime, now: datetime) -> bool:
        cursor = self.db.execute(
            """UPDATE import_operations SET updated_at=?, expires_at=?, final_fingerprint=NULL
            WHERE id=? AND status='preview_ready' AND expires_at>?""",
            (expires_at.isoformat(), expires_at.isoformat(), str(preview_id), now.isoformat()),
        )
        if cursor.rowcount != 1:
            return False
        cursor = self.db.execute(
            """UPDATE import_preview_rows SET name=?, quantity=?, unit=?, description=?,
            errors_json=?, warnings_json=?, status=? WHERE id=? AND operation_id=?""",
            (row.name, str(row.quantity) if row.quantity is not None else None, row.unit, row.description,
             json.dumps(row.errors, ensure_ascii=False), json.dumps(row.warnings, ensure_ascii=False), row.status.value,
             str(row.id), str(preview_id)),
        )
        return cursor.rowcount == 1

    def exclude_row(self, preview_id: UUID, row_id: UUID, expires_at: datetime, now: datetime) -> bool:
        cursor = self.db.execute(
            "UPDATE import_operations SET updated_at=?, expires_at=?, final_fingerprint=NULL WHERE id=? AND status='preview_ready' AND expires_at>?",
            (expires_at.isoformat(), expires_at.isoformat(), str(preview_id), now.isoformat()),
        )
        if cursor.rowcount != 1:
            return False
        cursor = self.db.execute(
            "UPDATE import_preview_rows SET status='excluded' WHERE id=? AND operation_id=?",
            (str(row_id), str(preview_id)),
        )
        return cursor.rowcount == 1

    def save_final_fingerprint(self, preview_id: UUID, fingerprint: str, now: datetime) -> bool:
        cursor = self.db.execute(
            """UPDATE import_operations SET final_fingerprint=?
            WHERE id=? AND status='preview_ready' AND expires_at>?""",
            (fingerprint, str(preview_id), now.isoformat()),
        )
        return cursor.rowcount == 1

    def mark_confirmed(self, preview_id: UUID, item_ids: tuple[UUID, ...], when: datetime, final_fingerprint: str) -> bool:
        cursor = self.db.execute(
            """UPDATE import_operations SET status='confirmed', created_item_ids_json=?, confirmed_at=?, updated_at=?
            WHERE id=? AND status='preview_ready' AND expires_at>? AND final_fingerprint=?""",
            (json.dumps([str(item_id) for item_id in item_ids]), when.isoformat(), when.isoformat(),
             str(preview_id), when.isoformat(), final_fingerprint),
        )
        return cursor.rowcount == 1

    def mark_expired(self, preview_id: UUID) -> None:
        self.db.execute("UPDATE import_operations SET status='expired', updated_at=? WHERE id=?", (datetime.now(timezone.utc).isoformat(), str(preview_id)))

    def delete_expired(self, now: datetime) -> int:
        cursor = self.db.execute(
            "DELETE FROM import_operations WHERE status IN ('uploaded','preview_ready','failed') AND expires_at < ?",
            (now.isoformat(),),
        )
        return cursor.rowcount


class SqliteUnitOfWork(PriceCheckerUnitOfWork):
    def __init__(self, database: SqliteDatabase) -> None:
        self.database = database
        self._transaction = None

    def __enter__(self) -> "SqliteUnitOfWork":
        self._transaction = self.database.transaction()
        connection = self._transaction.__enter__()
        self.runs = _RunRepository(connection)
        self.items = _ItemRepository(connection)
        self.audit = _AuditRepository(connection)
        self.previews = _ImportRepository(connection)
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        assert self._transaction is not None
        self._transaction.__exit__(exc_type, exc_value, traceback)

