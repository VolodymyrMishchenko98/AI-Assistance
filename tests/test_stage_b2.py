import hashlib
import http.client
import re
import threading
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
from uuid import UUID
from zipfile import ZipFile, ZIP_DEFLATED
from openpyxl import Workbook

from src.ai_assistance.modules.price_checker.application.commands import (
    ConfirmImportCommand,
    CreateImportPreviewCommand,
    CreateReviewRunCommand,
    UpdateImportRowCommand,
)
from src.ai_assistance.modules.price_checker.application.import_ports import WorkbookRow
from src.ai_assistance.modules.price_checker.application.imports import ImportSheetDto, ImportRowStatus
from src.ai_assistance.modules.price_checker.application.services import PriceCheckerApplication
from src.ai_assistance.modules.price_checker.infrastructure.persistence.sqlite import SqliteDatabase, SqliteUnitOfWork
from src.ai_assistance.modules.price_checker.infrastructure.xlsx_reader import OpenpyxlXlsxReader, XlsxImportError, _resolve_package_target
from src.ai_assistance.presentation.http.server import LocalRequestHandler


class FakeReader:
    def list_sheets(self, path: Path):
        return (ImportSheetDto("Закупівля", 3, 4), ImportSheetDto("Інше", 1, 1))

    def iter_rows(self, path: Path, sheet: str, header_row: int, mapping: dict[str, str]):
        yield WorkbookRow(2, {"Назва": "Насос", "Кількість": 2, "Одиниця": "шт.", "Характеристики": "220V"}, frozenset())
        yield WorkbookRow(3, {"Назва": "Насос", "Кількість": 2, "Одиниця": "шт.", "Характеристики": "220V"}, frozenset())


class StageB2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.db = SqliteDatabase(Path(self.tmp.name) / "stage-b2.sqlite3")
        self.app = PriceCheckerApplication(lambda: SqliteUnitOfWork(self.db), FakeReader())
        self.run = self.app.create_review_run(CreateReviewRunCommand())
        self.source = Path(self.tmp.name) / "source.xlsx"
        self.source.write_bytes(b"fake-xlsx")

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    def command(self, key: str = "operation-1", fingerprint: str | None = None) -> CreateImportPreviewCommand:
        return CreateImportPreviewCommand(
            self.run.id, str(self.source), fingerprint or hashlib.sha256(b"fake-xlsx").hexdigest(), key,
            "Закупівля", 1, {"name": "Назва", "quantity": "Кількість", "unit": "Одиниця", "description": "Характеристики"},
        )

    def _count(self, table: str) -> int:
        with self.db.transaction() as connection:
            return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def _http(self, method: str, url: str, *, cookie: str | None = None, body: bytes | None = None,
              content_type: str | None = None, origin: str | None = None):
        parsed = urlsplit(url)
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
        headers = {}
        if cookie is not None:
            headers["Cookie"] = cookie
        if content_type is not None:
            headers["Content-Type"] = content_type
        if origin is not None:
            headers["Origin"] = origin
        if body is not None:
            headers["Content-Length"] = str(len(body))
        try:
            connection.request(method, parsed.path, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def _fetch_upload_form(self, base: str, run_id, cookie: str | None = None) -> tuple[str, str, str]:
        status, headers, body = self._http("GET", f"{base}/runs/{run_id}", cookie=cookie)
        self.assertEqual(status, 200)
        set_cookie = headers.get("Set-Cookie")
        session_cookie = set_cookie.split(";")[0] if set_cookie else cookie
        self.assertIsNotNone(session_cookie)
        text = body.decode("utf-8")
        token = re.search(r'name="csrf_token" value="([^"]+)"', text).group(1)
        keys = re.findall(r'name="idempotency_key" value="([^"]*)"', text)
        self.assertEqual(len(keys), 1)
        return keys[0], token, session_cookie

    def _post_import(self, base: str, run_id, key: str, token: str, cookie: str, payload: bytes,
                     filename: str = "items.xlsx"):
        boundary = "stage-b2-upload-boundary"
        fields = {
            "idempotency_key": key,
            "sheet": "Закупівля",
            "header_row": "1",
            "name_column": "Назва",
            "quantity_column": "Кількість",
            "unit_column": "Одиниця",
            "csrf_token": token,
        }
        chunks = [
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
            for name, value in fields.items()
        ]
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n".encode()
            + payload
            + f"\r\n--{boundary}--\r\n".encode()
        )
        return self._http(
            "POST", f"{base}/runs/{run_id}/imports/preview", cookie=cookie, body=b"".join(chunks),
            content_type=f"multipart/form-data; boundary={boundary}", origin=base,
        )

    def test_preview_does_not_change_items_and_confirm_is_idempotent(self) -> None:
        preview = self.app.create_import_preview(self.command())
        self.assertEqual(self.app.list_procurement_items(self.run.id), [])
        self.assertEqual(preview.rows[1].status, ImportRowStatus.WARNING)
        with self.assertRaises(ValueError):
            self.app.confirm_import(ConfirmImportCommand(preview.id, "operation-1", self.run.id))
        ids = self.app.confirm_import(ConfirmImportCommand(preview.id, "operation-1", self.run.id, True))
        self.assertEqual(len(ids), 2)
        self.assertEqual(self.app.confirm_import(ConfirmImportCommand(preview.id, "operation-1", self.run.id, True)), ids)
        self.assertEqual(len(self.app.list_procurement_items(self.run.id)), 2)

    def test_row_correction_changes_status_and_extends_preview(self) -> None:
        preview = self.app.create_import_preview(self.command())
        old_expiry = preview.expires_at
        row = preview.rows[0]
        updated = self.app.update_import_row(UpdateImportRowCommand(preview.id, row.id, "Оновлений насос", Decimal("3"), "шт.", "нова характеристика", self.run.id))
        self.assertEqual(updated.rows[0].status, ImportRowStatus.VALID)
        self.assertGreater(updated.expires_at, old_expiry)

    def test_expired_preview_cannot_be_confirmed_and_cleanup_removes_it(self) -> None:
        preview = self.app.create_import_preview(self.command("ttl-operation"))
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE import_operations SET expires_at=? WHERE id=?",
                ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), str(preview.id)),
            )
        with self.assertRaises(ValueError):
            self.app.confirm_import(ConfirmImportCommand(preview.id, "ttl-operation", self.run.id, True))
        self.assertEqual(self.app.cleanup_imports(), 1)

    def test_same_key_with_different_source_is_rejected(self) -> None:
        self.app.create_import_preview(self.command("fingerprint-operation"))
        changed = CreateImportPreviewCommand(
            self.run.id, str(self.source), "different", "fingerprint-operation",
            "Закупівля", 1, {"name": "Назва", "quantity": "Кількість", "unit": "Одиниця"},
        )
        with self.assertRaises(ValueError):
            self.app.create_import_preview(changed)
        self.assertEqual(self._count("import_operations"), 1)

    def test_two_different_files_create_separate_previews_in_one_run(self) -> None:
        first = self.app.create_import_preview(self.command("upload-one", "fingerprint-one"))
        second = self.app.create_import_preview(self.command("upload-two", "fingerprint-two"))
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(self._count("import_operations"), 2)
        self.assertEqual(self.app.get_import_preview(first.id, self.run.id).source_fingerprint, "fingerprint-one")
        self.assertEqual(self.app.get_import_preview(second.id, self.run.id).source_fingerprint, "fingerprint-two")

    def test_retry_of_same_operation_returns_existing_preview(self) -> None:
        first = self.app.create_import_preview(self.command("upload-retry", "fingerprint-retry"))
        retry = self.app.create_import_preview(self.command("upload-retry", "fingerprint-retry"))
        self.assertEqual(retry.id, first.id)
        self.assertEqual(self._count("import_operations"), 1)

    def test_same_file_with_new_key_creates_new_preview(self) -> None:
        first = self.app.create_import_preview(self.command("upload-first", "fingerprint-same"))
        second = self.app.create_import_preview(self.command("upload-second", "fingerprint-same"))
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(self._count("import_operations"), 2)

    def test_closed_run_is_checked_at_confirmation(self) -> None:
        preview = self.app.create_import_preview(self.command("closed-operation"))
        with self.db.transaction() as connection:
            connection.execute("UPDATE review_runs SET status='completed' WHERE id=?", (str(self.run.id),))
        with self.assertRaises(ValueError):
            self.app.confirm_import(ConfirmImportCommand(preview.id, "closed-operation", self.run.id, True))
        self.assertEqual(self.app.list_procurement_items(self.run.id), [])

    def test_confirmation_rolls_back_all_items_when_audit_fails(self) -> None:
        preview = self.app.create_import_preview(self.command("atomic-operation"))

        class FailingAudit:
            def append(self, event):
                if event.aggregate_type == "procurement_item":
                    raise RuntimeError("audit unavailable")

        class FailingUow(SqliteUnitOfWork):
            def __enter__(self):
                result = super().__enter__()
                result.audit = FailingAudit()
                return result

        failing_app = PriceCheckerApplication(lambda: FailingUow(self.db), FakeReader())
        with self.assertRaises(RuntimeError):
            failing_app.confirm_import(ConfirmImportCommand(preview.id, "atomic-operation", self.run.id, True))
        self.assertEqual(self.app.list_procurement_items(self.run.id), [])
        self.assertEqual(self.app.get_import_preview(preview.id, self.run.id).status.value, "preview_ready")

    def test_secure_reader_rejects_traversal_and_bad_xlsx(self) -> None:
        bad = Path(self.tmp.name) / "bad.xlsx"
        with ZipFile(bad, "w", ZIP_DEFLATED) as archive:
            archive.writestr("../evil.txt", "x")
        with self.assertRaises(XlsxImportError):
            OpenpyxlXlsxReader().list_sheets(bad)

    def test_real_openpyxl_reader_handles_values_dates_and_relationships(self) -> None:
        path = Path(self.tmp.name) / "real.xlsx"
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Items"
        sheet.append(["Name", "Qty", "Unit", "Date"])
        sheet.append(["Pump", 2, "pcs", __import__("datetime").datetime(2026, 1, 2)])
        workbook.save(path)
        reader = OpenpyxlXlsxReader()
        self.assertEqual(reader.list_sheets(path)[0].name, "Items")
        rows = list(reader.iter_rows(path, "Items", 1, {"name": "Name", "quantity": "Qty", "unit": "Unit"}))
        self.assertEqual(rows[0].values["Name"], "Pump")
        self.assertEqual(rows[0].values["Date"], "2026-01-02T00:00:00")

    def test_real_openpyxl_full_import_creates_items_and_audit(self) -> None:
        path = Path(self.tmp.name) / "full-real.xlsx"
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Items"
        sheet.append(["Name", "Qty", "Unit", "Date"])
        sheet.append(["Pump", 2, "pcs", datetime(2026, 1, 2)])
        workbook.create_sheet("Other")
        workbook.save(path)

        app = PriceCheckerApplication(
            lambda: SqliteUnitOfWork(self.db),
            OpenpyxlXlsxReader(),
        )
        run = app.create_review_run(CreateReviewRunCommand())
        preview = app.create_import_preview(CreateImportPreviewCommand(
            run.id,
            str(path),
            hashlib.sha256(path.read_bytes()).hexdigest(),
            "full-real-operation",
            "Items",
            1,
            {"name": "Name", "quantity": "Qty", "unit": "Unit"},
        ))
        item_ids = app.confirm_import(ConfirmImportCommand(
            preview.id,
            "full-real-operation",
            run.id,
            True,
        ))
        self.assertEqual(len(item_ids), 1)
        self.assertEqual([item.name for item in app.list_procurement_items(run.id)], ["Pump"])
        with self.db.transaction() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM audit_events WHERE aggregate_type='procurement_item'"
                ).fetchone()[0],
                1,
            )

    def test_relationship_target_resolution_handles_absolute_relative_and_rejects_escape(self) -> None:
        names = {"xl/workbook.xml", "xl/worksheets/sheet1.xml"}
        self.assertEqual(_resolve_package_target("xl/workbook.xml", "/xl/worksheets/sheet1.xml", names), "xl/worksheets/sheet1.xml")
        self.assertEqual(_resolve_package_target("xl/workbook.xml", "worksheets/sheet1.xml", names), "xl/worksheets/sheet1.xml")
        self.assertEqual(
            _resolve_package_target("xl/workbook.xml", "../xl/worksheets/sheet1.xml", names),
            "xl/worksheets/sheet1.xml",
        )
        with self.assertRaises(XlsxImportError):
            _resolve_package_target("xl/workbook.xml", "../secret.xml", names)
        with self.assertRaises(XlsxImportError):
            _resolve_package_target("xl/workbook.xml", "../../secret.xml", names)
        with self.assertRaises(XlsxImportError):
            _resolve_package_target("xl/workbook.xml", "%2e%2e/secret.xml", names)
        with self.assertRaises(XlsxImportError):
            _resolve_package_target("xl/workbook.xml", "%252e%252e/secret.xml", names)
        with self.assertRaises(XlsxImportError):
            _resolve_package_target("xl/workbook.xml", "../xl/../secret.xml", {"xl/workbook.xml", "secret.xml"}),
        with self.assertRaises(XlsxImportError):
            _resolve_package_target("xl/workbook.xml", "/xl/worksheets/missing.xml", names)

    def test_real_openpyxl_formula_is_not_evaluated(self) -> None:
        path = Path(self.tmp.name) / "formula.xlsx"
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["Name", "Qty", "Unit"])
        sheet.append(["Pump", "=1+1", "pcs"])
        workbook.save(path)
        rows = list(OpenpyxlXlsxReader().iter_rows(path, "Sheet", 1, {"name": "Name", "quantity": "Qty", "unit": "Unit"}))
        self.assertEqual(rows[0].values["Qty"], "=1+1")
        self.assertIn("Qty", rows[0].formula_fields)

    def test_real_openpyxl_merged_cells_are_rejected(self) -> None:
        path = Path(self.tmp.name) / "merged-real.xlsx"
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["Name", "Qty", "Unit"])
        sheet.merge_cells("A2:B2")
        sheet["A2"] = "Pump"
        workbook.save(path)
        with self.assertRaises(XlsxImportError):
            OpenpyxlXlsxReader().list_sheets(path)

    def test_operation_must_belong_to_declared_review_run(self) -> None:
        preview = self.app.create_import_preview(self.command("ownership-operation"))
        other = self.app.create_review_run(CreateReviewRunCommand())
        with self.assertRaises(LookupError):
            self.app.get_import_preview(preview.id, other.id)
        with self.assertRaises(LookupError):
            self.app.confirm_import(ConfirmImportCommand(preview.id, "ownership-operation", other.id, True))
        with self.assertRaises(LookupError):
            self.app.update_import_row(UpdateImportRowCommand(
                preview.id, preview.rows[0].id, "other", Decimal("1"), "шт.", None, other.id,
            ))

    def test_application_import_operations_require_run_id(self) -> None:
        preview = self.app.create_import_preview(self.command("required-run-operation"))
        with self.assertRaises(TypeError):
            self.app.get_import_preview(preview.id)
        with self.assertRaises(TypeError):
            self.app.confirm_import(ConfirmImportCommand(preview.id, "required-run-operation"))

    def test_duplicate_zip_entries_are_rejected(self) -> None:
        bad = Path(self.tmp.name) / "duplicates.xlsx"
        with ZipFile(bad, "w", ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", "<Types/>")
            archive.writestr("[Content_Types].xml", "<Types/>")
        with self.assertRaises(XlsxImportError):
            OpenpyxlXlsxReader().list_sheets(bad)

    def test_large_xml_component_is_rejected_before_read(self) -> None:
        bad = Path(self.tmp.name) / "large.xml.xlsx"
        with ZipFile(bad, "w", ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", b"x" * (9 * 1024 * 1024))
        with self.assertRaises(XlsxImportError):
            OpenpyxlXlsxReader().list_sheets(bad)

    def test_macro_external_link_and_merged_cell_components_are_rejected(self) -> None:
        cases = {
            "macro.xlsx": ("xl/vbaProject.bin", b"macro"),
            "external.xlsx": ("xl/externalLinks/externalLink1.xml", b"<externalLink/>"),
            "merged.xlsx": ("xl/worksheets/sheet1.xml", b"<worksheet><mergeCells count=\"1\"/></worksheet>"),
        }
        for filename, (entry_name, payload) in cases.items():
            with self.subTest(filename=filename):
                path = Path(self.tmp.name) / filename
                with ZipFile(path, "w", ZIP_DEFLATED) as archive:
                    archive.writestr("[Content_Types].xml", "<Types/>")
                    archive.writestr("xl/workbook.xml", "<workbook/>")
                    archive.writestr("_rels/.rels", "<Relationships/>")
                    archive.writestr("xl/_rels/workbook.xml.rels", "<Relationships/>")
                    archive.writestr(entry_name, payload)
                with self.assertRaises(XlsxImportError):
                    OpenpyxlXlsxReader().list_sheets(path)

    def test_unusual_vba_name_is_rejected_via_content_type(self) -> None:
        path = Path(self.tmp.name) / "unusual-vba.xlsx"
        with ZipFile(path, "w", ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", (
                "<Types><Override PartName=\"/xl/custom.bin\" "
                "ContentType=\"application/vnd.ms-office.vbaProject\"/></Types>"
            ))
            archive.writestr("xl/custom.bin", b"vba")
        with self.assertRaises(XlsxImportError):
            OpenpyxlXlsxReader().list_sheets(path)

    def test_dangerous_default_content_type_is_rejected_without_filename_signal(self) -> None:
        path = Path(self.tmp.name) / "default-activex.xlsx"
        with ZipFile(path, "w", ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", (
                "<Types xmlns=\"http://schemas.openxmlformats.org/package/2006/content-types\">"
                "<Default Extension=\"dat\" ContentType=\"application/vnd.ms-office.activeX\"/>"
                "</Types>"
            ))
            archive.writestr("xl/custom.dat", b"active")
        with self.assertRaises(XlsxImportError):
            OpenpyxlXlsxReader().list_sheets(path)

    def test_safe_default_content_type_is_not_rejected_for_its_type(self) -> None:
        path = Path(self.tmp.name) / "safe-default.xlsx"
        with ZipFile(path, "w", ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", (
                "<Types xmlns=\"http://schemas.openxmlformats.org/package/2006/content-types\">"
                "<Default Extension=\"xml\" ContentType=\"application/xml\"/>"
                "</Types>"
            ))
            archive.writestr("xl/custom.xml", b"<safe/>")
        with self.assertRaises(XlsxImportError) as context:
            OpenpyxlXlsxReader().list_sheets(path)
        self.assertNotIn("ActiveX", str(context.exception))

    def test_conflicting_content_type_declarations_are_rejected(self) -> None:
        path = Path(self.tmp.name) / "conflicting-content-types.xlsx"
        with ZipFile(path, "w", ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", (
                "<Types><Default Extension=\"dat\" ContentType=\"application/octet-stream\"/>"
                "<Default Extension=\"DAT\" ContentType=\"application/xml\"/></Types>"
            ))
            archive.writestr("xl/custom.dat", b"x")
        with self.assertRaises(XlsxImportError):
            OpenpyxlXlsxReader().list_sheets(path)

    def test_activex_and_external_relationship_are_rejected(self) -> None:
        path = Path(self.tmp.name) / "activex.xlsx"
        with ZipFile(path, "w", ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", (
                "<Types><Override PartName=\"/xl/activex.bin\" "
                "ContentType=\"application/vnd.ms-office.activeX\"/></Types>"
            ))
            archive.writestr("xl/activex.bin", b"active")
        with self.assertRaises(XlsxImportError):
            OpenpyxlXlsxReader().list_sheets(path)

        external = Path(self.tmp.name) / "external-target.xlsx"
        with ZipFile(external, "w", ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", "<Types/>")
            archive.writestr("_rels/.rels", "<Relationships/>")
            archive.writestr("xl/workbook.xml", "<workbook/>")
            archive.writestr("xl/_rels/workbook.xml.rels", (
                "<Relationships><Relationship Id=\"rExt\" "
                "Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/externalLink\" "
                "Target=\"https://example.invalid/data\" TargetMode=\"External\"/></Relationships>"
            ))
        with self.assertRaises(XlsxImportError):
            OpenpyxlXlsxReader().list_sheets(external)

    def test_missing_workbook_override_is_rejected(self) -> None:
        path = Path(self.tmp.name) / "missing-workbook-override.xlsx"
        with ZipFile(path, "w", ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", "<Types/>")
            archive.writestr("xl/workbook.xml", "<workbook/>")
            archive.writestr("_rels/.rels", "<Relationships/>")
            archive.writestr("xl/_rels/workbook.xml.rels", "<Relationships/>")
        with self.assertRaises(XlsxImportError):
            OpenpyxlXlsxReader().list_sheets(path)

    def test_fake_content_types_namespace_is_rejected(self) -> None:
        path = Path(self.tmp.name) / "fake-content-types.xlsx"
        with ZipFile(path, "w", ZIP_DEFLATED) as archive:
            archive.writestr(
                "[Content_Types].xml",
                '<Types xmlns="urn:fake:content-types">'
                '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                '</Types>',
            )
            archive.writestr("_rels/.rels", "<Relationships/>")
            archive.writestr("xl/workbook.xml", "<workbook/>")
            archive.writestr("xl/_rels/workbook.xml.rels", "<Relationships/>")
        with self.assertRaises(XlsxImportError):
            OpenpyxlXlsxReader().list_sheets(path)

    def test_fake_package_relationship_namespace_is_rejected(self) -> None:
        path = Path(self.tmp.name) / "fake-package-relationships.xlsx"
        with ZipFile(path, "w", ZIP_DEFLATED) as archive:
            archive.writestr(
                "[Content_Types].xml",
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                '</Types>',
            )
            archive.writestr(
                "_rels/.rels",
                '<Relationships xmlns="urn:fake:relationships">'
                '<Relationship Id="rRoot" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                '</Relationships>',
            )
            archive.writestr("xl/workbook.xml", "<workbook/>")
            archive.writestr("xl/_rels/workbook.xml.rels", "<Relationships/>")
        with self.assertRaises(XlsxImportError):
            OpenpyxlXlsxReader().list_sheets(path)

    def test_fake_workbook_namespace_is_rejected(self) -> None:
        path = Path(self.tmp.name) / "fake-workbook-namespace.xlsx"
        with ZipFile(path, "w", ZIP_DEFLATED) as archive:
            archive.writestr(
                "[Content_Types].xml",
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                '</Types>',
            )
            archive.writestr(
                "_rels/.rels",
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rRoot" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                '</Relationships>',
            )
            archive.writestr(
                "xl/_rels/workbook.xml.rels",
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rSheet" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
                '</Relationships>',
            )
            archive.writestr("xl/workbook.xml", '<workbook xmlns="urn:fake:workbook"/>')
            archive.writestr(
                "xl/worksheets/sheet1.xml",
                '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"/>',
            )
        with self.assertRaises(XlsxImportError):
            OpenpyxlXlsxReader().list_sheets(path)

    def test_traversal_is_rejected_when_target_exists_in_zip(self) -> None:
        path = Path(self.tmp.name) / "existing-traversal-target.xlsx"
        with ZipFile(path, "w", ZIP_DEFLATED) as archive:
            archive.writestr(
                "[Content_Types].xml",
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                '<Override PartName="/secret.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                '</Types>',
            )
            archive.writestr(
                "_rels/.rels",
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rRoot" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                '</Relationships>',
            )
            archive.writestr(
                "xl/_rels/workbook.xml.rels",
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rSheet" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="../secret.xml"/>'
                '</Relationships>',
            )
            archive.writestr(
                "xl/workbook.xml",
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                '<sheets><sheet name="Sheet1" sheetId="1" r:id="rSheet"/></sheets></workbook>',
            )
            archive.writestr("secret.xml", "<worksheet/>")
            archive.writestr(
                "xl/worksheets/sheet1.xml",
                '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"/>',
            )
        with self.assertRaises(XlsxImportError):
            OpenpyxlXlsxReader().list_sheets(path)

    def test_duplicate_relationship_id_is_rejected_per_rels_file(self) -> None:
        path = Path(self.tmp.name) / "duplicate-rel-id.xlsx"
        with ZipFile(path, "w", ZIP_DEFLATED) as archive:
            archive.writestr(
                "[Content_Types].xml",
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                '</Types>',
            )
            archive.writestr(
                "_rels/.rels",
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rRoot" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                '</Relationships>',
            )
            archive.writestr(
                "xl/_rels/workbook.xml.rels",
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rSame" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
                '<Relationship Id="rSame" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
                '</Relationships>',
            )
            archive.writestr(
                "xl/workbook.xml",
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                '<sheets><sheet name="Sheet1" sheetId="1" r:id="rSame"/></sheets></workbook>',
            )
            archive.writestr(
                "xl/worksheets/sheet1.xml",
                '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"/>',
            )
        with self.assertRaises(XlsxImportError):
            OpenpyxlXlsxReader().list_sheets(path)

    def test_same_relationship_id_in_different_rels_files_is_allowed(self) -> None:
        path = Path(self.tmp.name) / "same-rel-id-different-files.xlsx"
        with ZipFile(path, "w", ZIP_DEFLATED) as archive:
            archive.writestr(
                "[Content_Types].xml",
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                '</Types>',
            )
            archive.writestr(
                "_rels/.rels",
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rSame" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                '</Relationships>',
            )
            archive.writestr(
                "xl/_rels/workbook.xml.rels",
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rSame" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
                '</Relationships>',
            )
            archive.writestr(
                "xl/workbook.xml",
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                '<sheets><sheet name="Sheet1" sheetId="1" r:id="rSame"/></sheets></workbook>',
            )
            archive.writestr(
                "xl/worksheets/sheet1.xml",
                '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"/>',
            )
        sheets = OpenpyxlXlsxReader().list_sheets(path)
        self.assertEqual([sheet.name for sheet in sheets], ["Sheet1"])

    def test_ole_relationship_with_internal_ordinary_target_is_rejected(self) -> None:
        path = Path(self.tmp.name) / "dangerous-ole-relation.xlsx"
        with ZipFile(path, "w", ZIP_DEFLATED) as archive:
            archive.writestr(
                "[Content_Types].xml",
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                '<Override PartName="/xl/custom/data.bin" ContentType="application/octet-stream"/>'
                '</Types>',
            )
            archive.writestr(
                "_rels/.rels",
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rRoot" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                '</Relationships>',
            )
            archive.writestr(
                "xl/_rels/workbook.xml.rels",
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rSheet" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
                '<Relationship Id="rOle" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/oleObject" Target="custom/data.bin"/>'
                '</Relationships>',
            )
            archive.writestr(
                "xl/workbook.xml",
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                '<sheets><sheet name="Sheet1" sheetId="1" r:id="rSheet"/></sheets></workbook>',
            )
            archive.writestr(
                "xl/worksheets/sheet1.xml",
                '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"/>',
            )
            archive.writestr("xl/custom/data.bin", b"ordinary")
        with self.assertRaises(XlsxImportError):
            OpenpyxlXlsxReader().list_sheets(path)

    def test_fake_worksheet_namespace_is_rejected(self) -> None:
        path = Path(self.tmp.name) / "fake-worksheet-namespace.xlsx"
        with ZipFile(path, "w", ZIP_DEFLATED) as archive:
            archive.writestr(
                "[Content_Types].xml",
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                '</Types>',
            )
            archive.writestr(
                "_rels/.rels",
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rRoot" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                '</Relationships>',
            )
            archive.writestr(
                "xl/_rels/workbook.xml.rels",
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rSheet" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
                '</Relationships>',
            )
            archive.writestr(
                "xl/workbook.xml",
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                '<sheets><sheet name="Sheet1" sheetId="1" r:id="rSheet"/></sheets></workbook>',
            )
            archive.writestr(
                "xl/worksheets/sheet1.xml",
                '<worksheet xmlns="urn:fake:worksheet"/>',
            )
        with self.assertRaises(XlsxImportError):
            OpenpyxlXlsxReader().list_sheets(path)

    def test_missing_or_mismatched_worksheet_relationship_is_rejected(self) -> None:
        path = Path(self.tmp.name) / "bad-worksheet-link.xlsx"
        with ZipFile(path, "w", ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", (
                "<Types>"
                "<Override PartName=\"/xl/workbook.xml\" "
                "ContentType=\"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml\"/>"
                "<Override PartName=\"/xl/worksheets/sheet1.xml\" "
                "ContentType=\"application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml\"/>"
                "</Types>"
            ))
            archive.writestr("_rels/.rels", (
                "<Relationships><Relationship Id=\"rId1\" "
                "Type=\"http://schemas.openxmlformats.org/package/2006/relationships/officeDocument\" "
                "Target=\"xl/workbook.xml\"/></Relationships>"
            ))
            archive.writestr("xl/workbook.xml", (
                "<workbook xmlns:r=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships\">"
                "<sheets><sheet name=\"Sheet1\" sheetId=\"1\" r:id=\"rMissing\"/></sheets></workbook>"
            ))
            archive.writestr("xl/_rels/workbook.xml.rels", (
                "<Relationships><Relationship Id=\"rPresent\" "
                "Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet\" "
                "Target=\"worksheets/sheet1.xml\"/></Relationships>"
            ))
            archive.writestr("xl/worksheets/sheet1.xml", "<worksheet/>")
        with self.assertRaises(XlsxImportError):
            OpenpyxlXlsxReader().list_sheets(path)

    def test_two_concurrent_confirmations_create_items_once(self) -> None:
        preview = self.app.create_import_preview(self.command("concurrent-confirm"))
        results = []
        errors = []

        def confirm() -> None:
            try:
                results.append(self.app.confirm_import(
                    ConfirmImportCommand(preview.id, "concurrent-confirm", self.run.id, True)
                ))
            except Exception as exc:
                errors.append(exc)

        workers = [threading.Thread(target=confirm) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=5)
        self.assertFalse([worker for worker in workers if worker.is_alive()])
        self.assertLessEqual(len(errors), 1, errors)
        if results:
            first_ids = results[0]
            self.assertEqual(len(first_ids), 2)
        self.assertEqual(len(self.app.list_procurement_items(self.run.id)), 2)
        stored = self.app.get_import_preview(preview.id, self.run.id)
        self.assertEqual(stored.status.value, "confirmed")
        self.assertEqual(len(stored.created_item_ids), 2)

    def test_repeat_confirm_after_success_returns_saved_ids(self) -> None:
        preview = self.app.create_import_preview(self.command("repeat-confirm"))
        ids = self.app.confirm_import(
            ConfirmImportCommand(preview.id, "repeat-confirm", self.run.id, True)
        )
        repeated = self.app.confirm_import(
            ConfirmImportCommand(preview.id, "repeat-confirm", self.run.id, True)
        )
        self.assertEqual(repeated, ids)

    def test_cleanup_race_with_confirm(self) -> None:
        preview = self.app.create_import_preview(self.command("cleanup-confirm"))
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE import_operations SET expires_at=? WHERE id=?",
                ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), str(preview.id)),
            )
        confirmed = []
        cleaned = []
        errors = []

        def run_cleanup() -> None:
            try:
                cleaned.append(self.app.cleanup_imports())
            except Exception as exc:
                errors.append(exc)

        def run_confirm() -> None:
            try:
                confirmed.append(self.app.confirm_import(
                    ConfirmImportCommand(preview.id, "cleanup-confirm", self.run.id, True)
                ))
            except Exception as exc:
                errors.append(exc)

        workers = [
            threading.Thread(target=run_cleanup),
            threading.Thread(target=run_confirm),
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=5)
        self.assertTrue(all(isinstance(error, ValueError) for error in errors), errors)
        self.assertEqual(len(self.app.list_procurement_items(self.run.id)), 0)
        with self.db.transaction() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM audit_events WHERE aggregate_type='procurement_item'").fetchone()[0], 0)

    def test_edit_race_with_confirm_is_not_confirmed_as_edited_preview(self) -> None:
        preview = self.app.create_import_preview(self.command("edit-confirm"))
        row = preview.rows[0]
        update_error = []
        confirm_error = []
        confirmed = []

        def edit() -> None:
            try:
                self.app.update_import_row(UpdateImportRowCommand(
                    preview.id, row.id, "changed", Decimal("3"), "шт.", None, self.run.id,
                ))
            except Exception as exc:
                update_error.append(exc)

        def confirm() -> None:
            try:
                confirmed.append(self.app.confirm_import(
                    ConfirmImportCommand(preview.id, "edit-confirm", self.run.id, True)
                ))
            except Exception as exc:
                confirm_error.append(exc)

        barrier = threading.Barrier(2)

        def edit_barrier() -> None:
            barrier.wait()
            edit()

        def confirm_barrier() -> None:
            barrier.wait()
            confirm()

        workers = [
            threading.Thread(target=edit_barrier),
            threading.Thread(target=confirm_barrier),
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=5)
        stored = self.app.get_import_preview(preview.id, self.run.id)
        if stored.status.value == "confirmed":
            self.assertEqual(len(stored.created_item_ids), len(stored.rows))
        else:
            self.assertEqual(stored.status.value, "preview_ready")

    def test_http_preview_and_confirm_workflow(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), type("Handler", (LocalRequestHandler,), {"application": self.app}))
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        boundary = "stage-b2-boundary"
        fields = {
            "idempotency_key": "http-operation",
            "sheet": "Закупівля",
            "header_row": "1",
            "name_column": "Назва",
            "quantity_column": "Кількість",
            "unit_column": "Одиниця",
        }
        url = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with urlopen(f"{url}/runs/{self.run.id}") as page:
                cookie = page.headers.get("Set-Cookie").split(";")[0]
                token = re.search(r'name="csrf_token" value="([^"]+)"', page.read().decode("utf-8")).group(1)
            fields["csrf_token"] = token
            chunks = []
            for key, value in fields.items():
                chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
            chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="items.xlsx"\r\nContent-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\nfake-xlsx\r\n--{boundary}--\r\n'.encode())
            body = b"".join(chunks)
            response = urlopen(Request(
                f"{url}/runs/{self.run.id}/imports/preview",
                data=body,
                method="POST",
                headers={
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                    "Origin": url,
                    "Cookie": cookie,
                },
            ))
            preview_id = UUID(urlsplit(response.geturl()).path.split("/")[-1])
            confirm = urlencode({
                "idempotency_key": "http-operation",
                "acknowledge_warnings": "on",
                "csrf_token": token,
            }).encode()
            with urlopen(Request(
                f"{url}/runs/{self.run.id}/imports/{preview_id}/confirm",
                data=confirm,
                method="POST",
                headers={"Origin": url, "Cookie": cookie},
            )):
                pass
            self.assertEqual(len(self.app.list_procurement_items(self.run.id)), 2)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=3)

    def test_http_upload_form_issues_unique_keys_and_retries_are_idempotent(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), type("Handler", (LocalRequestHandler,), {"application": self.app}))
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        second_file = Path(self.tmp.name) / "second.xlsx"
        second_file.write_bytes(b"fake-xlsx-second")
        try:
            first_key, token, cookie = self._fetch_upload_form(base, self.run.id)
            second_key, _, _ = self._fetch_upload_form(base, self.run.id, cookie)
            self.assertTrue(first_key)
            self.assertTrue(second_key)
            self.assertNotEqual(first_key, second_key)
            UUID(second_key)

            status, headers, _ = self._post_import(base, self.run.id, first_key, token, cookie, self.source.read_bytes())
            self.assertEqual(status, 303)
            first_location = headers["Location"]
            status, headers, _ = self._post_import(base, self.run.id, first_key, token, cookie, self.source.read_bytes())
            self.assertEqual(status, 303)
            self.assertEqual(headers["Location"], first_location)
            self.assertEqual(self._count("import_operations"), 1)

            status, headers, _ = self._post_import(base, self.run.id, second_key, token, cookie, second_file.read_bytes())
            self.assertEqual(status, 303)
            self.assertNotEqual(headers["Location"], first_location)
            self.assertEqual(self._count("import_operations"), 2)

            status, _, _ = self._post_import(base, self.run.id, first_key, token, cookie, second_file.read_bytes())
            self.assertEqual(status, 400)
            self.assertEqual(self._count("import_operations"), 2)

            preview_id = UUID(urlsplit(first_location).path.rsplit("/", 1)[-1])
            confirm = urlencode({
                "idempotency_key": first_key,
                "acknowledge_warnings": "on",
                "csrf_token": token,
            }).encode()
            for _ in range(2):
                status, _, _ = self._http(
                    "POST", f"{base}/runs/{self.run.id}/imports/{preview_id}/confirm",
                    cookie=cookie, body=confirm,
                    content_type="application/x-www-form-urlencoded", origin=base,
                )
                self.assertEqual(status, 303)
            self.assertEqual(len(self.app.list_procurement_items(self.run.id)), 2)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
