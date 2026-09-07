import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import RunResult
from server import MAX_BODY_BYTES, _terminal_safe, build_server
from session import Session


class TestTerminalSafe(unittest.TestCase):
    """Candidate SQL must not be able to repaint the interviewer's terminal."""

    def test_strips_ansi_and_control_chars(self):
        hostile = "SELECT 1 \x1b[2K\x1b[1A FAKE ok \x07\x08\x7f\x9b"
        out = _terminal_safe(hostile, 200)
        for bad in ("\x1b", "\x07", "\x08", "\x7f", "\x9b"):
            self.assertNotIn(bad, out)
        self.assertIn("SELECT 1", out)

    def test_collapses_newlines_to_one_line(self):
        out = _terminal_safe("SELECT\n  1,\n  2", 200)
        self.assertEqual(out, "SELECT 1, 2")

    def test_respects_limit(self):
        self.assertEqual(len(_terminal_safe("x" * 500, 40)), 40)


class StubEngine:
    row_cap = 200
    timeout_s = 30

    def run(self, exercise, sql):
        return RunResult(status="ok", columns=["a"], rows=[[1]],
                         row_count=1, truncated=False, duration_ms=5)


BOOTSTRAP = {
    "exercises": [{"name": "ex1", "title": "Ex 1", "difficulty": "junior",
                   "statement_md": "# hi", "schema_sql": "CREATE TABLE t(i INT);",
                   "tables": []}],
    "row_cap": 200,
    "timeout_s": 30,
}


class TestServer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.session = Session(["ex1"], ttl_min=60, session_dir=self.tmp.name)
        self.httpd = build_server(self.session, StubEngine(), BOOTSTRAP,
                                  "<html>PAGE</html>", port=0, quiet=True)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.tmp.cleanup()

    def _request(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {}
        payload = None
        if body is not None:
            payload = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        try:
            parsed = json.loads(data) if data else None
        except json.JSONDecodeError:
            parsed = data.decode("utf-8", "replace")
        return resp.status, parsed

    def _t(self, path):
        sep = "&" if "?" in path else "?"
        return f"{path}{sep}t={self.session.token}"

    # -- auth ---------------------------------------------------------------
    def test_no_token_404(self):
        for path in ["/", "/c", "/api/bootstrap", "/favicon.ico"]:
            status, _ = self._request("GET", path)
            self.assertEqual(status, 404, path)

    def test_bad_token_404(self):
        status, _ = self._request("GET", "/c?t=wrongtoken")
        self.assertEqual(status, 404)

    def test_non_ascii_token_404_not_crash(self):
        # percent-encoded UTF-8 decodes to non-ASCII: must be a clean 404,
        # not a TypeError inside hmac.compare_digest
        for path in ["/c?t=%C3%B1abc", "/c?t=abc%E2%80%8B"]:
            status, _ = self._request("GET", path)
            self.assertEqual(status, 404, path)

    def test_unknown_path_with_valid_token_404(self):
        status, _ = self._request("GET", self._t("/api/secret"))
        self.assertEqual(status, 404)

    # -- happy paths ----------------------------------------------------------
    def test_candidate_page(self):
        status, body = self._request("GET", self._t("/c"))
        self.assertEqual(status, 200)
        self.assertIn("PAGE", body)

    def test_bootstrap(self):
        status, body = self._request("GET", self._t("/api/bootstrap"))
        self.assertEqual(status, 200)
        self.assertEqual(body["exercises"][0]["name"], "ex1")
        self.assertIn("saved_editor_texts", body)
        self.assertIn("expires_at", body)

    def test_editor_roundtrip(self):
        status, _ = self._request("POST", self._t("/api/editor"),
                                  {"exercise": "ex1", "text": "SELECT 1"})
        self.assertEqual(status, 204)
        _, body = self._request("GET", self._t("/api/bootstrap"))
        self.assertEqual(body["saved_editor_texts"]["ex1"], "SELECT 1")

    def test_run_ok(self):
        self.session.last_run_ts = 0
        status, body = self._request("POST", self._t("/api/run"),
                                     {"exercise": "ex1", "sql": "SELECT 1"})
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["rows"], [[1]])
        # run persisted the SQL as editor text too
        self.assertEqual(self.session.editor_snapshot()["ex1"], "SELECT 1")

    # -- guardrails at the HTTP layer -----------------------------------------
    def test_rapid_second_run_429(self):
        self.session.last_run_ts = 0
        status, _ = self._request("POST", self._t("/api/run"),
                                  {"exercise": "ex1", "sql": "SELECT 1"})
        self.assertEqual(status, 200)
        status, body = self._request("POST", self._t("/api/run"),
                                     {"exercise": "ex1", "sql": "SELECT 1"})
        self.assertEqual(status, 429)

    def test_rejected_sql_400_and_logged(self):
        self.session.last_run_ts = 0
        status, body = self._request("POST", self._t("/api/run"),
                                     {"exercise": "ex1", "sql": "DROP TABLE t"})
        self.assertEqual(status, 400)
        self.assertEqual(body["status"], "rejected")
        self.assertEqual(self.session.runs[-1]["status"], "rejected")
        self.assertEqual(self.session.runs[-1]["sql"], "DROP TABLE t")

    def test_rejected_run_does_not_arm_rate_limit(self):
        self.session.last_run_ts = 0
        status, _ = self._request("POST", self._t("/api/run"),
                                  {"exercise": "ex1", "sql": "DROP TABLE t"})
        self.assertEqual(status, 400)
        # an immediate valid run must NOT get a 429
        status, body = self._request("POST", self._t("/api/run"),
                                     {"exercise": "ex1", "sql": "SELECT 1"})
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    def test_early_error_post_closes_connection(self):
        # A POST whose body is never read must not poison a keep-alive
        # connection: the server closes it explicitly.
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        payload = json.dumps({"exercise": "ex1", "text": "SELECT 1"}).encode()
        conn.request("POST", "/api/editor?t=wrongtoken", body=payload,
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 404)
        self.assertEqual(resp.getheader("Connection"), "close")
        resp.read()
        conn.close()

    def test_unknown_exercise_400(self):
        self.session.last_run_ts = 0
        status, _ = self._request("POST", self._t("/api/run"),
                                  {"exercise": "nope", "sql": "SELECT 1"})
        self.assertEqual(status, 400)

    def test_oversized_body_413(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", self._t("/api/editor"), body=b"",
                     headers={"Content-Length": str(MAX_BODY_BYTES + 1)})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 413)
        conn.close()

    def test_editor_text_too_long_400(self):
        status, _ = self._request("POST", self._t("/api/editor"),
                                  {"exercise": "ex1", "text": "x" * 30_000})
        self.assertEqual(status, 400)

    def test_invalid_json_400(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", self._t("/api/run"), body=b"not json{{",
                     headers={"Content-Type": "application/json"})
        self.assertEqual(conn.getresponse().status, 400)
        conn.close()

    # -- expiry ---------------------------------------------------------------
    def test_expired_session_410(self):
        self.session.expires_at = time.time() - 1
        status, _ = self._request("GET", self._t("/api/bootstrap"))
        self.assertEqual(status, 410)
        status, _ = self._request("POST", self._t("/api/run"),
                                  {"exercise": "ex1", "sql": "SELECT 1"})
        self.assertEqual(status, 410)
        status, body = self._request("GET", self._t("/c"))
        self.assertEqual(status, 410)

    def test_end_writes_log(self):
        self.session.end("test")
        log_path = self.session.log_path
        with open(log_path, encoding="utf-8") as f:
            lines = [json.loads(l) for l in f if l.strip()]
        self.assertEqual(lines[-1]["type"], "session_end")
        self.assertEqual(lines[-1]["reason"], "test")


if __name__ == "__main__":
    unittest.main()
