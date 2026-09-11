import http.client
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import checker
import server as server_module
from checker import Expected
from engine import RunResult
from server import (MAX_BODY_BYTES, Layout, _display_width, _terminal_safe,
                    build_server)
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
                         row_count=1, truncated=False, duration_ms=5,
                         raw_rows=[(1,)])


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


SENTINEL_NAME = "Zed Quux Sentinel"


def _expected_one():
    return {"ex1": Expected(columns=["a"], rows=[(Decimal("1.00"),)],
                            ordered=False, key_indexes=[], decimals=2,
                            row_cap=200)}


class TestInterviewerOnlyData(unittest.TestCase):
    """Verdicts, style flags and the candidate's name stay out of every response."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.session = Session(["ex1"], ttl_min=60, session_dir=self.tmp.name,
                               candidate=SENTINEL_NAME)
        self.httpd = build_server(self.session, StubEngine(), BOOTSTRAP,
                                  "<html>PAGE</html>", port=0, quiet=True,
                                  expected=_expected_one())
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.tmp.cleanup()

    def _get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", f"{path}?t={self.session.token}")
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", "replace")
        conn.close()
        return body

    def _run(self, sql="SELECT 1"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        payload = json.dumps({"exercise": "ex1", "sql": sql}).encode()
        conn.request("POST", f"/api/run?t={self.session.token}", body=payload,
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8", "replace")
        conn.close()
        return resp.status, raw

    def test_run_response_carries_only_the_candidate_visible_fields(self):
        self.session.last_run_ts = 0
        status, raw = self._run()
        self.assertEqual(status, 200)
        self.assertEqual(set(json.loads(raw)),
                         {"status", "columns", "rows", "row_count", "truncated",
                          "duration_ms", "error"})

    def test_the_verdict_is_recorded_in_the_log_instead(self):
        self.session.last_run_ts = 0
        self._run()
        entry = self.session.runs[-1]
        self.assertEqual(entry["check"]["label"], "PASS")
        self.assertEqual(entry["check"]["verdict"], "PASS")
        self.assertIn("flags", entry["style"])
        self.assertIn("issues", entry["style"])

    def test_a_rejected_query_is_still_styled_but_not_judged(self):
        self.session.last_run_ts = 0
        status, _ = self._run("DROP TABLE t")
        self.assertEqual(status, 400)
        entry = self.session.runs[-1]
        self.assertIsNone(entry["check"])
        self.assertIsNotNone(entry["style"])

    def test_no_response_mentions_the_candidate_or_a_verdict(self):
        self.session.last_run_ts = 0
        bodies = [self._get("/c"), self._get("/api/bootstrap"), self._run()[1]]
        for body in bodies:
            self.assertNotIn(SENTINEL_NAME, body)
            for leak in ("solution", "check", "focus", "candidate", "verdict",
                         "PASS", "style_flags", "raw_rows"):
                self.assertNotIn(leak, body, leak)


class TestExtrasCannotBreakARun(unittest.TestCase):
    """A bug in the interviewer-only extras must not cost the candidate a reply."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.session = Session(["ex1"], ttl_min=60, session_dir=self.tmp.name)
        self.httpd = build_server(self.session, StubEngine(), BOOTSTRAP,
                                  "<html>PAGE</html>", port=0, quiet=False,
                                  expected=_expected_one())
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.session.last_run_ts = 0

    def tearDown(self):
        self.httpd.shutdown()
        self.tmp.cleanup()

    def _run(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        payload = json.dumps({"exercise": "ex1", "sql": "SELECT 1"}).encode()
        conn.request("POST", f"/api/run?t={self.session.token}", body=payload,
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        resp.read()
        conn.close()
        return resp.status

    def test_a_crashing_result_check_is_reported_and_survived(self):
        def boom(*args, **kwargs):
            raise RuntimeError("checker exploded")

        original = checker.compare
        checker.compare = boom
        stderr = io.StringIO()
        try:
            with redirect_stderr(stderr):
                self.assertEqual(self._run(), 200)
        finally:
            checker.compare = original
        self.assertIsNone(self.session.runs[-1]["check"])
        self.assertIn("result check failed", stderr.getvalue())

    def test_a_crashing_feed_row_still_logs_the_run(self):
        def boom(*args, **kwargs):
            raise RuntimeError("layout exploded")

        original = Layout.row
        Layout.row = boom
        stderr = io.StringIO()
        try:
            with redirect_stderr(stderr):
                self.assertEqual(self._run(), 200)
        finally:
            Layout.row = original
        self.assertEqual(self.session.runs[-1]["status"], "ok")
        self.assertIn("console row failed", stderr.getvalue())


class TestChecksBannerLine(unittest.TestCase):
    """The banner must not claim a check is running when it is not."""

    def setUp(self):
        import serve
        self.line = serve._checks_line
        self.exercises = [type("E", (), {"name": f"exercise_0{i}"}) for i in range(1, 6)]

    def test_all_checks_on(self):
        self.assertEqual(self.line(self.exercises, {}, {}),
                         "result column on for 5/5 exercises")

    def test_an_exercise_with_no_reference_is_named(self):
        got = self.line(self.exercises, {"exercise_03": "no solution.sql"}, {})
        self.assertIn("on for 4/5", got)
        self.assertIn("off: exercise_03 (no solution.sql)", got)

    def test_a_degraded_check_is_named_too(self):
        got = self.line(self.exercises, {}, {"exercise_02": "row order not checked"})
        self.assertIn("on for 5/5", got)
        self.assertIn("degraded: exercise_02 (row order not checked)", got)


class TestLayout(unittest.TestCase):
    """The header, the run rows and the reason line must stay aligned."""

    NAMES = ["exercise_01", "exercise_05"]
    WIDEST = ("14:12:43", "exercise_01", "cancelled", "200", "30000",
              "NEAR:order", "TILS*A")

    def _layout(self, width):
        return Layout(self.NAMES, width_fn=lambda: width)

    def test_the_header_and_a_row_share_their_column_starts(self):
        layout = self._layout(120)
        header, row = layout.header(), layout.row(*self.WIDEST, "select 1")
        for title, cell in (("exercise", "exercise_01"), ("result", "NEAR:order")):
            self.assertEqual(header.index(title), row.index(cell), title)
        self.assertEqual(len(header) - len("query"), layout.prefix)
        self.assertTrue(row.endswith("select 1"))

    def test_nothing_ever_exceeds_the_window(self):
        # A wrapped row would break the alignment of every row below it.
        long_name = "exercise_07_window_functions_and_ranking"
        for names in (self.NAMES, self.NAMES + [long_name]):
            for width in (60, 72, 80, 88, 92, 100, 120, 200):
                layout = Layout(names, width_fn=lambda w=width: w)
                lines = [layout.header(), layout.rule(), layout.rule("="),
                         layout.row(*self.WIDEST, "select " + "a" * 300),
                         layout.detail("19 rows, expected 4 (extra rows)")]
                for line in lines:
                    self.assertLessEqual(_display_width(line), width,
                                         f"{width} cols, {names[-1]}: {line!r}")
                    self.assertNotIn("\n", line)

    def test_a_narrow_window_drops_the_ms_column_rather_than_wrapping(self):
        self.assertTrue(self._layout(80).compact)
        self.assertFalse(self._layout(100).compact)
        self.assertNotIn("ms", self._layout(80).header())

    def test_geometry_follows_a_resized_window(self):
        # Header and rows must not disagree after the terminal is resized.
        width = [120]
        layout = Layout(self.NAMES, width_fn=lambda: width[0])
        wide_prefix = layout.prefix
        width[0] = 70
        self.assertNotEqual(layout.prefix, wide_prefix)
        self.assertLessEqual(_display_width(layout.row(*self.WIDEST, "select 1")), 70)

    def test_wide_characters_are_budgeted_by_display_width(self):
        # CJK and emoji take two columns each; counting characters would wrap.
        layout = self._layout(100)
        row = layout.row("14:12:43", "exercise_01", "ok", "200", "12", "PASS",
                         "ok", "select '" + "日本語" * 40 + "' as x")
        self.assertLessEqual(_display_width(row), 100)
        self.assertLess(len(row), 100)  # fewer characters than columns used

    def test_the_reason_line_sits_under_the_result_column_and_fits(self):
        for width in (80, 100, 120):
            layout = self._layout(width)
            detail = layout.detail("19 rows, expected 4")
            self.assertLessEqual(len(detail), width, width)
            self.assertEqual(layout.header().index("result"),
                             detail.index("\u21b3"))

    def test_a_long_exercise_name_is_shortened_not_wrapped(self):
        layout = Layout(["exercise_01"], width_fn=lambda: 100)
        row = layout.row("14:12:43", "exercise_with_a_very_long_name", "ok",
                         "1", "5", "PASS", "ok", "select 1")
        self.assertIn("\u2026", row)
        self.assertLessEqual(len(row), 100)

    def test_missing_verdict_and_style_cells_are_blank(self):
        row = self._layout(100).row("14:12:43", "exercise_01", "error", "0", "7",
                                    "", "", "select 1")
        self.assertLessEqual(len(row), 100)
        self.assertIn("select 1", row)

    def test_the_rule_is_never_shorter_than_the_header(self):
        for width in (80, 100):
            layout = self._layout(width)
            self.assertGreaterEqual(len(layout.rule()), len(layout.header()))


if __name__ == "__main__":
    unittest.main()
