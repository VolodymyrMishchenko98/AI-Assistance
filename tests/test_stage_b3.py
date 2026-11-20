import sqlite3
import threading
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from src.ai_assistance.modules.price_checker.application.commands import (
    AddProcurementItemCommand,
    CancelReviewRunCommand,
    CompleteReviewRunCommand,
    CreateReviewRunCommand,
    StartReviewRunCommand,
)
from src.ai_assistance.modules.price_checker.application.errors import ConcurrencyError
from src.ai_assistance.modules.price_checker.application.services import PriceCheckerApplication
from src.ai_assistance.modules.price_checker.domain.models import (
    InvalidTransitionError,
    ProcurementItem,
    ProcurementItemStatus,
    ReviewRun,
    ReviewRunStatus,
    ValidationError,
)
from src.ai_assistance.modules.price_checker.infrastructure.persistence.sqlite import (
    SqliteDatabase,
    SqliteUnitOfWork,
    _is_lock_contention,
)


class _FakeConnection:
    def __init__(self, error: BaseException) -> None:
        self.error = error

    def execute(self, *args, **kwargs):
        raise self.error

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        pass


class StageB3Step1Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = SqliteDatabase(self.root / "step1.sqlite3")

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    def _seed_run_with_items(self) -> tuple[object, list[object]]:
        app = PriceCheckerApplication(lambda: SqliteUnitOfWork(self.db))
        run = app.create_review_run(CreateReviewRunCommand())
        first = app.add_procurement_item(AddProcurementItemCommand(
            run.id, "Насос", "220V", Decimal("2"), "шт."
        ))
        second = app.add_procurement_item(AddProcurementItemCommand(
            run.id, "Кабель", "мідь", Decimal("5"), "м"
        ))
        return run, [first, second]

    # ------------------------------------------------------------------
    # Migration 004
    # ------------------------------------------------------------------

    def test_migration_004_is_idempotent(self) -> None:
        columns = self._columns("procurement_items")
        self.assertIn("deleted_at", columns)
        self.db._migrate()
        self.db._migrate()
        self.assertIn("deleted_at", self._columns("procurement_items"))
        with self.db.transaction() as connection:
            applied = connection.execute(
                "SELECT COUNT(*) FROM schema_migrations WHERE name = '004_item_soft_delete.sql'"
            ).fetchone()[0]
        self.assertEqual(applied, 1)

    def test_migration_004_applies_exactly_once(self) -> None:
        with self.db.transaction() as connection:
            count = connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
        self.assertEqual(count, 4)

    def test_existing_rows_receive_null_deleted_at(self) -> None:
        _, items = self._seed_run_with_items()
        with self.db.transaction() as connection:
            for item in items:
                row = connection.execute(
                    "SELECT deleted_at FROM procurement_items WHERE id = ?", (str(item.id),)
                ).fetchone()
                self.assertIsNone(row["deleted_at"])

    def test_unmarked_migration_004_rejects_existing_deleted_at(self) -> None:
        with self.db.transaction() as connection:
            connection.execute(
                "DELETE FROM schema_migrations WHERE name = '004_item_soft_delete.sql'"
            )
        with self.assertRaisesRegex(RuntimeError, "несумісний стан схеми"):
            SqliteDatabase(self.root / "step1.sqlite3")

    def test_marked_migration_004_rejects_missing_deleted_at(self) -> None:
        path = self.root / "marked-missing-004.sqlite3"
        self._create_base_schema(path, with_deleted_at=False)
        with self.assertRaisesRegex(RuntimeError, "несумісний стан схеми"):
            SqliteDatabase(path)

    # ------------------------------------------------------------------
    # Soft delete visibility
    # ------------------------------------------------------------------

    def test_soft_deleted_item_absent_from_get_and_list(self) -> None:
        _, items = self._seed_run_with_items()
        target = items[0]
        with SqliteUnitOfWork(self.db) as uow:
            self.assertTrue(
                uow.items.soft_delete(target.run_id, target.id, ProcurementItemStatus.DRAFT)
            )
        with SqliteUnitOfWork(self.db) as uow:
            self.assertIsNone(uow.items.get(target.run_id, target.id))
            remaining = uow.items.list_for_run(target.run_id)
        self.assertEqual([item.id for item in remaining], [items[1].id])

    def test_get_returns_none_for_unknown_item(self) -> None:
        run, _ = self._seed_run_with_items()
        with SqliteUnitOfWork(self.db) as uow:
            self.assertIsNone(uow.items.get(run.id, uuid4()))

    def test_list_for_run_is_isolated_and_excludes_deleted(self) -> None:
        app = PriceCheckerApplication(lambda: SqliteUnitOfWork(self.db))
        run_a = app.create_review_run(CreateReviewRunCommand())
        run_b = app.create_review_run(CreateReviewRunCommand())
        a1 = app.add_procurement_item(AddProcurementItemCommand(
            run_a.id, "A1", quantity=Decimal("1"), unit="шт."
        ))
        app.add_procurement_item(AddProcurementItemCommand(
            run_a.id, "A2", quantity=Decimal("1"), unit="шт."
        ))
        app.add_procurement_item(AddProcurementItemCommand(
            run_b.id, "B1", quantity=Decimal("1"), unit="шт."
        ))
        with SqliteUnitOfWork(self.db) as uow:
            uow.items.soft_delete(run_a.id, a1.id, ProcurementItemStatus.DRAFT)
        with SqliteUnitOfWork(self.db) as uow:
            run_a_items = uow.items.list_for_run(run_a.id)
            run_b_items = uow.items.list_for_run(run_b.id)
        self.assertEqual([item.name for item in run_a_items], ["A2"])
        self.assertEqual([item.name for item in run_b_items], ["B1"])

    # ------------------------------------------------------------------
    # Repeated delete / status preservation
    # ------------------------------------------------------------------

    def test_repeated_soft_delete_returns_false_and_keeps_row(self) -> None:
        _, items = self._seed_run_with_items()
        target = items[0]
        with SqliteUnitOfWork(self.db) as uow:
            self.assertTrue(
                uow.items.soft_delete(target.run_id, target.id, ProcurementItemStatus.DRAFT)
            )
        with self.db.transaction() as connection:
            first = connection.execute(
                "SELECT deleted_at, name FROM procurement_items WHERE id = ?", (str(target.id),)
            ).fetchone()
        with SqliteUnitOfWork(self.db) as uow:
            self.assertFalse(
                uow.items.soft_delete(target.run_id, target.id, ProcurementItemStatus.DRAFT)
            )
        with self.db.transaction() as connection:
            second = connection.execute(
                "SELECT deleted_at, name FROM procurement_items WHERE id = ?", (str(target.id),)
            ).fetchone()
        self.assertEqual(first["deleted_at"], second["deleted_at"])
        self.assertEqual(first["name"], second["name"])

    def test_soft_delete_does_not_change_status(self) -> None:
        _, items = self._seed_run_with_items()
        target = items[0]
        with SqliteUnitOfWork(self.db) as uow:
            uow.items.soft_delete(target.run_id, target.id, ProcurementItemStatus.DRAFT)
        with self.db.transaction() as connection:
            status = connection.execute(
                "SELECT status FROM procurement_items WHERE id = ?", (str(target.id),)
            ).fetchone()["status"]
        self.assertEqual(status, ProcurementItemStatus.DRAFT.value)

    def test_soft_delete_is_allowed_for_draft_item(self) -> None:
        _, items = self._seed_run_with_items()
        target = items[0]
        with SqliteUnitOfWork(self.db) as uow:
            self.assertTrue(
                uow.items.soft_delete(target.run_id, target.id, ProcurementItemStatus.DRAFT)
            )
        with SqliteUnitOfWork(self.db) as uow:
            self.assertIsNone(uow.items.get(target.run_id, target.id))

    def test_soft_delete_is_rejected_for_non_draft_items(self) -> None:
        app = PriceCheckerApplication(lambda: SqliteUnitOfWork(self.db))
        run = app.create_review_run(CreateReviewRunCommand())
        for status in (
            ProcurementItemStatus.PENDING,
            ProcurementItemStatus.FAILED,
            ProcurementItemStatus.COMPLETED,
        ):
            with self.subTest(status=status):
                item = app.add_procurement_item(AddProcurementItemCommand(
                    run.id, f"item-{status.value}", quantity=Decimal("1"), unit="шт."
                ))
                with self.db.transaction() as connection:
                    connection.execute(
                        "UPDATE procurement_items SET status = ? WHERE id = ?",
                        (status.value, str(item.id)),
                    )
                with SqliteUnitOfWork(self.db) as uow:
                    self.assertFalse(
                        uow.items.soft_delete(run.id, item.id, status)
                    )
                    self.assertIsNotNone(uow.items.get(run.id, item.id))
                with self.db.transaction() as connection:
                    row = connection.execute(
                        "SELECT status, deleted_at FROM procurement_items WHERE id = ?",
                        (str(item.id),),
                    ).fetchone()
                self.assertEqual(row["status"], status.value)
                self.assertIsNone(row["deleted_at"])

    def test_soft_delete_wrong_run_returns_false(self) -> None:
        app = PriceCheckerApplication(lambda: SqliteUnitOfWork(self.db))
        run_a = app.create_review_run(CreateReviewRunCommand())
        run_b = app.create_review_run(CreateReviewRunCommand())
        item = app.add_procurement_item(AddProcurementItemCommand(
            run_a.id, "A1", quantity=Decimal("1"), unit="шт."
        ))
        with SqliteUnitOfWork(self.db) as uow:
            self.assertFalse(
                uow.items.soft_delete(run_b.id, item.id, ProcurementItemStatus.DRAFT)
            )

    # ------------------------------------------------------------------
    # update_fields
    # ------------------------------------------------------------------

    def test_update_fields_cannot_resurrect_deleted(self) -> None:
        _, items = self._seed_run_with_items()
        target = items[0]
        updated = ProcurementItem(
            name="Змінений", id=target.id, run_id=target.run_id,
            description="нове", quantity=Decimal("9"), unit="шт",
        )
        with SqliteUnitOfWork(self.db) as uow:
            uow.items.soft_delete(target.run_id, target.id, ProcurementItemStatus.DRAFT)
            self.assertFalse(uow.items.update_fields(updated))
        with SqliteUnitOfWork(self.db) as uow:
            self.assertIsNone(uow.items.get(target.run_id, target.id))
        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT name, deleted_at FROM procurement_items WHERE id = ?", (str(target.id),)
            ).fetchone()
        self.assertEqual(row["name"], "Насос")
        self.assertIsNotNone(row["deleted_at"])

    def test_update_fields_updates_active_item(self) -> None:
        _, items = self._seed_run_with_items()
        target = items[0]
        updated = ProcurementItem(
            name="  Насос GX  ", id=target.id, run_id=target.run_id,
            description="380V", quantity=Decimal("3"), unit=" шт ",
        )
        with SqliteUnitOfWork(self.db) as uow:
            self.assertTrue(uow.items.update_fields(updated))
        with SqliteUnitOfWork(self.db) as uow:
            stored = uow.items.get(target.run_id, target.id)
        self.assertIsNotNone(stored)
        self.assertEqual(stored.name, "Насос GX")
        self.assertEqual(stored.unit, "шт")
        self.assertEqual(stored.quantity, Decimal("3"))
        self.assertEqual(stored.description, "380V")

    def test_update_fields_rejects_item_from_another_run(self) -> None:
        app = PriceCheckerApplication(lambda: SqliteUnitOfWork(self.db))
        run_a = app.create_review_run(CreateReviewRunCommand())
        run_b = app.create_review_run(CreateReviewRunCommand())
        item = app.add_procurement_item(AddProcurementItemCommand(
            run_a.id, "A1", quantity=Decimal("1"), unit="шт."
        ))
        updated = ProcurementItem(
            name="Hacked", id=item.id, run_id=run_b.id,
            quantity=Decimal("1"), unit="шт",
        )
        with SqliteUnitOfWork(self.db) as uow:
            self.assertFalse(uow.items.update_fields(updated))
        with SqliteUnitOfWork(self.db) as uow:
            self.assertEqual(uow.items.get(run_a.id, item.id).name, "A1")

    def test_update_fields_ignores_non_draft_item(self) -> None:
        app = PriceCheckerApplication(lambda: SqliteUnitOfWork(self.db))
        run = app.create_review_run(CreateReviewRunCommand())
        item = app.add_procurement_item(AddProcurementItemCommand(
            run.id, "A1", quantity=Decimal("1"), unit="шт."
        ))
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE procurement_items SET status = ? WHERE id = ?",
                (ProcurementItemStatus.PENDING.value, str(item.id)),
            )
        updated = ProcurementItem(
            name="Changed", id=item.id, run_id=run.id,
            quantity=Decimal("1"), unit="шт",
        )
        with SqliteUnitOfWork(self.db) as uow:
            self.assertFalse(uow.items.update_fields(updated))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _columns(self, table: str) -> set[str]:
        with self.db.transaction() as connection:
            rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
        return {row["name"] for row in rows}

    def _create_base_schema(self, path: Path, *, with_deleted_at: bool) -> None:
        connection = sqlite3.connect(path)
        try:
            connection.execute(
                "CREATE TABLE schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            connection.execute(
                """CREATE TABLE review_runs (
                    id TEXT PRIMARY KEY, status TEXT NOT NULL, policy_version TEXT NOT NULL,
                    policy_required_fields TEXT NOT NULL, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL)"""
            )
            deleted = ", deleted_at TEXT" if with_deleted_at else ""
            connection.execute(
                f"""CREATE TABLE procurement_items (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, name TEXT NOT NULL,
                    description TEXT, quantity TEXT, unit TEXT, status TEXT NOT NULL,
                    created_at TEXT NOT NULL{deleted})"""
            )
            connection.execute(
                """CREATE TABLE audit_events (
                    id TEXT PRIMARY KEY, event_type TEXT NOT NULL, aggregate_type TEXT NOT NULL,
                    aggregate_id TEXT NOT NULL, data_json TEXT NOT NULL, occurred_at TEXT NOT NULL)"""
            )
            connection.execute(
                """CREATE TABLE import_operations (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, idempotency_key TEXT NOT NULL,
                    source_fingerprint TEXT NOT NULL, final_fingerprint TEXT, status TEXT NOT NULL,
                    selected_sheet TEXT, header_row INTEGER, sheets_json TEXT NOT NULL,
                    created_item_ids_json TEXT NOT NULL, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, expires_at TEXT NOT NULL, confirmed_at TEXT,
                    diagnostic TEXT)"""
            )
            connection.execute(
                """CREATE TABLE import_preview_rows (
                    id TEXT PRIMARY KEY, operation_id TEXT NOT NULL,
                    excel_row_number INTEGER NOT NULL, raw_values_json TEXT NOT NULL,
                    name TEXT, quantity TEXT, unit TEXT, description TEXT,
                    errors_json TEXT NOT NULL, warnings_json TEXT NOT NULL, status TEXT NOT NULL)"""
            )
            for name in (
                "001_initial.sql",
                "002_import_staging.sql",
                "003_import_diagnostics.sql",
                "004_item_soft_delete.sql",
            ):
                connection.execute(
                    "INSERT INTO schema_migrations(name, applied_at) VALUES (?, ?)",
                    (name, "2026-01-01T00:00:00+00:00"),
                )
            connection.commit()
        finally:
            connection.close()


class StageB3Step2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = SqliteDatabase(self.root / "step2.sqlite3")

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    def _app(self, write=None) -> PriceCheckerApplication:
        factory = write or (lambda: SqliteUnitOfWork(self.db, immediate=True))
        return PriceCheckerApplication(
            lambda: SqliteUnitOfWork(self.db),
            write_unit_of_work=factory,
        )

    def _run_with_items(self, count: int = 2):
        app = self._app()
        run = app.create_review_run(CreateReviewRunCommand())
        items = [
            app.add_procurement_item(AddProcurementItemCommand(
                run.id, f"item-{index}", quantity=Decimal("1"), unit="шт."
            ))
            for index in range(count)
        ]
        return app, run, items

    def _run_status(self, run_id) -> ReviewRunStatus:
        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT status FROM review_runs WHERE id = ?", (str(run_id),)
            ).fetchone()
        return ReviewRunStatus(row["status"])

    def _item_rows(self, run_id):
        with self.db.transaction() as connection:
            return connection.execute(
                "SELECT id, status, deleted_at FROM procurement_items "
                "WHERE run_id = ? ORDER BY created_at",
                (str(run_id),),
            ).fetchall()

    def _audit_counts(self) -> dict[str, int]:
        with self.db.transaction() as connection:
            rows = connection.execute(
                "SELECT event_type, COUNT(*) AS c FROM audit_events GROUP BY event_type"
            ).fetchall()
        return {row["event_type"]: row["c"] for row in rows}

    def _force_item_status(self, item_id, status: ProcurementItemStatus) -> None:
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE procurement_items SET status = ? WHERE id = ?",
                (status.value, str(item_id)),
            )

    def _complete_items(self, run_id) -> None:
        with SqliteUnitOfWork(self.db, immediate=True) as uow:
            for item in uow.items.list_for_run(run_id):
                item.transition_to(ProcurementItemStatus.COMPLETED)
                uow.items.update_status(
                    item.id, ProcurementItemStatus.PENDING, ProcurementItemStatus.COMPLETED
                )

    # ------------------------------------------------------------------
    # start_review_run
    # ------------------------------------------------------------------

    def test_start_succeeds_with_multiple_active_items(self) -> None:
        app, run, items = self._run_with_items(2)
        result = app.start_review_run(StartReviewRunCommand(run.id))
        self.assertEqual(result.status, ReviewRunStatus.RUNNING)
        self.assertEqual(result.item_count, 2)
        self.assertEqual(self._run_status(run.id), ReviewRunStatus.RUNNING)
        rows = self._item_rows(run.id)
        self.assertEqual({row["status"] for row in rows}, {ProcurementItemStatus.PENDING.value})
        self.assertEqual(self._audit_counts().get("review_run_started"), 1)

    def test_start_rejects_empty_run(self) -> None:
        app = self._app()
        run = app.create_review_run(CreateReviewRunCommand())
        baseline = self._audit_counts()
        with self.assertRaises(ValidationError):
            app.start_review_run(StartReviewRunCommand(run.id))
        self.assertEqual(self._run_status(run.id), ReviewRunStatus.DRAFT)
        self.assertEqual(self._audit_counts(), baseline)

    def test_start_rejects_non_draft_items(self) -> None:
        app, run, items = self._run_with_items(1)
        self._force_item_status(items[0].id, ProcurementItemStatus.PENDING)
        baseline = self._audit_counts()
        with self.assertRaises(InvalidTransitionError):
            app.start_review_run(StartReviewRunCommand(run.id))
        self.assertEqual(self._run_status(run.id), ReviewRunStatus.DRAFT)
        self.assertEqual(self._audit_counts(), baseline)

    def test_start_ignores_soft_deleted_items(self) -> None:
        app, run, items = self._run_with_items(2)
        with SqliteUnitOfWork(self.db, immediate=True) as uow:
            self.assertTrue(
                uow.items.soft_delete(run.id, items[0].id, ProcurementItemStatus.DRAFT)
            )
        result = app.start_review_run(StartReviewRunCommand(run.id))
        self.assertEqual(result.item_count, 1)
        rows = self._item_rows(run.id)
        self.assertEqual(len(rows), 2)
        active = [row for row in rows if row["deleted_at"] is None]
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["status"], ProcurementItemStatus.PENDING.value)

    # ------------------------------------------------------------------
    # complete_review_run
    # ------------------------------------------------------------------

    def test_complete_succeeds_when_all_items_completed(self) -> None:
        app, run, _ = self._run_with_items(2)
        app.start_review_run(StartReviewRunCommand(run.id))
        self._complete_items(run.id)
        result = app.complete_review_run(CompleteReviewRunCommand(run.id))
        self.assertEqual(result.status, ReviewRunStatus.COMPLETED)
        self.assertEqual(self._run_status(run.id), ReviewRunStatus.COMPLETED)
        self.assertEqual(self._audit_counts().get("review_run_completed"), 1)

    def test_complete_rejects_non_completed_items(self) -> None:
        for status in (
            ProcurementItemStatus.DRAFT,
            ProcurementItemStatus.PENDING,
            ProcurementItemStatus.FAILED,
        ):
            with self.subTest(status=status):
                with TemporaryDirectory() as directory:
                    db = SqliteDatabase(Path(directory) / "complete.sqlite3")
                    try:
                        app = PriceCheckerApplication(
                            lambda: SqliteUnitOfWork(db),
                            write_unit_of_work=lambda: SqliteUnitOfWork(db, immediate=True),
                        )
                        run = app.create_review_run(CreateReviewRunCommand())
                        item = app.add_procurement_item(AddProcurementItemCommand(
                            run.id, "item", quantity=Decimal("1"), unit="шт."
                        ))
                        with db.transaction() as connection:
                            connection.execute(
                                "UPDATE review_runs SET status = ? WHERE id = ?",
                                (ReviewRunStatus.RUNNING.value, str(run.id)),
                            )
                            connection.execute(
                                "UPDATE procurement_items SET status = ? WHERE id = ?",
                                (status.value, str(item.id)),
                            )
                        with self.assertRaises(ValidationError):
                            app.complete_review_run(CompleteReviewRunCommand(run.id))
                        with db.transaction() as connection:
                            stored = connection.execute(
                                "SELECT status FROM review_runs WHERE id = ?", (str(run.id),)
                            ).fetchone()["status"]
                        self.assertEqual(stored, ReviewRunStatus.RUNNING.value)
                    finally:
                        db.close()

    def test_complete_ignores_soft_deleted_items(self) -> None:
        app, run, items = self._run_with_items(2)
        with SqliteUnitOfWork(self.db, immediate=True) as uow:
            self.assertTrue(
                uow.items.soft_delete(run.id, items[0].id, ProcurementItemStatus.DRAFT)
            )
        app.start_review_run(StartReviewRunCommand(run.id))
        self._complete_items(run.id)
        result = app.complete_review_run(CompleteReviewRunCommand(run.id))
        self.assertEqual(result.status, ReviewRunStatus.COMPLETED)

    def test_complete_from_draft_is_rejected(self) -> None:
        app, run, _ = self._run_with_items(1)
        with self.assertRaises(InvalidTransitionError):
            app.complete_review_run(CompleteReviewRunCommand(run.id))
        self.assertEqual(self._run_status(run.id), ReviewRunStatus.DRAFT)

    # ------------------------------------------------------------------
    # cancel_review_run
    # ------------------------------------------------------------------

    def test_cancel_from_draft(self) -> None:
        app = self._app()
        run = app.create_review_run(CreateReviewRunCommand())
        app.add_procurement_item(AddProcurementItemCommand(
            run.id, "item", quantity=Decimal("1"), unit="шт."
        ))
        result = app.cancel_review_run(CancelReviewRunCommand(run.id))
        self.assertEqual(result.status, ReviewRunStatus.CANCELLED)
        self.assertEqual(self._audit_counts().get("review_run_cancelled"), 1)

    def test_cancel_from_running(self) -> None:
        app, run, _ = self._run_with_items(1)
        app.start_review_run(StartReviewRunCommand(run.id))
        result = app.cancel_review_run(CancelReviewRunCommand(run.id))
        self.assertEqual(result.status, ReviewRunStatus.CANCELLED)
        self.assertEqual(self._audit_counts().get("review_run_cancelled"), 1)

    def test_cancel_does_not_modify_item_statuses(self) -> None:
        app, run, _ = self._run_with_items(2)
        app.start_review_run(StartReviewRunCommand(run.id))
        app.cancel_review_run(CancelReviewRunCommand(run.id))
        rows = self._item_rows(run.id)
        self.assertEqual({row["status"] for row in rows}, {ProcurementItemStatus.PENDING.value})

    def test_invalid_transition_writes_no_audit(self) -> None:
        app, run, _ = self._run_with_items(1)
        app.cancel_review_run(CancelReviewRunCommand(run.id))
        counts = self._audit_counts()
        with self.assertRaises(InvalidTransitionError):
            app.cancel_review_run(CancelReviewRunCommand(run.id))
        self.assertEqual(self._audit_counts(), counts)

    # ------------------------------------------------------------------
    # rollback and conflict handling
    # ------------------------------------------------------------------

    def test_failed_item_transition_rolls_back_everything(self) -> None:
        class FailingItemStatusUow(SqliteUnitOfWork):
            def __enter__(self):
                result = super().__enter__()
                original = result.items.update_status
                state = {"calls": 0}

                def wrapped(item_id, expected, target):
                    state["calls"] += 1
                    if state["calls"] == 2:
                        return False
                    return original(item_id, expected, target)

                result.items.update_status = wrapped
                return result

        _, run, items = self._run_with_items(2)
        app = self._app(write=lambda: FailingItemStatusUow(self.db, immediate=True))
        baseline = self._audit_counts()
        with self.assertRaises(ConcurrencyError):
            app.start_review_run(StartReviewRunCommand(run.id))
        self.assertEqual(self._run_status(run.id), ReviewRunStatus.DRAFT)
        self.assertEqual(
            {row["status"] for row in self._item_rows(run.id)},
            {ProcurementItemStatus.DRAFT.value},
        )
        self.assertEqual(self._audit_counts(), baseline)

    def test_zero_rowcount_transition_raises_concurrency_error(self) -> None:
        class FailingRunTransitionUow(SqliteUnitOfWork):
            def __enter__(self):
                result = super().__enter__()
                result.runs.transition_status = lambda *args, **kwargs: False
                return result

        _, run, _ = self._run_with_items(1)
        app = self._app(write=lambda: FailingRunTransitionUow(self.db, immediate=True))
        baseline = self._audit_counts()
        with self.assertRaises(ConcurrencyError):
            app.start_review_run(StartReviewRunCommand(run.id))
        self.assertEqual(self._run_status(run.id), ReviewRunStatus.DRAFT)
        self.assertEqual(self._audit_counts(), baseline)

    def test_domain_transition_precedes_persistence(self) -> None:
        _, run, _ = self._run_with_items(1)
        order: list[str] = []
        original_transition = ReviewRun.transition_to

        def spy_transition(self, status):
            order.append("domain")
            return original_transition(self, status)

        class RecordingUow(SqliteUnitOfWork):
            def __enter__(self):
                result = super().__enter__()
                original = result.runs.transition_status

                def wrapped(*args, **kwargs):
                    order.append("persist")
                    return original(*args, **kwargs)

                result.runs.transition_status = wrapped
                return result

        app = PriceCheckerApplication(
            lambda: SqliteUnitOfWork(self.db),
            write_unit_of_work=lambda: RecordingUow(self.db, immediate=True),
        )
        with patch.object(ReviewRun, "transition_to", spy_transition):
            app.start_review_run(StartReviewRunCommand(run.id))
        self.assertEqual(order[:2], ["domain", "persist"])

    def test_stale_expected_status_is_a_controlled_conflict(self) -> None:
        _, run, items = self._run_with_items(1)
        now = datetime.now(timezone.utc)
        with SqliteUnitOfWork(self.db, immediate=True) as uow:
            self.assertFalse(
                uow.runs.transition_status(
                    run.id, ReviewRunStatus.RUNNING, ReviewRunStatus.COMPLETED, now
                )
            )
            self.assertFalse(
                uow.items.update_status(
                    items[0].id, ProcurementItemStatus.PENDING, ProcurementItemStatus.COMPLETED
                )
            )

    def test_services_do_not_perform_direct_status_sql(self) -> None:
        services = (
            Path(__file__).parents[1]
            / "src" / "ai_assistance" / "modules" / "price_checker"
            / "application" / "services.py"
        )
        text = services.read_text(encoding="utf-8")
        self.assertNotIn("UPDATE review_runs SET status", text)
        self.assertNotIn("UPDATE procurement_items SET status", text)

    # ------------------------------------------------------------------
    # concurrency with real connections and threads
    # ------------------------------------------------------------------

    def _run_threads(self, workers) -> None:
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=20)
        self.assertFalse([worker for worker in workers if worker.is_alive()])

    def test_two_concurrent_starts_only_one_wins(self) -> None:
        app, run, _ = self._run_with_items(2)
        barrier = threading.Barrier(2)
        succeeded = []
        failed = []

        def start() -> None:
            barrier.wait()
            try:
                succeeded.append(app.start_review_run(StartReviewRunCommand(run.id)))
            except (InvalidTransitionError, ConcurrencyError) as exc:
                failed.append(exc)

        self._run_threads([threading.Thread(target=start) for _ in range(2)])
        self.assertEqual(len(succeeded), 1)
        self.assertEqual(len(failed), 1)
        self.assertEqual(self._run_status(run.id), ReviewRunStatus.RUNNING)
        self.assertEqual(
            {row["status"] for row in self._item_rows(run.id)},
            {ProcurementItemStatus.PENDING.value},
        )
        self.assertEqual(self._audit_counts().get("review_run_started"), 1)

    def test_concurrent_start_and_cancel_are_consistent(self) -> None:
        app, run, _ = self._run_with_items(1)
        barrier = threading.Barrier(2)
        outcomes = []
        failed = []

        def start() -> None:
            barrier.wait()
            try:
                outcomes.append(("start", app.start_review_run(StartReviewRunCommand(run.id))))
            except (InvalidTransitionError, ConcurrencyError) as exc:
                failed.append(exc)

        def cancel() -> None:
            barrier.wait()
            try:
                outcomes.append(("cancel", app.cancel_review_run(CancelReviewRunCommand(run.id))))
            except (InvalidTransitionError, ConcurrencyError) as exc:
                failed.append(exc)

        self._run_threads([threading.Thread(target=start), threading.Thread(target=cancel)])
        counts = self._audit_counts()
        started = counts.get("review_run_started", 0)
        cancelled = counts.get("review_run_cancelled", 0)
        status = self._run_status(run.id)
        self.assertLessEqual(started, 1)
        self.assertEqual(cancelled, 1)
        self.assertIn(status, (ReviewRunStatus.CANCELLED, ReviewRunStatus.RUNNING))
        if status is ReviewRunStatus.CANCELLED:
            self.assertIn(started, (0, 1))
            expected_item_status = (
                ProcurementItemStatus.PENDING.value
                if started == 1
                else ProcurementItemStatus.DRAFT.value
            )
        else:
            self.assertEqual(started, 1)
            expected_item_status = ProcurementItemStatus.PENDING.value
        self.assertEqual(
            {row["status"] for row in self._item_rows(run.id)},
            {expected_item_status},
        )

    def test_concurrent_complete_and_cancel_are_consistent(self) -> None:
        app, run, _ = self._run_with_items(1)
        app.start_review_run(StartReviewRunCommand(run.id))
        self._complete_items(run.id)
        barrier = threading.Barrier(2)
        failed = []

        def complete() -> None:
            barrier.wait()
            try:
                app.complete_review_run(CompleteReviewRunCommand(run.id))
            except (InvalidTransitionError, ConcurrencyError) as exc:
                failed.append(exc)

        def cancel() -> None:
            barrier.wait()
            try:
                app.cancel_review_run(CancelReviewRunCommand(run.id))
            except (InvalidTransitionError, ConcurrencyError) as exc:
                failed.append(exc)

        self._run_threads([threading.Thread(target=complete), threading.Thread(target=cancel)])
        counts = self._audit_counts()
        completed = counts.get("review_run_completed", 0)
        cancelled = counts.get("review_run_cancelled", 0)
        status = self._run_status(run.id)
        self.assertEqual(completed + cancelled, 1)
        if completed == 1:
            self.assertEqual(status, ReviewRunStatus.COMPLETED)
        else:
            self.assertEqual(status, ReviewRunStatus.CANCELLED)

    def test_held_write_lock_maps_to_concurrency_error(self) -> None:
        _, run, _ = self._run_with_items(1)
        holder = sqlite3.connect(self.db.path, timeout=1.0)
        try:
            holder.execute("BEGIN IMMEDIATE")
            holder.execute(
                "UPDATE review_runs SET updated_at = updated_at WHERE id = ?", (str(run.id),)
            )
            app = self._app()
            with self.assertRaises(ConcurrencyError):
                app.start_review_run(StartReviewRunCommand(run.id))
            self.assertEqual(self._run_status(run.id), ReviewRunStatus.DRAFT)
        finally:
            holder.rollback()
            holder.close()


class StageB3Step2LockTests(unittest.TestCase):
    def _error(self, code=None, message: str = "sqlite error") -> sqlite3.OperationalError:
        exc = sqlite3.OperationalError(message)
        if code is not None:
            exc.sqlite_errorcode = code
        return exc

    def test_recognizes_base_busy_and_locked_codes(self) -> None:
        for code in (5, 6):
            with self.subTest(code=code):
                self.assertTrue(_is_lock_contention(self._error(code)))

    def test_recognizes_extended_busy_and_locked_codes(self) -> None:
        for code in (261, 517, 773, 262, 518):
            with self.subTest(code=code):
                self.assertTrue(_is_lock_contention(self._error(code)))

    def test_rejects_unrelated_sqlite_errors(self) -> None:
        for code in (0, 1, 8, 11, 14, 19):
            with self.subTest(code=code):
                self.assertFalse(_is_lock_contention(self._error(code)))

    def test_rejects_operational_error_without_error_code(self) -> None:
        exc = sqlite3.OperationalError("database is locked")
        self.assertFalse(_is_lock_contention(exc))

    def test_transaction_translates_lock_error_and_preserves_cause(self) -> None:
        cause = self._error(5, "database is locked")
        database = SqliteDatabase.__new__(SqliteDatabase)
        database._connect = lambda: _FakeConnection(cause)
        with self.assertRaises(ConcurrencyError) as context:
            with database.transaction(immediate=True):
                pass
        self.assertIs(context.exception.__cause__, cause)

    def test_transaction_keeps_unrelated_operational_error(self) -> None:
        error = self._error(1, "no such table: missing")
        database = SqliteDatabase.__new__(SqliteDatabase)
        database._connect = lambda: _FakeConnection(error)
        with self.assertRaises(sqlite3.OperationalError) as context:
            with database.transaction(immediate=True):
                pass
        self.assertIs(context.exception, error)
        self.assertNotIsInstance(context.exception, ConcurrencyError)


if __name__ == "__main__":
    unittest.main()