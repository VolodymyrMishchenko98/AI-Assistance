import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from http.server import ThreadingHTTPServer

from src.ai_assistance.config import Settings
from src.ai_assistance.bootstrap import build_application
from src.ai_assistance.presentation.http.server import LocalRequestHandler, serve


class PresentationTests(unittest.TestCase):
    def test_real_server_handles_parallel_gets_and_shuts_down(self) -> None:
        with TemporaryDirectory() as directory:
            application, database = build_application(Settings(Path(directory) / "app.sqlite3"))
            server = ThreadingHTTPServer(("127.0.0.1", 0), type("Handler", (LocalRequestHandler,), {"application": application}))
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_address[1]}/"
                responses = []
                errors = []

                def request() -> None:
                    try:
                        with urlopen(url, timeout=2) as response:
                            responses.append((response.status, response.read()))
                    except Exception as exc:
                        errors.append(exc)

                workers = [threading.Thread(target=request) for _ in range(8)]
                for worker in workers:
                    worker.start()
                for worker in workers:
                    worker.join(timeout=3)
                self.assertFalse(errors)
                self.assertEqual(len(responses), 8)
                self.assertTrue(all(status == 200 and b"Price Checker" in body for status, body in responses))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)
                database.close()
            self.assertFalse(thread.is_alive())

    def test_server_rejects_non_loopback_binding(self) -> None:
        with self.assertRaises(ValueError):
            serve(None, "0.0.0.0", 8765)
        with self.assertRaises(ValueError):
            serve(None, "192.168.1.10", 8765)

    def test_malformed_post_gets_controlled_response(self) -> None:
        with TemporaryDirectory() as directory:
            application, database = build_application(Settings(Path(directory) / "app.sqlite3"))
            server = ThreadingHTTPServer(("127.0.0.1", 0), type("Handler", (LocalRequestHandler,), {"application": application}))
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                request = Request(
                    f"http://127.0.0.1:{server.server_address[1]}/runs",
                    data=b"%FF", method="POST",
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                try:
                    urlopen(request, timeout=2)
                except HTTPError as error:
                    raised = error
                    error.close()
                else:
                    self.fail("expected HTTP 400")
                self.assertIn("HTTP Error 400", str(raised))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)
                database.close()
