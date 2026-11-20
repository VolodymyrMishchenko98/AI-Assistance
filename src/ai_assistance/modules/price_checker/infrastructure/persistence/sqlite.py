import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterator
from uuid import UUID

from ...application.ports import PriceCheckerUnitOfWork
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
                if connection.execute(
                    "SELECT 1 FROM schema_migrations WHERE name = ?", (migration.name,)
                ).fetchone():
                    continue
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
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        assert self._transaction is not None
        self._transaction.__exit__(exc_type, exc_value, traceback)

