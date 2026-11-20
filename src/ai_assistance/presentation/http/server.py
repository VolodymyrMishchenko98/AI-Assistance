from decimal import Decimal, InvalidOperation
from email.parser import BytesParser
from email.policy import default
import hashlib
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from uuid import UUID
import os
import tempfile

from ...config import validate_loopback_host
from ...modules.price_checker.application.commands import (
    AddProcurementItemCommand,
    ConfirmImportCommand,
    CreateImportPreviewCommand,
    CreateReviewRunCommand,
    ExcludeImportRowCommand,
    UpdateImportRowCommand,
)
from ...modules.price_checker.application.services import PriceCheckerApplication
from ...modules.price_checker.domain.models import ValidationError


class LocalRequestHandler(BaseHTTPRequestHandler):
    application: PriceCheckerApplication

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        is_import_page = (
            (parsed.path.startswith("/runs/") and "/imports/" in parsed.path)
            or parsed.path.startswith("/imports/")
        )
        is_run_page = parsed.path.startswith("/runs/") and not parsed.path.endswith("/items") and not is_import_page
        if parsed.path != "/" and not is_run_page and not is_import_page:
            self._send_error(404, "Сторінку не знайдено")
            return
        try:
            runs = self.application.dashboard()
            rows = "".join(
                f'<li><a href="/?run_id={run.id}">{escape(str(run.id))}</a> — '
                f'{escape(run.status.value)} — позицій: {run.item_count}</li>'
                for run in runs
            ) or "<li>Перевірок ще немає</li>"
            selected_id = parse_qs(parsed.query).get("run_id", [None])[0]
            if is_run_page:
                selected_id = parsed.path[len("/runs/"):]
            form = ""
            if selected_id is not None:
                run_id = UUID(selected_id)
                items = self.application.list_procurement_items(run_id)
                item_rows = "".join(
                    f"<li>{escape(item.name)} — {escape(str(item.quantity))} "
                    f"{escape(item.unit or '')}"
                    f"{(' — ' + escape(item.description)) if item.description else ''}</li>"
                    for item in items
                ) or "<li>Позицій ще немає</li>"
                form = f'''<h2>Додати закупівельну позицію</h2>
<form method="post" action="/runs/{run_id}/items">
<label>Назва <input name="name" required></label><br>
<label>Кількість <input name="quantity" required></label><br>
<label>Одиниця виміру <input name="unit" required></label><br>
<label>Технічні характеристики <textarea name="description"></textarea></label><br>
<button>Додати позицію</button></form>
<h2>Позиції</h2><ul>{item_rows}</ul>'''
                form += f'''<h2>Імпорт XLSX</h2>
<form method="post" enctype="multipart/form-data" action="/runs/{run_id}/imports/preview">
<input type="hidden" name="idempotency_key" value="{run_id}-new">
<label>Файл XLSX <input type="file" name="file" accept=".xlsx" required></label><br>
<label>Лист <input name="sheet" required></label><br>
<label>Рядок заголовків <input name="header_row" value="1" required></label><br>
<label>Назва <input name="name_column" required></label><br>
<label>Кількість <input name="quantity_column" required></label><br>
<label>Одиниця <input name="unit_column" required></label><br>
<label>Характеристики <input name="description_column"></label><br>
<button>Створити попередній перегляд</button></form>'''
            if is_import_page:
                if parsed.path.startswith("/runs/"):
                    run_path, import_path = parsed.path.split("/imports/", 1)
                    run_id = UUID(run_path[len("/runs/"):])
                    preview_id = UUID(import_path)
                    preview = self.application.get_import_preview(preview_id, run_id)
                else:
                    self._send_error(404, "Сторінку не знайдено")
                    return
                item_rows = "".join(
                    f"<tr><td>{row.excel_row_number}</td><td>{escape(row.name or '')}</td>"
                    f"<td>{escape(str(row.quantity) if row.quantity is not None else '')}</td>"
                    f"<td>{escape(row.unit or '')}</td><td>{escape(row.status.value)}</td>"
                    f"<td>{escape('; '.join(row.errors + row.warnings))}</td>"
                    f"<td><form method=\"post\" action=\"/runs/{run_id}/imports/{preview.id}/rows/{row.id}\">"
                    f"<input name=\"name\" value=\"{escape(row.name or '')}\">"
                    f"<input name=\"quantity\" value=\"{escape(str(row.quantity) if row.quantity is not None else '')}\">"
                    f"<input name=\"unit\" value=\"{escape(row.unit or '')}\">"
                    f"<input name=\"description\" value=\"{escape(row.description or '')}\">"
                    f"<button>Виправити</button></form></td></tr>"
                    for row in preview.rows
                )
                body = f'''<!doctype html><html lang="uk"><meta charset="utf-8">
<title>Попередній перегляд імпорту</title><h1>Попередній перегляд імпорту</h1>
<table><tr><th>Рядок</th><th>Назва</th><th>Кількість</th><th>Одиниця</th><th>Статус</th><th>Повідомлення</th><th>Редагування</th></tr>{item_rows}</table>
<form method="post" action="/runs/{run_id}/imports/{preview.id}/confirm">
<input type="hidden" name="idempotency_key" value="{escape(preview.idempotency_key)}">
<label><input type="checkbox" name="acknowledge_warnings"> Підтверджую попередження</label>
<button>Підтвердити імпорт</button></form></html>'''.encode("utf-8")
                self._send_body(200, body)
                return
            body = f'''<!doctype html><html lang="uk"><meta charset="utf-8">
<title>Price Checker</title><h1>Price Checker</h1>
<p>Ручне додавання закупівельних позицій.</p>
<form method="post" action="/runs"><button>Створити перевірку</button></form>
<h2>Перевірки</h2><ul>{rows}</ul>{form}</html>'''.encode("utf-8")
            self._send_body(200, body)
        except LookupError:
            self._send_error(404, "Перевірку не знайдено")
        except (ValueError, ValidationError):
            self._send_error(400, "Некоректні дані запиту")
        except Exception:
            self._send_error(500, "Внутрішня помилка застосунку")

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        is_item = path.startswith("/runs/") and path.endswith("/items")
        is_preview = path.startswith("/runs/") and path.endswith("/imports/preview")
        is_confirm = path.startswith("/runs/") and "/imports/" in path and path.endswith("/confirm")
        is_exclude = path.startswith("/runs/") and "/imports/" in path and "/rows/" in path and path.endswith("/exclude")
        is_update = path.startswith("/runs/") and "/imports/" in path and "/rows/" in path and not is_exclude
        if path != "/runs" and not is_item and not is_preview and not is_confirm and not is_exclude and not is_update:
            self._send_error(404, "Сторінку не знайдено")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0:
                raise ValueError("negative content length")
            max_body = 26 * 1024 * 1024 if is_preview else 4096
            if length > max_body:
                self._send_error(413, "Запит надто великий")
                return
            body = self.rfile.read(length)
            if is_preview:
                values, file_bytes = self._multipart_values(body, self.headers.get("Content-Type", ""))
                run_id = UUID(path[len("/runs/"):-len("/imports/preview")])
                temp_path = None
                try:
                    suffix = ".xlsx"
                    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as upload:
                        upload.write(file_bytes)
                        temp_path = upload.name
                    mapping = {
                        "name": values.get("name_column", ""),
                        "quantity": values.get("quantity_column", ""),
                        "unit": values.get("unit_column", ""),
                    }
                    if values.get("description_column"):
                        mapping["description"] = values["description_column"]
                    fingerprint = hashlib.sha256(file_bytes).hexdigest()
                    preview = self.application.create_import_preview(CreateImportPreviewCommand(
                        run_id=run_id,
                        source_path=temp_path,
                        source_fingerprint=fingerprint,
                        idempotency_key=values.get("idempotency_key", ""),
                        sheet=values.get("sheet", ""),
                        header_row=int(values.get("header_row", "1")),
                        mapping=mapping,
                    ))
                finally:
                    if temp_path:
                        try: os.unlink(temp_path)
                        except OSError: pass
                self.send_response(303)
                self.send_header("Location", f"/runs/{run_id}/imports/{preview.id}")
                self.end_headers()
                return
            values = parse_qs(body.decode("utf-8"), strict_parsing=True)
            if is_confirm:
                run_path, rest = path.split("/imports/", 1)
                run_id = UUID(run_path[len("/runs/"):])
                preview_id = UUID(rest[:-len("/confirm")])
                preview = self.application.get_import_preview(preview_id, run_id)
                self.application.confirm_import(ConfirmImportCommand(
                    preview_id, values.get("idempotency_key", [""])[0],
                    run_id,
                    values.get("acknowledge_warnings", [""])[0] == "on",
                ))
                location = "/"
            elif is_update:
                run_path, rest = path.split("/imports/", 1)
                run_id = UUID(run_path[len("/runs/"):])
                _, row_rest = rest.split("/rows/", 1)
                preview_id, row_id = UUID(rest.split("/rows/", 1)[0]), UUID(row_rest)
                preview = self.application.get_import_preview(preview_id, run_id)
                quantity_text = values.get("quantity", [""])[0].strip()
                quantity = Decimal(quantity_text) if quantity_text else None
                self.application.update_import_row(UpdateImportRowCommand(
                    preview_id, row_id, values.get("name", [""])[0], quantity,
                    values.get("unit", [""])[0], values.get("description", [""])[0] or None,
                    run_id,
                ))
                location = f"/runs/{preview.run_id}/imports/{preview_id}"
            elif is_exclude:
                run_path, rest = path.split("/imports/", 1)
                run_id = UUID(run_path[len("/runs/"):])
                preview_id, row_id = UUID(rest.split("/rows/", 1)[0]), UUID(rest.split("/rows/", 1)[1].removesuffix("/exclude"))
                preview = self.application.get_import_preview(preview_id, run_id)
                self.application.exclude_import_row(ExcludeImportRowCommand(
                    preview_id, row_id,
                    run_id,
                ))
                location = f"/runs/{preview.run_id}/imports/{preview_id}"
            elif path == "/runs":
                self.application.create_review_run(CreateReviewRunCommand())
                location = "/"
            else:
                run_id = UUID(path[len("/runs/"):-len("/items")])
                try:
                    quantity = Decimal(values.get("quantity", [""])[0].strip())
                except InvalidOperation as exc:
                    raise ValidationError("quantity must be a decimal") from exc
                self.application.add_procurement_item(AddProcurementItemCommand(
                    run_id=run_id,
                    name=values.get("name", [""])[0],
                    quantity=quantity,
                    unit=values.get("unit", [""])[0],
                    description=values.get("description", [None])[0] or None,
                ))
                location = f"/?run_id={run_id}"
            self.send_response(303)
            self.send_header("Location", location)
            self.end_headers()
        except LookupError:
            self._send_error(404, "Перевірку не знайдено")
        except (UnicodeDecodeError, ValueError, ValidationError):
            self._send_error(400, "Некоректні дані запиту")
        except Exception:
            self._send_error(500, "Внутрішня помилка застосунку")

    @staticmethod
    def _multipart_values(body: bytes, content_type: str) -> tuple[dict[str, str], bytes]:
        if not content_type.startswith("multipart/form-data"):
            raise ValueError("очікується multipart/form-data")
        message = BytesParser(policy=default).parsebytes(
            f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + body
        )
        if not message.is_multipart():
            raise ValueError("некоректний multipart-запит")
        values: dict[str, str] = {}
        file_bytes: bytes | None = None
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if name == "file":
                filename = part.get_filename() or ""
                if not filename.lower().endswith(".xlsx"):
                    raise ValueError("підтримуються лише файли .xlsx")
                file_bytes = part.get_payload(decode=True) or b""
            elif name:
                values[name] = (part.get_payload(decode=True) or b"").decode("utf-8")
        if file_bytes is None or not file_bytes:
            raise ValueError("файл XLSX не передано")
        if len(file_bytes) > 25 * 1024 * 1024:
            raise ValueError("розмір файлу перевищує дозволений ліміт")
        return values, file_bytes

    def _send_body(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status: int, message: str) -> None:
        body = f'<!doctype html><html lang="uk"><meta charset="utf-8"><title>Помилка</title><h1>{escape(message)}</h1></html>'.encode("utf-8")
        self._send_body(status, body)

    def log_message(self, format: str, *args: object) -> None:
        return


def serve(application: PriceCheckerApplication, host: str = "127.0.0.1", port: int = 8765) -> None:
    validate_loopback_host(host)
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    handler = type("BoundLocalRequestHandler", (LocalRequestHandler,), {"application": application})
    with ThreadingHTTPServer((host, port), handler) as server:
        server.serve_forever()
