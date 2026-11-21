import http.client
import re
import socket
import threading
import unittest
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlencode
from uuid import UUID

from openpyxl import Workbook

from src.ai_assistance.bootstrap import build_application
from src.ai_assistance.config import Settings
from src.ai_assistance.presentation.http.server import (
    LocalRequestHandler,
    SessionStore,
    _authority,
    _multipart_part_count,
    _parse_authority,
)


class HttpSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.application, self.database = build_application(
            Settings(Path(self.tmp.name) / "app.sqlite3")
        )
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            type("Handler", (LocalRequestHandler,), {"application": self.application}),
        )
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.database.close()
        self.tmp.cleanup()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_address[1], timeout=5
        )
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            data = response.read()
            return response.status, dict(response.getheaders()), data
        finally:
            connection.close()

    def _get(self, path="/", host=None, extra=None):
        headers = {}
        if host is not None:
            headers["Host"] = host
        if extra:
            headers.update(extra)
        return self._request("GET", path, headers=headers)

    def _session(self):
        status, headers, body = self._get("/")
        self.assertEqual(status, 200)
        cookie = headers["Set-Cookie"].split(";")[0]
        token = re.search(r'name="csrf_token" value="([^"]+)"', body.decode("utf-8")).group(1)
        return cookie, token, body.decode("utf-8")

    def _post(
        self,
        path,
        data=None,
        cookie=None,
        token=None,
        origin="",
        include_csrf=True,
        csrf=None,
        host=None,
    ):
        payload = {key: str(value) for key, value in (data or {}).items()}
        if include_csrf:
            payload["csrf_token"] = token if csrf is None else csrf
        body = urlencode(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Content-Length": str(len(body)),
        }
        if cookie is not None:
            headers["Cookie"] = cookie
        if origin is not None:
            headers["Origin"] = origin
        if host is not None:
            headers["Host"] = host
        return self._request("POST", path, body=body, headers=headers)

    def _post_raw(self, body, cookie, origin=None, content_type="application/x-www-form-urlencoded"):
        headers = {"Content-Type": content_type, "Content-Length": str(len(body))}
        if cookie is not None:
            headers["Cookie"] = cookie
        if origin is not None:
            headers["Origin"] = origin
        return self._request("POST", "/runs", body=body, headers=headers)

    def _raw_post(self, path, headers, body=b""):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_address[1], timeout=5
        )
        try:
            connection.putrequest("POST", path)
            for key, value in headers:
                connection.putheader(key, value)
            connection.endheaders()
            if body:
                connection.send(body)
            response = connection.getresponse()
            response.read()
            return response.status
        finally:
            connection.close()

    def _multipart(self, fields, file_path: Path, boundary="b3-boundary"):
        chunks = []
        for key, value in fields.items():
            items = value if isinstance(value, list) else [value]
            for item in items:
                chunks.append(
                    f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{item}\r\n'.encode()
                )
        payload = file_path.read_bytes()
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="items.xlsx"\r\n'
            f"Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n".encode()
            + payload
            + f"\r\n--{boundary}--\r\n".encode()
        )
        return b"".join(chunks), f"multipart/form-data; boundary={boundary}"

    def _multipart_above_limit(self, file_path: Path, boundary="limit-boundary"):
        chunks = []
        for index in range(1001):
            chunks.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="extra{index}"\r\n\r\nx\r\n'.encode()
            )
        payload = file_path.read_bytes()
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="items.xlsx"\r\n'
            f"Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n".encode()
            + payload
            + f"\r\n--{boundary}--\r\n".encode()
        )
        return b"".join(chunks), f"multipart/form-data; boundary={boundary}"

    def _count(self, table: str) -> int:
        with self.database.transaction() as connection:
            return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def _make_xlsx(self) -> Path:
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Sheet1"
        sheet.append(["Name", "Qty", "Unit"])
        sheet.append(["Bolt", 2, "pcs"])
        path = Path(self.tmp.name) / "items.xlsx"
        workbook.save(path)
        return path

    def _make_xlsx_with_embedded_boundary(self, boundary: str) -> Path:
        path = self._make_xlsx()
        with zipfile.ZipFile(path, "a") as archive:
            archive.comment = (b"--" + boundary.encode("utf-8")) * 2000
        return path

    def _create_run(self, cookie, token):
        status, _, _ = self._post("/runs", cookie=cookie, token=token, origin=self.base)
        self.assertEqual(status, 303)
        return self.application.dashboard()[0].id

    def _create_preview(self, run_id, cookie, token):
        body, content_type = self._multipart(
            {
                "idempotency_key": "security-operation",
                "sheet": "Sheet1",
                "header_row": "1",
                "name_column": "Name",
                "quantity_column": "Qty",
                "unit_column": "Unit",
                "csrf_token": token,
            },
            self._make_xlsx(),
        )
        status, headers, _ = self._request(
            "POST",
            f"/runs/{run_id}/imports/preview",
            body=body,
            headers={
                "Content-Type": content_type,
                "Content-Length": str(len(body)),
                "Cookie": cookie,
                "Origin": self.base,
            },
        )
        self.assertEqual(status, 303)
        return headers["Location"].split("/")[-1]

    # ------------------------------------------------------------------
    # Host validation
    # ------------------------------------------------------------------

    def test_valid_ipv4_host_is_accepted(self) -> None:
        status, _, _ = self._get("/")
        self.assertEqual(status, 200)

    def test_foreign_hostname_is_rejected(self) -> None:
        status, _, _ = self._get("/", host="evil.example.com")
        self.assertEqual(status, 403)

    def test_wrong_port_is_rejected(self) -> None:
        status, _, _ = self._get("/", host="127.0.0.1:1")
        self.assertEqual(status, 403)

    def test_malformed_host_is_rejected(self) -> None:
        status, _, _ = self._get("/", host="127.0.0.1:notaport")
        self.assertEqual(status, 400)

    def test_forwarded_headers_are_ignored(self) -> None:
        status, _, _ = self._get(
            "/",
            extra={"X-Forwarded-Host": "evil.example.com", "Forwarded": "host=evil.example.com"},
        )
        self.assertEqual(status, 200)

    def test_ipv6_authority_parsing(self) -> None:
        self.assertEqual(_parse_authority("[::1]:8765"), ("::1", 8765))
        self.assertEqual(_authority("::1", 8765), "[::1]:8765")
        self.assertIsNone(_parse_authority("::1:8765"))

    def test_ipv6_loopback_http_if_supported(self) -> None:
        try:
            server_class = type(
                "V6Server",
                (ThreadingHTTPServer,),
                {"address_family": socket.AF_INET6},
            )
            server = server_class(
                ("::1", 0),
                type("Handler", (LocalRequestHandler,), {"application": self.application}),
            )
        except OSError:
            self.skipTest("IPv6 loopback is not available")
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            connection = http.client.HTTPConnection("::1", server.server_address[1], timeout=5)
            try:
                connection.request("GET", "/")
                response = connection.getresponse()
                response.read()
                self.assertEqual(response.status, 200)
            finally:
                connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    # ------------------------------------------------------------------
    # Origin validation
    # ------------------------------------------------------------------

    def test_exact_local_origin_is_accepted(self) -> None:
        cookie, token, _ = self._session()
        status, _, _ = self._post("/runs", cookie=cookie, token=token, origin=self.base)
        self.assertEqual(status, 303)

    def test_foreign_origin_is_rejected(self) -> None:
        cookie, token, _ = self._session()
        status, _, _ = self._post(
            "/runs", cookie=cookie, token=token, origin="http://evil.example.com"
        )
        self.assertEqual(status, 403)

    def test_null_origin_is_rejected(self) -> None:
        cookie, token, _ = self._session()
        status, _, _ = self._post("/runs", cookie=cookie, token=token, origin="null")
        self.assertEqual(status, 403)

    def test_missing_origin_is_rejected(self) -> None:
        cookie, token, _ = self._session()
        status, _, _ = self._post("/runs", cookie=cookie, token=token, origin=None)
        self.assertEqual(status, 403)

    def test_malformed_origin_is_rejected(self) -> None:
        cookie, token, _ = self._session()
        status, _, _ = self._post("/runs", cookie=cookie, token=token, origin="http://")
        self.assertEqual(status, 403)

    def test_similar_looking_origin_is_rejected(self) -> None:
        cookie, token, _ = self._session()
        port = self.server.server_address[1]
        for origin in (
            f"http://127.0.0.1:{port}.evil.example.com",
            f"http://127.0.0.1.evil.example.com:{port}",
            f"http://evil.example.com:{port}",
        ):
            with self.subTest(origin=origin):
                status, _, _ = self._post("/runs", cookie=cookie, token=token, origin=origin)
                self.assertEqual(status, 403)

    # ------------------------------------------------------------------
    # CSRF
    # ------------------------------------------------------------------

    def test_valid_csrf_token_is_accepted(self) -> None:
        cookie, token, _ = self._session()
        status, _, _ = self._post("/runs", cookie=cookie, token=token, origin=self.base)
        self.assertEqual(status, 303)

    def test_missing_csrf_token_is_rejected(self) -> None:
        cookie, token, _ = self._session()
        status, _, _ = self._post(
            "/runs", cookie=cookie, token=token, origin=self.base, include_csrf=False
        )
        self.assertEqual(status, 403)

    def test_invalid_csrf_token_is_rejected(self) -> None:
        cookie, token, _ = self._session()
        status, _, _ = self._post(
            "/runs", cookie=cookie, token=token, origin=self.base, csrf="invalid-token"
        )
        self.assertEqual(status, 403)

    def test_csrf_token_from_another_session_is_rejected(self) -> None:
        cookie_one, _, _ = self._session()
        _, token_two, _ = self._session()
        status, _, _ = self._post(
            "/runs", cookie=cookie_one, token=token_two, origin=self.base
        )
        self.assertEqual(status, 403)

    def test_csrf_token_does_not_appear_in_urls(self) -> None:
        _, token, body = self._session()
        urls = re.findall(r'(?:href|action)="([^"]*)"', body)
        self.assertTrue(urls)
        for url in urls:
            self.assertNotIn(token, url)
        self.assertNotIn("csrf_token=", body)

    def test_session_cookie_flags(self) -> None:
        _, headers, _ = self._get("/")
        cookie = headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertIn("Path=/", cookie)
        self.assertNotIn("Secure", cookie)

    # ------------------------------------------------------------------
    # Every POST workflow rejects missing CSRF
    # ------------------------------------------------------------------

    def test_all_post_routes_reject_missing_csrf(self) -> None:
        cookie, token, _ = self._session()
        run_id = self._create_run(cookie, token)
        preview_id = self._create_preview(run_id, cookie, token)
        row_id = self.application.get_import_preview(UUID(preview_id), run_id).rows[0].id

        status, _, _ = self._post(
            "/runs", cookie=cookie, token=token, origin=self.base, include_csrf=False
        )
        self.assertEqual(status, 403)

        status, _, _ = self._post(
            f"/runs/{run_id}/items",
            data={"name": "x", "quantity": "1", "unit": "pcs"},
            cookie=cookie,
            token=token,
            origin=self.base,
            include_csrf=False,
        )
        self.assertEqual(status, 403)

        status, _, _ = self._post(
            f"/runs/{run_id}/imports/{preview_id}/confirm",
            data={"idempotency_key": "security-operation"},
            cookie=cookie,
            token=token,
            origin=self.base,
            include_csrf=False,
        )
        self.assertEqual(status, 403)

        status, _, _ = self._post(
            f"/runs/{run_id}/imports/{preview_id}/rows/{row_id}",
            data={"name": "x", "quantity": "1", "unit": "pcs"},
            cookie=cookie,
            token=token,
            origin=self.base,
            include_csrf=False,
        )
        self.assertEqual(status, 403)

        status, _, _ = self._post(
            f"/runs/{run_id}/imports/{preview_id}/rows/{row_id}/exclude",
            cookie=cookie,
            token=token,
            origin=self.base,
            include_csrf=False,
        )
        self.assertEqual(status, 403)

        body, content_type = self._multipart(
            {
                "idempotency_key": "no-token",
                "sheet": "Sheet1",
                "header_row": "1",
                "name_column": "Name",
                "quantity_column": "Qty",
                "unit_column": "Unit",
            },
            self._make_xlsx(),
        )
        status, _, _ = self._request(
            "POST",
            f"/runs/{run_id}/imports/preview",
            body=body,
            headers={
                "Content-Type": content_type,
                "Content-Length": str(len(body)),
                "Cookie": cookie,
                "Origin": self.base,
            },
        )
        self.assertEqual(status, 403)

    # ------------------------------------------------------------------
    # Regression
    # ------------------------------------------------------------------

    def test_get_pages_still_work(self) -> None:
        for path in ("/",):
            with self.subTest(path=path):
                status, _, _ = self._get(path)
                self.assertEqual(status, 200)

    def test_valid_post_workflows_still_work(self) -> None:
        cookie, token, _ = self._session()
        run_id = self._create_run(cookie, token)

        status, _, _ = self._post(
            f"/runs/{run_id}/items",
            data={"name": "Pump", "quantity": "2", "unit": "pcs"},
            cookie=cookie,
            token=token,
            origin=self.base,
        )
        self.assertEqual(status, 303)
        self.assertEqual(len(self.application.list_procurement_items(run_id)), 1)

        preview_id = self._create_preview(run_id, cookie, token)
        status, _, _ = self._post(
            f"/runs/{run_id}/imports/{preview_id}/confirm",
            data={"idempotency_key": "security-operation", "acknowledge_warnings": "on"},
            cookie=cookie,
            token=token,
            origin=self.base,
        )
        self.assertEqual(status, 303)
        self.assertEqual(len(self.application.list_procurement_items(run_id)), 2)

    def test_invalid_requests_do_not_invoke_application_commands(self) -> None:
        cookie, token, _ = self._session()
        before = len(self.application.dashboard())
        status, _, _ = self._post(
            "/runs", cookie=cookie, token=token, origin="http://evil.example.com"
        )
        self.assertEqual(status, 403)
        self.assertEqual(len(self.application.dashboard()), before)

    def test_import_upload_size_limit_is_unchanged(self) -> None:
        cookie, _, _ = self._session()
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_address[1], timeout=5
        )
        try:
            connection.request(
                "POST",
                "/runs",
                body=b"",
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Content-Length": str(27 * 1024 * 1024),
                    "Cookie": cookie,
                    "Origin": self.base,
                },
            )
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 413)
        finally:
            connection.close()

    def test_unknown_page_returns_404(self) -> None:
        status, _, _ = self._get("/unknown")
        self.assertEqual(status, 404)

    def test_invalid_input_returns_400(self) -> None:
        cookie, token, _ = self._session()
        run_id = self._create_run(cookie, token)
        status, _, _ = self._post(
            f"/runs/{run_id}/items",
            data={"name": "Pump", "quantity": "not-a-number", "unit": "pcs"},
            cookie=cookie,
            token=token,
            origin=self.base,
        )
        self.assertEqual(status, 400)


    # ------------------------------------------------------------------
    # Content-Length and transfer encoding
    # ------------------------------------------------------------------

    def test_malformed_content_length_is_rejected(self) -> None:
        cookie, _, _ = self._session()
        before = len(self.application.dashboard())
        for value in ("abc", "-1", "1.5", "", "+1", "1_0"):
            with self.subTest(value=value):
                status = self._raw_post(
                    "/runs",
                    [("Origin", self.base), ("Cookie", cookie), ("Content-Length", value)],
                )
                self.assertEqual(status, 400)
        self.assertEqual(len(self.application.dashboard()), before)

    def test_missing_content_length_is_rejected(self) -> None:
        cookie, _, _ = self._session()
        status = self._raw_post("/runs", [("Origin", self.base), ("Cookie", cookie)])
        self.assertEqual(status, 400)

    def test_duplicate_content_length_is_rejected(self) -> None:
        cookie, _, _ = self._session()
        status = self._raw_post(
            "/runs",
            [
                ("Origin", self.base),
                ("Cookie", cookie),
                ("Content-Length", "0"),
                ("Content-Length", "0"),
            ],
        )
        self.assertEqual(status, 400)

    def test_transfer_encoding_is_rejected(self) -> None:
        cookie, _, _ = self._session()
        status = self._raw_post(
            "/runs",
            [("Origin", self.base), ("Cookie", cookie), ("Transfer-Encoding", "chunked")],
        )
        self.assertEqual(status, 400)

    def test_oversized_non_preview_body_is_rejected(self) -> None:
        cookie, _, _ = self._session()
        status = self._raw_post(
            "/runs",
            [("Origin", self.base), ("Cookie", cookie), ("Content-Length", "4097")],
        )
        self.assertEqual(status, 413)

    def test_invalid_multipart_is_rejected(self) -> None:
        cookie, token, _ = self._session()
        run_id = self._create_run(cookie, token)
        body = b"not-a-multipart"
        status, _, _ = self._request(
            "POST",
            f"/runs/{run_id}/imports/preview",
            body=body,
            headers={
                "Content-Type": "multipart/form-data",
                "Content-Length": str(len(body)),
                "Cookie": cookie,
                "Origin": self.base,
            },
        )
        self.assertEqual(status, 400)

    def test_multipart_below_part_limit_is_accepted(self) -> None:
        cookie, token, _ = self._session()
        run_id = self._create_run(cookie, token)
        fields = {
            "idempotency_key": "below-limit",
            "sheet": "Sheet1",
            "header_row": "1",
            "name_column": "Name",
            "quantity_column": "Qty",
            "unit_column": "Unit",
            "csrf_token": token,
        }
        for index in range(50):
            fields[f"extra{index}"] = "x"
        body, content_type = self._multipart(fields, self._make_xlsx())
        status, _, _ = self._request(
            "POST",
            f"/runs/{run_id}/imports/preview",
            body=body,
            headers={
                "Content-Type": content_type,
                "Content-Length": str(len(body)),
                "Cookie": cookie,
                "Origin": self.base,
            },
        )
        self.assertEqual(status, 303)

    def test_multipart_above_part_limit_is_rejected_without_side_effects(self) -> None:
        cookie, token, _ = self._session()
        run_id = self._create_run(cookie, token)
        operations_before = self._count("import_operations")
        items_before = self._count("procurement_items")
        body, content_type = self._multipart_above_limit(self._make_xlsx())
        status, _, _ = self._request(
            "POST",
            f"/runs/{run_id}/imports/preview",
            body=body,
            headers={
                "Content-Type": content_type,
                "Content-Length": str(len(body)),
                "Cookie": cookie,
                "Origin": self.base,
            },
        )
        self.assertEqual(status, 400)
        self.assertEqual(self._count("import_operations"), operations_before)
        self.assertEqual(self._count("procurement_items"), items_before)
        self.assertEqual(self.application.dashboard()[0].item_count, 0)

    def test_part_count_ignores_boundary_inside_content(self) -> None:
        boundary = b"embedded-boundary"
        content = (b"--" + boundary) * 2000
        body = (
            b"--embedded-boundary\r\n"
            b'Content-Disposition: form-data; name="file"; filename="x.xlsx"\r\n'
            b"\r\n" + content + b"\r\n--embedded-boundary--\r\n"
        )
        self.assertEqual(_multipart_part_count(body, boundary), 1)

    def test_part_count_counts_parts_and_closing(self) -> None:
        boundary = b"b"
        body = (
            b"--b\r\n\r\none\r\n"
            b"--b\r\n\r\ntwo\r\n"
            b"--b\r\n\r\nthree\r\n"
            b"--b--\r\n"
        )
        self.assertEqual(_multipart_part_count(body, boundary), 3)

    def test_part_count_handles_leading_crlf(self) -> None:
        boundary = b"b"
        body = b"preamble\r\n--b\r\n\r\none\r\n--b--\r\n"
        self.assertEqual(_multipart_part_count(body, boundary), 1)

    def test_boundary_inside_file_content_does_not_cause_false_rejection(self) -> None:
        cookie, token, _ = self._session()
        run_id = self._create_run(cookie, token)
        boundary = "embedded-boundary"
        body, content_type = self._multipart(
            {
                "idempotency_key": "embedded",
                "sheet": "Sheet1",
                "header_row": "1",
                "name_column": "Name",
                "quantity_column": "Qty",
                "unit_column": "Unit",
                "csrf_token": token,
            },
            self._make_xlsx_with_embedded_boundary(boundary),
            boundary=boundary,
        )
        status, _, _ = self._request(
            "POST",
            f"/runs/{run_id}/imports/preview",
            body=body,
            headers={
                "Content-Type": content_type,
                "Content-Length": str(len(body)),
                "Cookie": cookie,
                "Origin": self.base,
            },
        )
        self.assertEqual(status, 303)

    # ------------------------------------------------------------------
    # Duplicate CSRF tokens
    # ------------------------------------------------------------------

    def test_duplicate_identical_csrf_token_is_rejected(self) -> None:
        cookie, token, _ = self._session()
        body = urlencode([("csrf_token", token), ("csrf_token", token)]).encode("utf-8")
        status, _, _ = self._post_raw(body, cookie, origin=self.base)
        self.assertEqual(status, 403)

    def test_duplicate_different_csrf_token_is_rejected(self) -> None:
        cookie, token, _ = self._session()
        body = urlencode(
            [("csrf_token", token), ("csrf_token", "a-different-token")]
        ).encode("utf-8")
        status, _, _ = self._post_raw(body, cookie, origin=self.base)
        self.assertEqual(status, 403)

    def test_empty_then_valid_csrf_token_is_rejected(self) -> None:
        cookie, token, _ = self._session()
        body = urlencode([("csrf_token", ""), ("csrf_token", token)]).encode("utf-8")
        status, _, _ = self._post_raw(body, cookie, origin=self.base)
        self.assertEqual(status, 403)

    def test_valid_then_empty_csrf_token_is_rejected(self) -> None:
        cookie, token, _ = self._session()
        body = urlencode([("csrf_token", token), ("csrf_token", "")]).encode("utf-8")
        status, _, _ = self._post_raw(body, cookie, origin=self.base)
        self.assertEqual(status, 403)

    def test_duplicate_csrf_token_in_multipart_is_rejected(self) -> None:
        cookie, token, _ = self._session()
        run_id = self._create_run(cookie, token)
        body, content_type = self._multipart(
            {
                "idempotency_key": "duplicate",
                "sheet": "Sheet1",
                "header_row": "1",
                "name_column": "Name",
                "quantity_column": "Qty",
                "unit_column": "Unit",
                "csrf_token": [token, token],
            },
            self._make_xlsx(),
        )
        status, _, _ = self._request(
            "POST",
            f"/runs/{run_id}/imports/preview",
            body=body,
            headers={
                "Content-Type": content_type,
                "Content-Length": str(len(body)),
                "Cookie": cookie,
                "Origin": self.base,
            },
        )
        self.assertEqual(status, 403)

    # ------------------------------------------------------------------
    # Form injection
    # ------------------------------------------------------------------

    def test_each_form_has_exactly_one_csrf_token(self) -> None:
        cookie, token, _ = self._session()
        run_id = self._create_run(cookie, token)
        status, _, body = self._get(f"/runs/{run_id}")
        self.assertEqual(status, 200)
        forms = re.findall(r"<form\b.*?</form>", body.decode("utf-8"), re.DOTALL)
        self.assertGreaterEqual(len(forms), 2)
        for form in forms:
            self.assertEqual(form.count('name="csrf_token"'), 1)

        preview_id = self._create_preview(run_id, cookie, token)
        status, _, page = self._get(f"/runs/{run_id}/imports/{preview_id}")
        self.assertEqual(status, 200)
        import_forms = re.findall(r"<form\b.*?</form>", page.decode("utf-8"), re.DOTALL)
        self.assertGreaterEqual(len(import_forms), 2)
        for form in import_forms:
            self.assertEqual(form.count('name="csrf_token"'), 1)

    def test_injection_is_idempotent_per_form(self) -> None:
        html = (
            b'<form method="post" action="/a"></form>'
            b'<form method="post" action="/b">'
            b'<input type="hidden" name="csrf_token" value="old"></form>'
        )
        result = LocalRequestHandler._inject_csrf(html, "new-token").decode("utf-8")
        forms = re.findall(r"<form\b.*?</form>", result, re.DOTALL)
        self.assertEqual(len(forms), 2)
        self.assertEqual(forms[0].count('name="csrf_token"'), 1)
        self.assertEqual(forms[1].count('name="csrf_token"'), 1)
        self.assertIn('value="new-token"', forms[0])
        self.assertIn('value="old"', forms[1])

    def test_token_not_in_title_or_error_pages(self) -> None:
        _, token, body = self._session()
        title = re.search(r"<title>(.*?)</title>", body)
        self.assertIsNotNone(title)
        self.assertNotIn(token, title.group(1))
        status, _, error_body = self._get("/unknown")
        self.assertEqual(status, 404)
        self.assertNotIn(token, error_body.decode("utf-8"))

    # ------------------------------------------------------------------
    # Session expiration and eviction
    # ------------------------------------------------------------------

    def test_expired_session_is_rejected_and_new_get_creates_session(self) -> None:
        self.server.sessions = SessionStore(ttl_seconds=0)
        cookie, token, _ = self._session()
        status, _, _ = self._post("/runs", cookie=cookie, token=token, origin=self.base)
        self.assertEqual(status, 403)
        status, headers, _ = self._get("/", extra={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertNotEqual(headers["Set-Cookie"].split(";")[0], cookie)

    def test_evicted_session_is_rejected_and_new_get_creates_session(self) -> None:
        self.server.sessions = SessionStore(max_sessions=1)
        cookie_one, token_one, _ = self._session()
        self._session()
        status, _, _ = self._post(
            "/runs", cookie=cookie_one, token=token_one, origin=self.base
        )
        self.assertEqual(status, 403)
        status, headers, _ = self._get("/", extra={"Cookie": cookie_one})
        self.assertEqual(status, 200)
        self.assertNotEqual(headers["Set-Cookie"].split(";")[0], cookie_one)

    def test_session_store_concurrent_access(self) -> None:
        store = SessionStore(max_sessions=1000)
        errors = []

        def worker() -> None:
            try:
                session_id, token = store.create()
                if store.token(session_id) != token:
                    errors.append("token mismatch")
            except Exception as exc:  # pragma: no cover - defensive
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(100)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertFalse([thread for thread in threads if thread.is_alive()])
        self.assertEqual(errors, [])

    def test_session_ids_and_tokens_are_unpredictable(self) -> None:
        store = SessionStore()
        sessions = [store.create() for _ in range(200)]
        identifiers = [session_id for session_id, _ in sessions]
        tokens = [token for _, token in sessions]
        self.assertEqual(len(set(identifiers)), 200)
        self.assertEqual(len(set(tokens)), 200)
        self.assertTrue(all(len(value) >= 32 for value in identifiers))
        self.assertTrue(all(len(value) >= 32 for value in tokens))


if __name__ == "__main__":
    unittest.main()