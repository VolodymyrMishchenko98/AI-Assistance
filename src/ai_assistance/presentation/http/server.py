from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from ...config import validate_loopback_host
from ...modules.price_checker.application.commands import CreateReviewRunCommand
from ...modules.price_checker.application.services import PriceCheckerApplication


class LocalRequestHandler(BaseHTTPRequestHandler):
    application: PriceCheckerApplication

    def do_GET(self) -> None:  # noqa: N802
        if urlsplit(self.path).path != "/":
            self._send_error(404, "Сторінку не знайдено")
            return
        try:
            runs = self.application.dashboard()
            rows = "".join(
                f"<li>{escape(str(run.id))} — {escape(run.status.value)} — позицій: {run.item_count}</li>"
                for run in runs
            ) or "<li>Перевірок ще немає</li>"
            body = f"""<!doctype html><html lang=\"uk\"><meta charset=\"utf-8\">
<title>Price Checker</title><h1>Price Checker</h1>
<p>Основа застосунку готова. Пошук товарів та імпорт Excel ще не реалізовані.</p>
<form method=\"post\" action=\"/runs\"><button>Створити перевірку</button></form>
<h2>Перевірки</h2><ul>{rows}</ul></html>""".encode("utf-8")
            self._send_body(200, body)
        except Exception:
            self._send_error(500, "Внутрішня помилка застосунку")

    def do_POST(self) -> None:  # noqa: N802
        if urlsplit(self.path).path != "/runs":
            self._send_error(404, "Сторінку не знайдено")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_error(400, "Некоректний Content-Length")
            return
        if length < 0:
            self._send_error(400, "Некоректний Content-Length")
            return
        if length > 4096:
            self._send_error(413, "Запит надто великий")
            return
        try:
            if length:
                parse_qs(self.rfile.read(length).decode("utf-8"), strict_parsing=True)
            self.application.create_review_run(CreateReviewRunCommand())
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()
        except (UnicodeDecodeError, ValueError):
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
        body = f"<!doctype html><html lang=\"uk\"><meta charset=\"utf-8\"><title>Помилка</title><h1>{escape(message)}</h1></html>".encode("utf-8")
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

