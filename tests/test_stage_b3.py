import http.client
import json
import re
import sqlite3
import threading
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlencode, urlsplit
from uuid import uuid4

from src.ai_assistance.modules.price_checker.application.commands import (
    AddProcurementItemCommand,
    CancelReviewRunCommand,
    CompleteReviewRunCommand,
    CreateReviewRunCommand,
    SetProcurementItemStatusCommand,
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
from src.ai_assistance.presentation.http.server import LocalRequestHandler


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


class StageB3Step3Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = SqliteDatabase(self.root / "step3-app.sqlite3")

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

    def _start(self, app, run):
        app.start_review_run(StartReviewRunCommand(run.id))
        return app.list_procurement_items(run.id)

    def _set(self, app, run, item, status):
        return app.set_procurement_item_status(
            SetProcurementItemStatusCommand(run.id, item.id, status)
        )

    def _item_row(self, item_id):
        with self.db.transaction() as connection:
            return connection.execute(
                "SELECT status FROM procurement_items WHERE id = ?", (str(item_id),)
            ).fetchone()

    def _audit(self):
        with self.db.transaction() as connection:
            return connection.execute(
                "SELECT event_type, data_json FROM audit_events ORDER BY occurred_at, id"
            ).fetchall()

    def _audit_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in self._audit():
            counts[row["event_type"]] = counts.get(row["event_type"], 0) + 1
        return counts

    # ------------------------------------------------------------------
    # item status transitions
    # ------------------------------------------------------------------

    def test_complete_item_transitions_pending_to_completed(self) -> None:
        app, run, items = self._run_with_items(1)
        self._start(app, run)
        result = self._set(app, run, items[0], ProcurementItemStatus.COMPLETED)
        self.assertEqual(result.status, ProcurementItemStatus.COMPLETED)
        self.assertEqual(self._item_row(items[0].id)["status"], ProcurementItemStatus.COMPLETED.value)
        self.assertEqual(self._audit_counts().get("procurement_item_completed"), 1)

    def test_fail_item_transitions_pending_to_failed(self) -> None:
        app, run, items = self._run_with_items(1)
        self._start(app, run)
        result = self._set(app, run, items[0], ProcurementItemStatus.FAILED)
        self.assertEqual(result.status, ProcurementItemStatus.FAILED)
        self.assertEqual(self._item_row(items[0].id)["status"], ProcurementItemStatus.FAILED.value)
        self.assertEqual(self._audit_counts().get("procurement_item_failed"), 1)

    def test_retry_failed_item_transitions_back_to_pending(self) -> None:
        app, run, items = self._run_with_items(1)
        self._start(app, run)
        self._set(app, run, items[0], ProcurementItemStatus.FAILED)
        result = self._set(app, run, items[0], ProcurementItemStatus.PENDING)
        self.assertEqual(result.status, ProcurementItemStatus.PENDING)
        self.assertEqual(self._item_row(items[0].id)["status"], ProcurementItemStatus.PENDING.value)
        counts = self._audit_counts()
        self.assertEqual(counts.get("procurement_item_failed"), 1)
        self.assertEqual(counts.get("procurement_item_retried"), 1)

    def test_completed_item_cannot_be_reopened(self) -> None:
        app, run, items = self._run_with_items(1)
        self._start(app, run)
        self._set(app, run, items[0], ProcurementItemStatus.COMPLETED)
        baseline = self._audit_counts()
        with self.assertRaises(InvalidTransitionError):
            self._set(app, run, items[0], ProcurementItemStatus.PENDING)
        self.assertEqual(self._item_row(items[0].id)["status"], ProcurementItemStatus.COMPLETED.value)
        self.assertEqual(self._audit_counts(), baseline)

    def test_failed_item_cannot_be_completed_directly(self) -> None:
        app, run, items = self._run_with_items(1)
        self._start(app, run)
        self._set(app, run, items[0], ProcurementItemStatus.FAILED)
        baseline = self._audit_counts()
        with self.assertRaises(InvalidTransitionError):
            self._set(app, run, items[0], ProcurementItemStatus.COMPLETED)
        self.assertEqual(self._item_row(items[0].id)["status"], ProcurementItemStatus.FAILED.value)
        self.assertEqual(self._audit_counts(), baseline)

    def test_pending_item_cannot_be_retried(self) -> None:
        app, run, items = self._run_with_items(1)
        self._start(app, run)
        baseline = self._audit_counts()
        with self.assertRaises(InvalidTransitionError):
            self._set(app, run, items[0], ProcurementItemStatus.PENDING)
        self.assertEqual(self._item_row(items[0].id)["status"], ProcurementItemStatus.PENDING.value)
        self.assertEqual(self._audit_counts(), baseline)

    def test_item_status_change_does_not_touch_other_items(self) -> None:
        app, run, items = self._run_with_items(2)
        self._start(app, run)
        self._set(app, run, items[0], ProcurementItemStatus.COMPLETED)
        self.assertEqual(self._item_row(items[1].id)["status"], ProcurementItemStatus.PENDING.value)

    # ------------------------------------------------------------------
    # guards
    # ------------------------------------------------------------------

    def test_item_status_change_requires_running_run(self) -> None:
        app, run, items = self._run_with_items(1)
        baseline = self._audit_counts()
        with self.assertRaises(InvalidTransitionError):
            self._set(app, run, items[0], ProcurementItemStatus.COMPLETED)
        self.assertEqual(self._item_row(items[0].id)["status"], ProcurementItemStatus.DRAFT.value)
        self.assertEqual(self._audit_counts(), baseline)

    def test_item_status_change_rejects_missing_item(self) -> None:
        app, run, _ = self._run_with_items(1)
        self._start(app, run)
        with self.assertRaises(LookupError):
            self._set(app, run, SimpleNamespace(id=uuid4()), ProcurementItemStatus.COMPLETED)

    def test_item_status_change_rejects_item_from_another_run(self) -> None:
        app = self._app()
        first = app.create_review_run(CreateReviewRunCommand())
        second = app.create_review_run(CreateReviewRunCommand())
        foreign = app.add_procurement_item(AddProcurementItemCommand(
            second.id, "foreign", quantity=Decimal("1"), unit="шт."
        ))
        own = app.add_procurement_item(AddProcurementItemCommand(
            first.id, "own", quantity=Decimal("1"), unit="шт."
        ))
        app.start_review_run(StartReviewRunCommand(first.id))
        with self.assertRaises(LookupError):
            self._set(app, first, foreign, ProcurementItemStatus.COMPLETED)
        self.assertEqual(self._item_row(own.id)["status"], ProcurementItemStatus.PENDING.value)
        self.assertEqual(self._item_row(foreign.id)["status"], ProcurementItemStatus.DRAFT.value)

    def test_item_status_change_rejects_soft_deleted_item(self) -> None:
        app, run, items = self._run_with_items(2)
        with SqliteUnitOfWork(self.db, immediate=True) as uow:
            self.assertTrue(uow.items.soft_delete(
                run.id, items[0].id, ProcurementItemStatus.DRAFT
            ))
        self._start(app, run)
        with self.assertRaises(LookupError):
            self._set(app, run, items[0], ProcurementItemStatus.COMPLETED)
        self.assertEqual(self._item_row(items[1].id)["status"], ProcurementItemStatus.PENDING.value)

    def test_item_status_change_rejects_terminal_run(self) -> None:
        app, run, items = self._run_with_items(1)
        self._start(app, run)
        self._set(app, run, items[0], ProcurementItemStatus.COMPLETED)
        app.complete_review_run(CompleteReviewRunCommand(run.id))
        baseline = self._audit_counts()
        with self.assertRaises(InvalidTransitionError):
            self._set(app, run, items[0], ProcurementItemStatus.FAILED)
        self.assertEqual(self._audit_counts(), baseline)

    # ------------------------------------------------------------------
    # audit payload and transactions
    # ------------------------------------------------------------------

    def test_audit_payload_uses_enum_value_strings(self) -> None:
        app, run, items = self._run_with_items(1)
        self._start(app, run)
        self._set(app, run, items[0], ProcurementItemStatus.COMPLETED)
        rows = [row for row in self._audit() if row["event_type"] == "procurement_item_completed"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            json.loads(rows[0]["data_json"]),
            {
                "run_id": str(run.id),
                "item_id": str(items[0].id),
                "from_status": "pending",
                "to_status": "completed",
            },
        )

    def test_audit_failure_rolls_back_item_status(self) -> None:
        class FailingAudit:
            def append(self, event):
                raise RuntimeError("audit unavailable")

        class FailingUow(SqliteUnitOfWork):
            def __enter__(self):
                result = super().__enter__()
                result.audit = FailingAudit()
                return result

        app, run, items = self._run_with_items(1)
        self._start(app, run)
        failing_app = PriceCheckerApplication(
            lambda: SqliteUnitOfWork(self.db),
            write_unit_of_work=lambda: FailingUow(self.db, immediate=True),
        )
        baseline = self._audit_counts()
        with self.assertRaises(RuntimeError):
            failing_app.set_procurement_item_status(SetProcurementItemStatusCommand(
                run.id, items[0].id, ProcurementItemStatus.COMPLETED
            ))
        self.assertEqual(self._item_row(items[0].id)["status"], ProcurementItemStatus.PENDING.value)
        self.assertEqual(self._audit_counts(), baseline)

    def test_stale_item_status_raises_concurrency_error(self) -> None:
        class FailingItemStatusUow(SqliteUnitOfWork):
            def __enter__(self):
                result = super().__enter__()
                result.items.update_status = lambda *args, **kwargs: False
                return result

        app, run, items = self._run_with_items(1)
        self._start(app, run)
        stale_app = PriceCheckerApplication(
            lambda: SqliteUnitOfWork(self.db),
            write_unit_of_work=lambda: FailingItemStatusUow(self.db, immediate=True),
        )
        baseline = self._audit_counts()
        with self.assertRaises(ConcurrencyError):
            stale_app.set_procurement_item_status(SetProcurementItemStatusCommand(
                run.id, items[0].id, ProcurementItemStatus.COMPLETED
            ))
        self.assertEqual(self._item_row(items[0].id)["status"], ProcurementItemStatus.PENDING.value)
        self.assertEqual(self._audit_counts(), baseline)

    def test_domain_transition_precedes_persistence(self) -> None:
        app, run, items = self._run_with_items(1)
        self._start(app, run)
        order: list[str] = []
        original_transition = ProcurementItem.transition_to

        def spy_transition(self, status):
            order.append("domain")
            return original_transition(self, status)

        class RecordingUow(SqliteUnitOfWork):
            def __enter__(self):
                result = super().__enter__()
                original = result.items.update_status

                def wrapped(*args, **kwargs):
                    order.append("persist")
                    return original(*args, **kwargs)

                result.items.update_status = wrapped
                return result

        recording_app = PriceCheckerApplication(
            lambda: SqliteUnitOfWork(self.db),
            write_unit_of_work=lambda: RecordingUow(self.db, immediate=True),
        )
        with patch.object(ProcurementItem, "transition_to", spy_transition):
            recording_app.set_procurement_item_status(SetProcurementItemStatusCommand(
                run.id, items[0].id, ProcurementItemStatus.COMPLETED
            ))
        self.assertEqual(order[:2], ["domain", "persist"])

    def test_item_status_change_writes_single_audit_event(self) -> None:
        app, run, items = self._run_with_items(1)
        self._start(app, run)
        before = len(self._audit())
        self._set(app, run, items[0], ProcurementItemStatus.FAILED)
        after = self._audit()
        self.assertEqual(len(after), before + 1)
        self.assertEqual(after[-1]["event_type"], "procurement_item_failed")

    def test_item_status_change_uses_write_unit_of_work(self) -> None:
        seen: list[bool] = []

        class RecordingUow(SqliteUnitOfWork):
            def __enter__(self):
                seen.append(self.immediate)
                return super().__enter__()

        app, run, items = self._run_with_items(1)
        self._start(app, run)
        recording_app = PriceCheckerApplication(
            lambda: SqliteUnitOfWork(self.db),
            write_unit_of_work=lambda: RecordingUow(self.db, immediate=True),
        )
        recording_app.set_procurement_item_status(SetProcurementItemStatusCommand(
            run.id, items[0].id, ProcurementItemStatus.COMPLETED
        ))
        self.assertTrue(seen)
        self.assertTrue(all(seen))


class StageB3Step3HttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = SqliteDatabase(self.root / "step3-http.sqlite3")
        self.app = PriceCheckerApplication(
            lambda: SqliteUnitOfWork(self.db),
            write_unit_of_work=lambda: SqliteUnitOfWork(self.db, immediate=True),
        )
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            type("Handler", (LocalRequestHandler,), {"application": self.app}),
        )
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.db.close()
        self.tmp.cleanup()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _request(self, method, path, *, cookie=None, body=None, content_type=None,
                 origin=None, host=None):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_address[1], timeout=10
        )
        headers = {}
        if cookie is not None:
            headers["Cookie"] = cookie
        if content_type is not None:
            headers["Content-Type"] = content_type
        if origin is not None:
            headers["Origin"] = origin
        if host is not None:
            headers["Host"] = host
        if body is not None:
            headers["Content-Length"] = str(len(body))
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def _post(self, path, data, cookie, token, *, origin=None, host=None,
              include_csrf=True, csrf=None):
        payload = {key: str(value) for key, value in data.items()}
        if include_csrf:
            payload["csrf_token"] = token if csrf is None else csrf
        body = urlencode(payload).encode("utf-8")
        return self._request(
            "POST", path, cookie=cookie, body=body,
            content_type="application/x-www-form-urlencoded",
            origin=self.base if origin is None else origin, host=host,
        )

    def _session(self):
        status, headers, body = self._request("GET", "/")
        self.assertEqual(status, 200)
        cookie = headers["Set-Cookie"].split(";")[0]
        token = re.search(r'name="csrf_token" value="([^"]+)"', body.decode("utf-8")).group(1)
        return cookie, token

    def _create_run(self, cookie, token):
        before = {run.id for run in self.app.dashboard()}
        status, _, _ = self._post("/runs", {}, cookie, token)
        self.assertEqual(status, 303)
        created = [run.id for run in self.app.dashboard() if run.id not in before]
        self.assertEqual(len(created), 1)
        return created[0]

    def _add_item(self, run_id, cookie, token, name="позиція"):
        status, _, _ = self._post(
            f"/runs/{run_id}/items",
            {"name": name, "quantity": "1", "unit": "шт.", "description": ""},
            cookie, token,
        )
        self.assertEqual(status, 303)
        return self.app.list_procurement_items(run_id)[-1]

    def _page(self, run_id):
        status, _, body = self._request("GET", f"/runs/{run_id}")
        self.assertEqual(status, 200)
        return body.decode("utf-8")

    def _run_status(self, run_id):
        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT status FROM review_runs WHERE id = ?", (str(run_id),)
            ).fetchone()
        return ReviewRunStatus(row["status"])

    def _item_status(self, item_id):
        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT status FROM procurement_items WHERE id = ?", (str(item_id),)
            ).fetchone()
        return ProcurementItemStatus(row["status"])

    def _audit_counts(self) -> dict[str, int]:
        with self.db.transaction() as connection:
            rows = connection.execute(
                "SELECT event_type, COUNT(*) AS c FROM audit_events GROUP BY event_type"
            ).fetchall()
        return {row["event_type"]: row["c"] for row in rows}

    # ------------------------------------------------------------------
    # rendering
    # ------------------------------------------------------------------

    def test_run_page_shows_status_and_only_valid_actions(self) -> None:
        cookie, token = self._session()
        run_id = self._create_run(cookie, token)
        item = self._add_item(run_id, cookie, token)
        draft = self._page(run_id)
        self.assertIn("Статус: Чернетка", draft)
        self.assertIn("Почати перевірку", draft)
        self.assertIn("Скасувати перевірку", draft)
        self.assertNotIn("Завершити перевірку", draft)
        self.assertIn("Додати закупівельну позицію", draft)
        self.assertIn("Імпорт XLSX", draft)

        self.assertEqual(self._post(f"/runs/{run_id}/start", {}, cookie, token)[0], 303)
        running = self._page(run_id)
        self.assertIn("Статус: Виконується", running)
        self.assertIn("Завершити перевірку", running)
        self.assertIn("Позначити виконаною", running)
        self.assertIn("Позначити помилкою", running)
        self.assertNotIn("Почати перевірку", running)
        self.assertNotIn("Додати закупівельну позицію", running)
        self.assertNotIn("Імпорт XLSX", running)

        self.assertEqual(
            self._post(f"/runs/{run_id}/items/{item.id}/complete", {}, cookie, token)[0], 303
        )
        after_item = self._page(run_id)
        self.assertNotIn("Позначити виконаною", after_item)
        self.assertNotIn("Позначити помилкою", after_item)

        self.assertEqual(self._post(f"/runs/{run_id}/complete", {}, cookie, token)[0], 303)
        completed = self._page(run_id)
        self.assertIn("Статус: Завершено", completed)
        self.assertNotIn("Завершити перевірку", completed)
        self.assertNotIn("Скасувати перевірку", completed)
        self.assertNotIn("Повторити", completed)

    def test_empty_run_page_explains_start_requirement(self) -> None:
        cookie, token = self._session()
        run_id = self._create_run(cookie, token)
        page = self._page(run_id)
        self.assertIn("додайте хоча б одну активну позицію", page)
        self.assertIn("<button disabled>Почати перевірку</button>", page)

    def test_each_rendered_form_has_exactly_one_csrf_token(self) -> None:
        cookie, token = self._session()
        run_id = self._create_run(cookie, token)
        self._add_item(run_id, cookie, token)
        for page in (self._page(run_id),):
            forms = re.findall(r"<form\b.*?</form>", page, re.DOTALL)
            self.assertGreaterEqual(len(forms), 3)
            for form in forms:
                self.assertEqual(form.count('name="csrf_token"'), 1)

        self.assertEqual(self._post(f"/runs/{run_id}/start", {}, cookie, token)[0], 303)
        forms = re.findall(r"<form\b.*?</form>", self._page(run_id), re.DOTALL)
        self.assertGreaterEqual(len(forms), 2)
        for form in forms:
            self.assertEqual(form.count('name="csrf_token"'), 1)

    # ------------------------------------------------------------------
    # lifecycle actions
    # ------------------------------------------------------------------

    def test_start_transitions_all_active_items_atomically(self) -> None:
        cookie, token = self._session()
        run_id = self._create_run(cookie, token)
        first = self._add_item(run_id, cookie, token, "перша")
        second = self._add_item(run_id, cookie, token, "друга")
        self.assertEqual(self._post(f"/runs/{run_id}/start", {}, cookie, token)[0], 303)
        self.assertEqual(self._run_status(run_id), ReviewRunStatus.RUNNING)
        self.assertEqual(self._item_status(first.id), ProcurementItemStatus.PENDING)
        self.assertEqual(self._item_status(second.id), ProcurementItemStatus.PENDING)
        self.assertEqual(self._audit_counts().get("review_run_started"), 1)

    def test_start_without_items_is_rejected(self) -> None:
        cookie, token = self._session()
        run_id = self._create_run(cookie, token)
        baseline = self._audit_counts()
        self.assertEqual(self._post(f"/runs/{run_id}/start", {}, cookie, token)[0], 400)
        self.assertEqual(self._run_status(run_id), ReviewRunStatus.DRAFT)
        self.assertEqual(self._audit_counts(), baseline)

    def test_item_complete_fail_and_retry_via_http(self) -> None:
        cookie, token = self._session()
        run_id = self._create_run(cookie, token)
        first = self._add_item(run_id, cookie, token, "перша")
        second = self._add_item(run_id, cookie, token, "друга")
        self.assertEqual(self._post(f"/runs/{run_id}/start", {}, cookie, token)[0], 303)

        self.assertEqual(
            self._post(f"/runs/{run_id}/items/{first.id}/complete", {}, cookie, token)[0], 303
        )
        self.assertEqual(self._item_status(first.id), ProcurementItemStatus.COMPLETED)

        self.assertEqual(
            self._post(f"/runs/{run_id}/items/{second.id}/fail", {}, cookie, token)[0], 303
        )
        self.assertEqual(self._item_status(second.id), ProcurementItemStatus.FAILED)
        failed_page = self._page(run_id)
        self.assertIn("Повторити", failed_page)
        self.assertNotIn("Позначити виконаною", failed_page)
        self.assertNotIn("Позначити помилкою", failed_page)

        self.assertEqual(
            self._post(f"/runs/{run_id}/items/{second.id}/retry", {}, cookie, token)[0], 303
        )
        self.assertEqual(self._item_status(second.id), ProcurementItemStatus.PENDING)
        retried_page = self._page(run_id)
        self.assertIn("Позначити виконаною", retried_page)
        self.assertNotIn("Повторити", retried_page)

        counts = self._audit_counts()
        self.assertEqual(counts.get("procurement_item_completed"), 1)
        self.assertEqual(counts.get("procurement_item_failed"), 1)
        self.assertEqual(counts.get("procurement_item_retried"), 1)

    def test_complete_run_requires_all_items_completed(self) -> None:
        cookie, token = self._session()
        run_id = self._create_run(cookie, token)
        first = self._add_item(run_id, cookie, token, "перша")
        second = self._add_item(run_id, cookie, token, "друга")
        self.assertEqual(self._post(f"/runs/{run_id}/start", {}, cookie, token)[0], 303)
        self.assertEqual(
            self._post(f"/runs/{run_id}/items/{first.id}/complete", {}, cookie, token)[0], 303
        )
        self.assertEqual(self._post(f"/runs/{run_id}/complete", {}, cookie, token)[0], 400)
        self.assertEqual(self._run_status(run_id), ReviewRunStatus.RUNNING)

        self.assertEqual(
            self._post(f"/runs/{run_id}/items/{second.id}/complete", {}, cookie, token)[0], 303
        )
        self.assertEqual(self._post(f"/runs/{run_id}/complete", {}, cookie, token)[0], 303)
        self.assertEqual(self._run_status(run_id), ReviewRunStatus.COMPLETED)
        self.assertEqual(self._audit_counts().get("review_run_completed"), 1)

    def test_cancel_draft_run(self) -> None:
        cookie, token = self._session()
        run_id = self._create_run(cookie, token)
        item = self._add_item(run_id, cookie, token)
        self.assertEqual(self._post(f"/runs/{run_id}/cancel", {}, cookie, token)[0], 303)
        self.assertEqual(self._run_status(run_id), ReviewRunStatus.CANCELLED)
        self.assertEqual(self._item_status(item.id), ProcurementItemStatus.DRAFT)
        self.assertEqual(self._audit_counts().get("review_run_cancelled"), 1)

    def test_cancel_running_run_keeps_item_statuses(self) -> None:
        cookie, token = self._session()
        run_id = self._create_run(cookie, token)
        item = self._add_item(run_id, cookie, token)
        self.assertEqual(self._post(f"/runs/{run_id}/start", {}, cookie, token)[0], 303)
        self.assertEqual(self._post(f"/runs/{run_id}/cancel", {}, cookie, token)[0], 303)
        self.assertEqual(self._run_status(run_id), ReviewRunStatus.CANCELLED)
        self.assertEqual(self._item_status(item.id), ProcurementItemStatus.PENDING)

    def test_complete_run_ignores_soft_deleted_items(self) -> None:
        cookie, token = self._session()
        run_id = self._create_run(cookie, token)
        removed = self._add_item(run_id, cookie, token, "видалити")
        kept = self._add_item(run_id, cookie, token, "залишити")
        with SqliteUnitOfWork(self.db, immediate=True) as uow:
            self.assertTrue(uow.items.soft_delete(
                run_id, removed.id, ProcurementItemStatus.DRAFT
            ))
        self.assertEqual(self._post(f"/runs/{run_id}/start", {}, cookie, token)[0], 303)
        self.assertEqual(self._item_status(kept.id), ProcurementItemStatus.PENDING)
        self.assertEqual(
            self._post(f"/runs/{run_id}/items/{kept.id}/complete", {}, cookie, token)[0], 303
        )
        self.assertEqual(self._post(f"/runs/{run_id}/complete", {}, cookie, token)[0], 303)
        self.assertEqual(self._run_status(run_id), ReviewRunStatus.COMPLETED)

    def test_invalid_transitions_return_409(self) -> None:
        cookie, token = self._session()
        run_id = self._create_run(cookie, token)
        item = self._add_item(run_id, cookie, token)
        self.assertEqual(self._post(f"/runs/{run_id}/complete", {}, cookie, token)[0], 409)
        self.assertEqual(self._post(f"/runs/{run_id}/start", {}, cookie, token)[0], 303)
        self.assertEqual(self._post(f"/runs/{run_id}/start", {}, cookie, token)[0], 409)
        self.assertEqual(
            self._post(f"/runs/{run_id}/items/{item.id}/complete", {}, cookie, token)[0], 303
        )
        self.assertEqual(self._post(f"/runs/{run_id}/complete", {}, cookie, token)[0], 303)
        self.assertEqual(self._post(f"/runs/{run_id}/cancel", {}, cookie, token)[0], 409)

    # ------------------------------------------------------------------
    # missing entities
    # ------------------------------------------------------------------

    def test_missing_run_returns_404(self) -> None:
        cookie, token = self._session()
        missing = uuid4()
        for path in (
            f"/runs/{missing}/start",
            f"/runs/{missing}/complete",
            f"/runs/{missing}/cancel",
        ):
            self.assertEqual(self._post(path, {}, cookie, token)[0], 404)
        self.assertEqual(
            self._post(
                f"/runs/{missing}/items/{uuid4()}/complete", {}, cookie, token
            )[0],
            404,
        )

    def test_missing_item_returns_404(self) -> None:
        cookie, token = self._session()
        run_id = self._create_run(cookie, token)
        self._add_item(run_id, cookie, token)
        self.assertEqual(self._post(f"/runs/{run_id}/start", {}, cookie, token)[0], 303)
        self.assertEqual(
            self._post(f"/runs/{run_id}/items/{uuid4()}/complete", {}, cookie, token)[0], 404
        )

    def test_item_from_another_run_returns_404(self) -> None:
        cookie, token = self._session()
        first_run = self._create_run(cookie, token)
        second_run = self._create_run(cookie, token)
        foreign = self._add_item(second_run, cookie, token, "чужа")
        self._add_item(first_run, cookie, token, "своя")
        self.assertEqual(self._post(f"/runs/{first_run}/start", {}, cookie, token)[0], 303)
        self.assertEqual(
            self._post(
                f"/runs/{first_run}/items/{foreign.id}/complete", {}, cookie, token
            )[0],
            404,
        )
        self.assertEqual(self._item_status(foreign.id), ProcurementItemStatus.DRAFT)

    def test_deleted_item_returns_404(self) -> None:
        cookie, token = self._session()
        run_id = self._create_run(cookie, token)
        removed = self._add_item(run_id, cookie, token, "видалити")
        self._add_item(run_id, cookie, token, "залишити")
        with SqliteUnitOfWork(self.db, immediate=True) as uow:
            self.assertTrue(uow.items.soft_delete(
                run_id, removed.id, ProcurementItemStatus.DRAFT
            ))
        self.assertEqual(self._post(f"/runs/{run_id}/start", {}, cookie, token)[0], 303)
        self.assertEqual(
            self._post(f"/runs/{run_id}/items/{removed.id}/complete", {}, cookie, token)[0], 404
        )

    # ------------------------------------------------------------------
    # security
    # ------------------------------------------------------------------

    def test_lifecycle_post_requires_csrf_token(self) -> None:
        cookie, token = self._session()
        run_id = self._create_run(cookie, token)
        self._add_item(run_id, cookie, token)

        self.assertEqual(
            self._post(f"/runs/{run_id}/start", {}, cookie, token, include_csrf=False)[0], 403
        )
        self.assertEqual(
            self._post(f"/runs/{run_id}/start", {}, cookie, token, csrf="wrong-token")[0], 403
        )
        duplicate = urlencode(
            [("csrf_token", token), ("csrf_token", token)]
        ).encode("utf-8")
        self.assertEqual(
            self._request(
                "POST", f"/runs/{run_id}/start", cookie=cookie, body=duplicate,
                content_type="application/x-www-form-urlencoded", origin=self.base,
            )[0],
            403,
        )
        self.assertEqual(self._run_status(run_id), ReviewRunStatus.DRAFT)

    def test_lifecycle_post_rejects_foreign_origin(self) -> None:
        cookie, token = self._session()
        run_id = self._create_run(cookie, token)
        self._add_item(run_id, cookie, token)
        self.assertEqual(
            self._post(
                f"/runs/{run_id}/start", {}, cookie, token, origin="http://evil.example"
            )[0],
            403,
        )
        self.assertEqual(
            self._post(f"/runs/{run_id}/start", {}, cookie, token, origin="")[0], 403
        )
        self.assertEqual(self._run_status(run_id), ReviewRunStatus.DRAFT)

    def test_lifecycle_post_rejects_foreign_host(self) -> None:
        cookie, token = self._session()
        run_id = self._create_run(cookie, token)
        self._add_item(run_id, cookie, token)
        self.assertEqual(
            self._post(f"/runs/{run_id}/start", {}, cookie, token, host="evil.example")[0], 403
        )
        self.assertEqual(self._run_status(run_id), ReviewRunStatus.DRAFT)

    # ------------------------------------------------------------------
    # method handling and concurrency
    # ------------------------------------------------------------------

    def test_get_on_action_paths_returns_404(self) -> None:
        cookie, token = self._session()
        run_id = self._create_run(cookie, token)
        item = self._add_item(run_id, cookie, token)
        for path in (
            f"/runs/{run_id}/start",
            f"/runs/{run_id}/complete",
            f"/runs/{run_id}/cancel",
            f"/runs/{run_id}/items/{item.id}/complete",
            f"/runs/{run_id}/items/{item.id}/fail",
            f"/runs/{run_id}/items/{item.id}/retry",
        ):
            status, _, _ = self._request("GET", path)
            self.assertEqual(status, 404, path)
        self.assertEqual(self._run_status(run_id), ReviewRunStatus.DRAFT)

    def test_concurrent_start_only_one_succeeds(self) -> None:
        cookie, token = self._session()
        run_id = self._create_run(cookie, token)
        self._add_item(run_id, cookie, token)
        barrier = threading.Barrier(2)
        results: list[int] = []
        errors: list[BaseException] = []

        def start() -> None:
            barrier.wait()
            try:
                results.append(self._post(f"/runs/{run_id}/start", {}, cookie, token)[0])
            except BaseException as exc:  # pragma: no cover - defensive
                errors.append(exc)

        workers = [threading.Thread(target=start) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=20)
        self.assertFalse(errors)
        self.assertCountEqual(results, [303, 409])
        self.assertEqual(self._run_status(run_id), ReviewRunStatus.RUNNING)

    def test_concurrent_item_complete_only_one_succeeds(self) -> None:
        cookie, token = self._session()
        run_id = self._create_run(cookie, token)
        item = self._add_item(run_id, cookie, token)
        self.assertEqual(self._post(f"/runs/{run_id}/start", {}, cookie, token)[0], 303)
        barrier = threading.Barrier(2)
        results: list[int] = []
        errors: list[BaseException] = []

        def complete() -> None:
            barrier.wait()
            try:
                results.append(
                    self._post(f"/runs/{run_id}/items/{item.id}/complete", {}, cookie, token)[0]
                )
            except BaseException as exc:  # pragma: no cover - defensive
                errors.append(exc)

        workers = [threading.Thread(target=complete) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=20)
        self.assertFalse(errors)
        self.assertCountEqual(results, [303, 409])
        self.assertEqual(self._item_status(item.id), ProcurementItemStatus.COMPLETED)


if __name__ == "__main__":
    unittest.main()