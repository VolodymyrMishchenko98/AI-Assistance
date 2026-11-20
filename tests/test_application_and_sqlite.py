from datetime import datetime, timezone
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

from src.ai_assistance.modules.price_checker.application.commands import AddProcurementItemCommand, CreateReviewRunCommand
from src.ai_assistance.modules.price_checker.application.services import PriceCheckerApplication
from src.ai_assistance.modules.price_checker.domain.models import (
    AuditEvent,
    ProcurementItem,
    ProcurementItemStatus,
    ReviewRun,
    ReviewRunStatus,
    ValidationError,
)
from src.ai_assistance.modules.price_checker.infrastructure.persistence.sqlite import SqliteDatabase, SqliteUnitOfWork


class ApplicationSqliteTests(unittest.TestCase):
    def test_create_run_and_persist_item_and_audit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = SqliteDatabase(Path(directory) / "test.sqlite3")
            app = PriceCheckerApplication(lambda: SqliteUnitOfWork(database))
            run = app.create_review_run(CreateReviewRunCommand())
            item = app.add_procurement_item(AddProcurementItemCommand(run.id, "SSD", quantity=Decimal("2"), unit="шт."))
            self.assertEqual(item.name, "SSD")
            self.assertEqual(app.dashboard()[0].item_count, 1)
            with database.transaction() as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0], 2)
            database.close()

    def test_review_run_rolls_back_when_audit_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = SqliteDatabase(Path(directory) / "test.sqlite3")
            audit = Mock()
            audit.append.side_effect = RuntimeError("audit unavailable")

            class FailingUnitOfWork(SqliteUnitOfWork):
                def __enter__(self):
                    result = super().__enter__()
                    result.audit = audit
                    return result

            app = PriceCheckerApplication(lambda: FailingUnitOfWork(database))
            with self.assertRaises(RuntimeError):
                app.create_review_run(CreateReviewRunCommand())
            with database.transaction() as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM review_runs").fetchone()[0], 0)

    def test_item_rolls_back_when_audit_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = SqliteDatabase(Path(directory) / "test.sqlite3")
            app = PriceCheckerApplication(lambda: SqliteUnitOfWork(database))
            run = app.create_review_run(CreateReviewRunCommand())
            audit = Mock()
            audit.append.side_effect = RuntimeError("audit unavailable")

            class FailingUnitOfWork(SqliteUnitOfWork):
                def __enter__(self):
                    result = super().__enter__()
                    result.audit = audit
                    return result

            failing_app = PriceCheckerApplication(lambda: FailingUnitOfWork(database))
            with self.assertRaises(RuntimeError):
                failing_app.add_procurement_item(AddProcurementItemCommand(run.id, "SSD", quantity=Decimal("2"), unit="шт."))
            with database.transaction() as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM procurement_items").fetchone()[0], 0)

    def test_migrations_are_idempotent_and_failed_migration_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = SqliteDatabase(root / "test.sqlite3")
            database._migrate()
            with database.transaction() as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0], 1)
            bad_dir = root / "bad_migrations"
            bad_dir.mkdir()
            (bad_dir / "001_bad.sql").write_text(
                "CREATE TABLE should_rollback (id INTEGER);\nINVALID SQL;\n", encoding="utf-8"
            )
            with self.assertRaises(Exception):
                SqliteDatabase(root / "bad.sqlite3", bad_dir)
            connection = __import__("sqlite3").connect(root / "bad.sqlite3")
            try:
                self.assertIsNone(connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='should_rollback'"
                ).fetchone())
            finally:
                connection.close()

    def test_sqlite_reconstitutes_all_saved_state_machine_statuses(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = SqliteDatabase(Path(directory) / "test.sqlite3")
            run_ids = {}
            item_ids = {}
            now = datetime.now(timezone.utc)
            with SqliteUnitOfWork(database) as uow:
                for status in ReviewRunStatus:
                    run = ReviewRun._reconstitute(
                        id=uuid4(),
                        status=status,
                        created_at=now,
                        updated_at=now,
                    )
                    uow.runs.save(run)
                    run_ids[status] = run.id

                for status in ProcurementItemStatus:
                    item = ProcurementItem._reconstitute(
                        name="SSD",
                        run_id=run_ids[ReviewRunStatus.DRAFT],
                        status=status,
                    )
                    uow.items.save(item)
                    item_ids[status] = item.id

            with SqliteUnitOfWork(database) as uow:
                for status, run_id in run_ids.items():
                    restored = uow.runs.get(run_id)
                    self.assertIsNotNone(restored)
                    self.assertEqual(restored.status, status)

                restored_items = uow.items.list_for_run(run_ids[ReviewRunStatus.DRAFT])
                self.assertEqual(
                    {item.id: item.status for item in restored_items},
                    {item_id: status for status, item_id in item_ids.items()},
                )
            database.close()

    def test_sqlite_reconstitution_does_not_skip_domain_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = SqliteDatabase(Path(directory) / "test.sqlite3")
            run = ReviewRun()
            with SqliteUnitOfWork(database) as uow:
                uow.runs.save(run)

            with database.transaction() as connection:
                connection.execute("PRAGMA ignore_check_constraints = ON")
                connection.execute(
                    """INSERT INTO procurement_items
                    (id, run_id, name, description, quantity, unit, status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        str(uuid4()),
                        str(run.id),
                        "SSD",
                        None,
                        "0",
                        None,
                        ProcurementItemStatus.COMPLETED.value,
                        "2026-01-01T00:00:00+00:00",
                    ),
                )

            with SqliteUnitOfWork(database) as uow:
                with self.assertRaises(ValidationError):
                    uow.items.list_for_run(run.id)
            database.close()

    def test_sqlite_reconstitution_rejects_unknown_statuses(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = SqliteDatabase(Path(directory) / "test.sqlite3")
            run = ReviewRun()
            with SqliteUnitOfWork(database) as uow:
                uow.runs.save(run)
            with database.transaction() as connection:
                connection.execute("PRAGMA ignore_check_constraints = ON")
                connection.execute("UPDATE review_runs SET status = 'unknown' WHERE id = ?", (str(run.id),))
            with SqliteUnitOfWork(database) as uow:
                with self.assertRaises(ValidationError):
                    uow.runs.get(run.id)
            database.close()

    def test_audit_event_json_is_persisted_as_regular_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = SqliteDatabase(Path(directory) / "test.sqlite3")
            event = AuditEvent(
                "nested_data",
                "run",
                uuid4(),
                {"nested": {"values": [1, {"enabled": True}]}},
            )
            with SqliteUnitOfWork(database) as uow:
                uow.audit.append(event)
            with database.transaction() as connection:
                row = connection.execute(
                    "SELECT data_json FROM audit_events WHERE id = ?",
                    (str(event.id),),
                ).fetchone()
            self.assertEqual(
                json.loads(row["data_json"]),
                {"nested": {"values": [1, {"enabled": True}]}},
            )
            database.close()

