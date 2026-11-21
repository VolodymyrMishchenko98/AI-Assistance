from decimal import Decimal, InvalidOperation
from email.message import EmailMessage
from email.parser import BytesParser
from email.policy import default
import hashlib
from html import escape
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import secrets
import socket
import threading
import time
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4
import os
import re
import tempfile

from ...config import validate_loopback_host
from ...modules.price_checker.application.commands import (
    AddProcurementItemCommand,
    CancelReviewRunCommand,
    CompleteReviewRunCommand,
    ConfirmImportCommand,
    CreateImportPreviewCommand,
    CreateReviewRunCommand,
    ExcludeImportRowCommand,
    SetProcurementItemStatusCommand,
    StartReviewRunCommand,
    UpdateImportRowCommand,
)
from ...modules.price_checker.application.errors import ConcurrencyError
from ...modules.price_checker.application.services import PriceCheckerApplication
from ...modules.price_checker.domain.models import (
    InvalidTransitionError,
    ProcurementItemStatus,
    ReviewRunStatus,
    ValidationError,
)


SESSION_COOKIE = "pc_session"
_SESSION_TTL_SECONDS = 8 * 60 * 60
_MAX_SESSIONS = 1024
_MAX_MULTIPART_PARTS = 1000
_SESSION_STORE_LOCK = threading.Lock()

_RUN_ACTION_PATH = re.compile(r"^/runs/([^/]+)/(start|complete|cancel)$")
_ITEM_ACTION_PATH = re.compile(r"^/runs/([^/]+)/items/([^/]+)/(complete|fail|retry)$")

_RUN_STATUS_LABELS = {
    ReviewRunStatus.DRAFT: "Чернетка",
    ReviewRunStatus.RUNNING: "Виконується",
    ReviewRunStatus.COMPLETED: "Завершено",
    ReviewRunStatus.CANCELLED: "Скасовано",
}

_ITEM_STATUS_LABELS = {
    ProcurementItemStatus.DRAFT: "Чернетка",
    ProcurementItemStatus.PENDING: "Очікує",
    ProcurementItemStatus.FAILED: "Помилка",
    ProcurementItemStatus.COMPLETED: "Завершено",
}

_ITEM_ACTION_LABELS = {
    ProcurementItemStatus.PENDING: (("complete", "Позначити виконаною"), ("fail", "Позначити помилкою")),
    ProcurementItemStatus.FAILED: (("retry", "Повторити"),),
}

_ITEM_ACTION_TARGETS = {
    "complete": ProcurementItemStatus.COMPLETED,
    "fail": ProcurementItemStatus.FAILED,
    "retry": ProcurementItemStatus.PENDING,
}


def _run_action_forms(run_id: UUID, status: ReviewRunStatus, item_count: int) -> str:
    if status is ReviewRunStatus.DRAFT:
        disabled = " disabled" if item_count == 0 else ""
        hint = (
            ""
            if item_count
            else "<p>Щоб почати перевірку, додайте хоча б одну активну позицію.</p>"
        )
        return (
            hint
            + f'<form method="post" action="/runs/{run_id}/start">'
            f'<button{disabled}>Почати перевірку</button></form>'
            + f'<form method="post" action="/runs/{run_id}/cancel">'
            '<button>Скасувати перевірку</button></form>'
        )
    if status is ReviewRunStatus.RUNNING:
        return (
            f'<form method="post" action="/runs/{run_id}/complete">'
            '<button>Завершити перевірку</button></form>'
            f'<form method="post" action="/runs/{run_id}/cancel">'
            '<button>Скасувати перевірку</button></form>'
        )
    return ""


def _item_action_forms(run_id: UUID, item, status: ReviewRunStatus) -> str:
    if status is not ReviewRunStatus.RUNNING:
        return ""
    return "".join(
        f'<form method="post" action="/runs/{run_id}/items/{item.id}/{action}">'
        f'<button>{escape(label)}</button></form>'
        for action, label in _ITEM_ACTION_LABELS.get(item.status, ())
    )


class SessionStore:
    """In-memory CSRF session store; no third-party dependency."""

    def __init__(self, ttl_seconds: int = _SESSION_TTL_SECONDS, max_sessions: int = _MAX_SESSIONS) -> None:
        self._ttl = ttl_seconds
        self._max = max_sessions
        self._lock = threading.Lock()
        self._sessions: dict[str, tuple[str, float]] = {}

    def create(self) -> tuple[str, str]:
        session_id = secrets.token_urlsafe(32)
        token = secrets.token_urlsafe(32)
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            self._sessions[session_id] = (token, now + self._ttl)
        return session_id, token

    def token(self, session_id: str) -> str | None:
        now = time.monotonic()
        with self._lock:
            entry = self._sessions.get(session_id)
            if entry is None:
                return None
            token, expires_at = entry
            if expires_at <= now:
                del self._sessions[session_id]
                return None
            return token

    def _prune(self, now: float) -> None:
        for session_id in [
            key for key, (_, expires_at) in self._sessions.items() if expires_at <= now
        ]:
            del self._sessions[session_id]
        while len(self._sessions) >= self._max:
            oldest = min(self._sessions, key=lambda key: self._sessions[key][1])
            del self._sessions[oldest]


def _authority(host: str, port: int) -> str:
    address = ipaddress.ip_address(host)
    rendered = f"[{address}]" if address.version == 6 else str(address)
    return f"{rendered}:{port}"


def _parse_authority(value: str | None) -> tuple[str, int] | None:
    if not value or value != value.strip():
        return None
    if any(ord(character) < 33 or ord(character) == 127 for character in value):
        return None
    if value.count(":") > 1 and not value.startswith("["):
        return None
    try:
        parsed = urlsplit(f"//{value}")
    except ValueError:
        return None
    if parsed.scheme or parsed.netloc != value:
        return None
    if parsed.path or parsed.query or parsed.fragment:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    try:
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if hostname is None:
        return None
    if port is None:
        if value.endswith(":"):
            return None
        port = 80
    return hostname, port


def _parse_origin(value: str | None) -> tuple[str, int] | None:
    if not value or value == "null":
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme != "http":
        return None
    if parsed.path or parsed.query or parsed.fragment:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    try:
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if hostname is None:
        return None
    return hostname, port if port is not None else 80


def _multipart_boundary(content_type: str) -> bytes | None:
    if not content_type.startswith("multipart/form-data"):
        return None
    message = EmailMessage()
    message["Content-Type"] = content_type
    boundary = message.get_boundary()
    if not boundary:
        return None
    return boundary.encode("latin-1")


def _multipart_part_count(body: bytes, boundary: bytes) -> int:
    """Count MIME part delimiters, ignoring boundary-like bytes in content."""
    delimiter = b"--" + boundary
    count = 0

    if body.startswith(delimiter):
        following = body[len(delimiter):len(delimiter) + 2]
        if following == b"\r\n" or following[:1] in (b" ", b"\t"):
            count += 1

    position = body.find(b"\r\n" + delimiter)
    while position != -1:
        start = position + 2 + len(delimiter)
        following = body[start:start + 2]
        if following == b"\r\n" or body[start:start + 1] in (b" ", b"\t"):
            count += 1
        position = body.find(b"\r\n" + delimiter, position + 1)

    return count


class LocalRequestHandler(BaseHTTPRequestHandler):
    application: PriceCheckerApplication
    allowed_hosts: frozenset[str] = frozenset()

    def _session_store(self) -> SessionStore:
        with _SESSION_STORE_LOCK:
            store = getattr(self.server, "sessions", None)
            if store is None:
                store = SessionStore()
                self.server.sessions = store
            return store

    def _allowed_authorities(self) -> frozenset[str]:
        if self.allowed_hosts:
            return self.allowed_hosts
        address = self.server.server_address
        return frozenset({_authority(address[0], address[1])})

    def _current_session(self) -> tuple[str, str] | None:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        jar = SimpleCookie()
        try:
            jar.load(raw)
        except CookieError:
            return None
        morsel = jar.get(SESSION_COOKIE)
        if morsel is None:
            return None
        token = self._session_store().token(morsel.value)
        if token is None:
            return None
        return morsel.value, token

    def _reject(self, status: int, message: str, *, drain: bool = False) -> bool:
        if drain:
            self._drain_body()
        self._send_error(status, message)
        return False

    def _drain_body(self, limit: int = 4096) -> None:
        lengths = self.headers.get_all("Content-Length") or []
        if len(lengths) != 1 or re.fullmatch(r"[0-9]+", lengths[0]) is None:
            return
        remaining = int(lengths[0])
        if remaining > limit:
            return
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 8192))
            if not chunk:
                return
            remaining -= len(chunk)

    def _check_host(self) -> bool:
        parsed = _parse_authority(self.headers.get("Host"))
        if parsed is None:
            return self._reject(400, "Некоректний Host", drain=True)
        hostname, port = parsed
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            return self._reject(403, "Недозволений Host", drain=True)
        if not address.is_loopback or _authority(hostname, port) not in self._allowed_authorities():
            return self._reject(403, "Недозволений Host", drain=True)
        return True

    def _check_origin(self) -> bool:
        parsed = _parse_origin(self.headers.get("Origin"))
        if parsed is None:
            return self._reject(403, "Недозволений Origin", drain=True)
        hostname, port = parsed
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            return self._reject(403, "Недозволений Origin", drain=True)
        if not address.is_loopback or _authority(hostname, port) not in self._allowed_authorities():
            return self._reject(403, "Недозволений Origin", drain=True)
        return True

    def _check_csrf(self, values: dict[str, list[str]]) -> bool:
        session = self._current_session()
        if session is None:
            return self._reject(403, "Сесію не знайдено")
        _, expected = session
        provided_values = values.get("csrf_token", [])
        if len(provided_values) != 1:
            return self._reject(403, "Некоректний CSRF-токен")
        provided = provided_values[0]
        if not provided or not secrets.compare_digest(expected, provided):
            return self._reject(403, "Некоректний CSRF-токен")
        return True

    @staticmethod
    def _inject_csrf(body: bytes, token: str) -> bytes:
        """Add exactly one hidden csrf_token to each POST form.

        New state-changing forms must use method="post" and be closed with
        </form>; they are protected automatically. A form that already contains
        a csrf_token field is left unchanged, so injection is idempotent.
        """
        hidden = f'<input type="hidden" name="csrf_token" value="{escape(token)}">'

        def replace(match: re.Match[str]) -> str:
            block = match.group(0)
            if 'name="csrf_token"' in block:
                return block
            return block.replace("</form>", hidden + "</form>")

        text = body.decode("utf-8")
        return re.sub(r"<form\b.*?</form>", replace, text, flags=re.DOTALL).encode("utf-8")

    def do_GET(self) -> None:  # noqa: N802
        if not self._check_host():
            return
        session = self._current_session()
        set_cookie = None
        if session is None:
            session_id, token = self._session_store().create()
            set_cookie = session_id
        else:
            _, token = session
        parsed = urlsplit(self.path)
        is_import_page = (
            (parsed.path.startswith("/runs/") and "/imports/" in parsed.path)
            or parsed.path.startswith("/imports/")
        )
        is_run_page = (
            parsed.path.startswith("/runs/")
            and not parsed.path.endswith("/items")
            and not is_import_page
            and _RUN_ACTION_PATH.match(parsed.path) is None
            and _ITEM_ACTION_PATH.match(parsed.path) is None
        )
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
                run = next((candidate for candidate in runs if candidate.id == run_id), None)
                if run is None:
                    raise LookupError(f"review run not found: {run_id}")
                items = self.application.list_procurement_items(run_id)
                item_rows = "".join(
                    f"<li>{escape(item.name)} — {escape(str(item.quantity))} "
                    f"{escape(item.unit or '')}"
                    f"{(' — ' + escape(item.description)) if item.description else ''}"
                    f" — статус: {escape(_ITEM_STATUS_LABELS[item.status])}"
                    f"{_item_action_forms(run_id, item, run.status)}</li>"
                    for item in items
                ) or "<li>Позицій ще немає</li>"
                form = f'''<h2>Перевірка</h2>
<p>Статус: {escape(_RUN_STATUS_LABELS[run.status])}. Позицій: {len(items)}.</p>
{_run_action_forms(run_id, run.status, len(items))}'''
                if run.status is ReviewRunStatus.DRAFT:
                    form += f'''<h2>Додати закупівельну позицію</h2>
<form method="post" action="/runs/{run_id}/items">
<label>Назва <input name="name" required></label><br>
<label>Кількість <input name="quantity" required></label><br>
<label>Одиниця виміру <input name="unit" required></label><br>
<label>Технічні характеристики <textarea name="description"></textarea></label><br>
<button>Додати позицію</button></form>'''
                form += f'''<h2>Позиції</h2><ul>{item_rows}</ul>'''
                if run.status is ReviewRunStatus.DRAFT:
                    upload_idempotency_key = uuid4()
                    form += f'''<h2>Імпорт XLSX</h2>
<form method="post" enctype="multipart/form-data" action="/runs/{run_id}/imports/preview">
<input type="hidden" name="idempotency_key" value="{escape(str(upload_idempotency_key))}">
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
                self._send_body(200, self._inject_csrf(body, token), set_cookie=set_cookie)
                return
            body = f'''<!doctype html><html lang="uk"><meta charset="utf-8">
<title>Price Checker</title><h1>Price Checker</h1>
<p>Ручне додавання закупівельних позицій.</p>
<form method="post" action="/runs"><button>Створити перевірку</button></form>
<h2>Перевірки</h2><ul>{rows}</ul>{form}</html>'''.encode("utf-8")
            self._send_body(200, self._inject_csrf(body, token), set_cookie=set_cookie)
        except LookupError:
            self._send_error(404, "Перевірку не знайдено")
        except (InvalidTransitionError, ConcurrencyError):
            self._send_error(409, "Операція конфліктує з іншою зміною")
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
        run_action = _RUN_ACTION_PATH.match(path)
        item_action = _ITEM_ACTION_PATH.match(path)
        if (
            path != "/runs"
            and not is_item
            and not is_preview
            and not is_confirm
            and not is_exclude
            and not is_update
            and run_action is None
            and item_action is None
        ):
            self._send_error(404, "Сторінку не знайдено")
            return
        if not self._check_host():
            return
        if not self._check_origin():
            return
        try:
            if self.headers.get("Transfer-Encoding") is not None:
                self._send_error(400, "Chunked transfer encoding не підтримується")
                return
            lengths = self.headers.get_all("Content-Length") or []
            if len(lengths) != 1 or re.fullmatch(r"[0-9]+", lengths[0]) is None:
                self._send_error(400, "Некоректний Content-Length")
                return
            length = int(lengths[0])
            max_body = 26 * 1024 * 1024 if is_preview else 4096
            if length > max_body:
                self._send_error(413, "Запит надто великий")
                return
            body = self.rfile.read(length)
            content_type = self.headers.get("Content-Type", "")
            if is_preview:
                values, file_bytes = self._multipart_values(body, content_type)
            else:
                values = parse_qs(body.decode("utf-8"), strict_parsing=True, keep_blank_values=True)
                file_bytes = None
            if not self._check_csrf(values):
                return
            if is_preview:
                run_id = UUID(path[len("/runs/"):-len("/imports/preview")])
                temp_path = None
                try:
                    suffix = ".xlsx"
                    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as upload:
                        upload.write(file_bytes)
                        temp_path = upload.name
                    mapping = {
                        "name": values.get("name_column", [""])[0],
                        "quantity": values.get("quantity_column", [""])[0],
                        "unit": values.get("unit_column", [""])[0],
                    }
                    if values.get("description_column", [""])[0]:
                        mapping["description"] = values["description_column"][0]
                    fingerprint = hashlib.sha256(file_bytes).hexdigest()
                    preview = self.application.create_import_preview(CreateImportPreviewCommand(
                        run_id=run_id,
                        source_path=temp_path,
                        source_fingerprint=fingerprint,
                        idempotency_key=values.get("idempotency_key", [""])[0],
                        sheet=values.get("sheet", [""])[0],
                        header_row=int(values.get("header_row", ["1"])[0]),
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
            if run_action is not None:
                run_id = UUID(run_action.group(1))
                action = run_action.group(2)
                if action == "start":
                    self.application.start_review_run(StartReviewRunCommand(run_id))
                elif action == "complete":
                    self.application.complete_review_run(CompleteReviewRunCommand(run_id))
                else:
                    self.application.cancel_review_run(CancelReviewRunCommand(run_id))
                location = f"/runs/{run_id}"
            elif item_action is not None:
                run_id = UUID(item_action.group(1))
                item_id = UUID(item_action.group(2))
                self.application.set_procurement_item_status(SetProcurementItemStatusCommand(
                    run_id, item_id, _ITEM_ACTION_TARGETS[item_action.group(3)],
                ))
                location = f"/runs/{run_id}"
            elif is_confirm:
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
        except (InvalidTransitionError, ConcurrencyError):
            self._send_error(409, "Операція конфліктує з іншою зміною")
        except (UnicodeDecodeError, ValueError, ValidationError):
            self._send_error(400, "Некоректні дані запиту")
        except Exception:
            self._send_error(500, "Внутрішня помилка застосунку")

    @staticmethod
    def _multipart_values(body: bytes, content_type: str) -> tuple[dict[str, list[str]], bytes]:
        if not content_type.startswith("multipart/form-data"):
            raise ValueError("очікується multipart/form-data")
        boundary = _multipart_boundary(content_type)
        if boundary is None:
            raise ValueError("очікується multipart/form-data з boundary")
        if _multipart_part_count(body, boundary) > _MAX_MULTIPART_PARTS:
            raise ValueError("multipart містить забагато частин")
        message = BytesParser(policy=default).parsebytes(
            f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + body
        )
        if not message.is_multipart():
            raise ValueError("некоректний multipart-запит")
        values: dict[str, list[str]] = {}
        file_bytes: bytes | None = None
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if name == "file":
                filename = part.get_filename() or ""
                if not filename.lower().endswith(".xlsx"):
                    raise ValueError("підтримуються лише файли .xlsx")
                file_bytes = part.get_payload(decode=True) or b""
            elif name:
                values.setdefault(name, []).append(
                    (part.get_payload(decode=True) or b"").decode("utf-8")
                )
        if file_bytes is None or not file_bytes:
            raise ValueError("файл XLSX не передано")
        if len(file_bytes) > 25 * 1024 * 1024:
            raise ValueError("розмір файлу перевищує дозволений ліміт")
        return values, file_bytes

    def _send_body(self, status: int, body: bytes, *, set_cookie: str | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if set_cookie is not None:
            self.send_header(
                "Set-Cookie",
                f"{SESSION_COOKIE}={set_cookie}; HttpOnly; SameSite=Strict; Path=/",
            )
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
    allowed_hosts = frozenset({_authority(host, port)})
    handler = type(
        "BoundLocalRequestHandler",
        (LocalRequestHandler,),
        {"application": application, "allowed_hosts": allowed_hosts},
    )
    address_family = socket.AF_INET6 if ipaddress.ip_address(host).version == 6 else socket.AF_INET
    server_class = type(
        "BoundThreadingHTTPServer",
        (ThreadingHTTPServer,),
        {"address_family": address_family},
    )
    with server_class((host, port), handler) as server:
        server.sessions = SessionStore()
        server.serve_forever()
