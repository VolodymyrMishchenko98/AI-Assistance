import re
import threading
import unittest
from decimal import Decimal
from http.server import ThreadingHTTPServer
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from pathlib import Path
from uuid import uuid4

from src.ai_assistance.modules.price_checker.application.commands import AddProcurementItemCommand, CreateReviewRunCommand
from src.ai_assistance.modules.price_checker.application.services import PriceCheckerApplication
from src.ai_assistance.modules.price_checker.domain.models import ReviewRunStatus, ValidationError
from src.ai_assistance.modules.price_checker.infrastructure.persistence.sqlite import SqliteDatabase, SqliteUnitOfWork
from src.ai_assistance.presentation.http.server import LocalRequestHandler


class StageB1Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.database = SqliteDatabase(Path(self.directory.name) / "b1.sqlite3")
        self.app = PriceCheckerApplication(lambda: SqliteUnitOfWork(self.database))
        self.run = self.app.create_review_run(CreateReviewRunCommand())

    def tearDown(self) -> None:
        self.database.close()
        self.directory.cleanup()

    def test_create_item_and_audit_preserve_input(self) -> None:
        item = self.app.add_procurement_item(AddProcurementItemCommand(
            self.run.id, "  Кабель  ", "мідь, 3x2.5", Decimal("2.5"), " м "
        ))
        self.assertEqual(item.name, "Кабель")
        self.assertEqual(item.description, "мідь, 3x2.5")
        self.assertEqual(item.unit, "м")
        with self.database.transaction() as connection:
            event = connection.execute(
                "SELECT event_type, aggregate_id, data_json FROM audit_events WHERE aggregate_type = 'procurement_item'"
            ).fetchone()
        self.assertEqual(event["event_type"], "procurement_item_created")
        self.assertEqual(event["aggregate_id"], str(item.id))
        self.assertIn(str(self.run.id), event["data_json"])

    def test_required_fields_and_positive_quantity(self) -> None:
        for command in (
            AddProcurementItemCommand(self.run.id, "", quantity=Decimal("1"), unit="шт."),
            AddProcurementItemCommand(self.run.id, "товар", quantity=Decimal("1"), unit=""),
            AddProcurementItemCommand(self.run.id, "товар", quantity=Decimal("0"), unit="шт."),
            AddProcurementItemCommand(self.run.id, "товар", quantity=Decimal("-1"), unit="шт."),
        ):
            with self.subTest(command=command), self.assertRaises(ValidationError):
                self.app.add_procurement_item(command)

    def test_unknown_and_closed_runs_are_rejected(self) -> None:
        with self.assertRaises(LookupError):
            self.app.add_procurement_item(AddProcurementItemCommand(uuid4(), "товар", quantity=Decimal("1"), unit="шт."))
        with self.database.transaction() as connection:
            connection.execute("UPDATE review_runs SET status = ? WHERE id = ?", (ReviewRunStatus.COMPLETED.value, str(self.run.id)))
        with self.assertRaises(ValidationError):
            self.app.add_procurement_item(AddProcurementItemCommand(self.run.id, "товар", quantity=Decimal("1"), unit="шт."))

    def test_list_isolated_by_review_run(self) -> None:
        other = self.app.create_review_run(CreateReviewRunCommand())
        self.app.add_procurement_item(AddProcurementItemCommand(self.run.id, "перший", quantity=Decimal("1"), unit="шт."))
        self.app.add_procurement_item(AddProcurementItemCommand(other.id, "другий", quantity=Decimal("1"), unit="шт."))
        self.assertEqual([item.name for item in self.app.list_procurement_items(self.run.id)], ["перший"])

    def test_http_form_adds_item_and_reports_validation(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), type("Handler", (LocalRequestHandler,), {"application": self.app}))
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with urlopen(f"{base}/runs/{self.run.id}") as response:
                page = response.read()
                cookie = response.headers.get("Set-Cookie")
            self.assertIn("Додати закупівельну позицію".encode(), page)
            token = re.search(r'name="csrf_token" value="([^"]+)"', page.decode("utf-8")).group(1)
            request = Request(
                f"{base}/runs/{self.run.id}/items",
                data=urlencode({
                    "name": "насос", "quantity": "0", "unit": "шт.", "csrf_token": token,
                }).encode(),
                method="POST",
                headers={"Origin": base, "Cookie": cookie.split(";")[0]},
            )
            with self.assertRaises(HTTPError) as context:
                urlopen(request)
            self.assertEqual(context.exception.code, 400)
            self.assertEqual(self.app.list_procurement_items(self.run.id), [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
