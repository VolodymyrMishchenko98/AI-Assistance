from decimal import Decimal, InvalidOperation
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

from ...config import validate_loopback_host
from ...modules.price_checker.application.commands import AddProcurementItemCommand, CreateReviewRunCommand
from ...modules.price_checker.application.services import PriceCheckerApplication
from ...modules.price_checker.domain.models import ValidationError


class LocalRequestHandler(BaseHTTPRequestHandler):
    application: PriceCheckerApplication

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        is_run_page = parsed.path.startswith("/runs/") and not parsed.path.endswith("/items")
        if parsed.path != "/" and not is_run_page:
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
        if path != "/runs" and not is_item:
            self._send_error(404, "Сторінку не знайдено")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0:
                raise ValueError("negative content length")
            if length > 4096:
                self._send_error(413, "Запит надто великий")
                return
            values = parse_qs(self.rfile.read(length).decode("utf-8"), strict_parsing=True)
            if path == "/runs":
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
