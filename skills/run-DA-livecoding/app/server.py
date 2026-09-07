"""HTTP server: token-gated routes for the candidate page and the run API.

Every route requires ?t=<session token> — cloudflared forwards remote traffic
to localhost, so source IP means nothing and the token is the only gate.
Anything else (including / and /favicon.ico) gets a bland 404.
"""
import json
import shutil
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import guardrails

MAX_BODY_BYTES = 256 * 1024
EDITOR_MAX_CHARS = 20_000
MIN_SECONDS_BETWEEN_RUNS = 2.0
SQL_SNIPPET_CHARS = 80
ROW_PREFIX_CHARS = 57  # width of the columns before the query text


def _terminal_safe(sql, limit):
    """One-line, control-character-free excerpt of candidate SQL.

    The live feed is the interviewer's record of what was run, so the
    candidate must not be able to smuggle ANSI escapes, backspaces or bells
    into it and repaint or erase the terminal.
    """
    flat = " ".join(sql.split())
    return "".join(c if c.isprintable() else " " for c in flat)[:limit]

ENDED_HTML = (
    "<!doctype html><meta charset='utf-8'>"
    "<body style='font-family:sans-serif;display:grid;place-items:center;height:90vh'>"
    "<h2>Session ended</h2></body>"
)


def build_server(session, engine, bootstrap, candidate_html, port=0, quiet=False):
    handler = type("Handler", (_Handler,), {
        "session": session,
        "engine": engine,
        "bootstrap": bootstrap,
        "candidate_html": candidate_html,
        "quiet": quiet,
    })
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    httpd.daemon_threads = True
    return httpd


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = ""
    sys_version = ""

    session = None
    engine = None
    bootstrap = None
    candidate_html = None
    quiet = False

    # -- routing ----------------------------------------------------------
    def do_GET(self):
        parts = urlsplit(self.path)
        if not self._authorized(parts):
            return self._not_found()
        if not self.session.active():
            if parts.path == "/c":
                return self._send(410, "text/html; charset=utf-8", ENDED_HTML)
            return self._json(410, {"error": "This session has ended."})
        if parts.path == "/c":
            return self._send(200, "text/html; charset=utf-8", self.candidate_html)
        if parts.path == "/api/bootstrap":
            payload = dict(self.bootstrap)
            payload["saved_editor_texts"] = self.session.editor_snapshot()
            payload["expires_at"] = self.session.expires_at
            return self._json(200, payload)
        return self._not_found()

    def do_POST(self):
        parts = urlsplit(self.path)
        if not self._authorized(parts):
            # Responding without draining the body would desync the
            # keep-alive connection: close it instead.
            self.close_connection = True
            return self._not_found()
        if not self.session.active():
            self.close_connection = True
            return self._json(410, {"error": "This session has ended."})
        body = self._read_body()
        if body is None:
            return  # error response already sent
        if parts.path == "/api/editor":
            return self._handle_editor(body)
        if parts.path == "/api/run":
            return self._handle_run(body)
        return self._not_found()

    # -- handlers ---------------------------------------------------------
    def _handle_editor(self, body):
        exercise = body.get("exercise")
        text = body.get("text", "")
        if not isinstance(text, str) or len(text) > EDITOR_MAX_CHARS:
            return self._json(400, {"error": "Text too long."})
        if not self.session.set_editor_text(exercise, text):
            return self._json(400, {"error": "Unknown exercise."})
        return self._json(204, None)

    def _handle_run(self, body):
        exercise = body.get("exercise")
        sql = body.get("sql", "")
        if exercise not in self.session.editor_texts or not isinstance(sql, str):
            return self._json(400, {"error": "Unknown exercise."})
        if not self.session.run_lock.acquire(blocking=False):
            return self._json(429, {"error": "A query is already running."})
        try:
            now = time.time()
            if now - self.session.last_run_ts < MIN_SECONDS_BETWEEN_RUNS:
                return self._json(429, {"error": "Too fast — wait a moment between runs."})
            self.session.set_editor_text(exercise, sql)

            ok, reason = guardrails.validate_sql(sql)
            if not ok:
                self._record(exercise, "rejected", sql, error=reason)
                return self._json(400, {"status": "rejected", "error": reason})

            # Stamp only for queries that reach the engine, so an instant
            # rejection doesn't force the candidate to wait out the window.
            self.session.last_run_ts = now
            result = self.engine.run(exercise, sql)
            self._record(exercise, result.status, sql,
                         row_count=result.row_count, truncated=result.truncated,
                         duration_ms=result.duration_ms, error=result.error)
            return self._json(200, result.to_dict())
        finally:
            self.session.run_lock.release()

    def _record(self, exercise, status, sql, row_count=None, truncated=None,
                duration_ms=None, error=None):
        self.session.record_run({
            "ts": time.time(), "exercise": exercise, "status": status,
            "sql": sql, "row_count": row_count, "truncated": truncated,
            "duration_ms": duration_ms, "error": error,
        })
        if not self.quiet:
            # Column widths must match the header printed by serve.py's banner.
            rows = "" if row_count is None else str(row_count)
            ms = "" if duration_ms is None else str(duration_ms)
            stamp = time.strftime("%H:%M:%S")
            width = shutil.get_terminal_size((100, 24)).columns
            snippet = _terminal_safe(sql, max(24, min(SQL_SNIPPET_CHARS,
                                                      width - ROW_PREFIX_CHARS)))
            print(f"  {stamp:<10}{exercise:<18}{status:<10}{rows:>6}{ms:>8}   {snippet}",
                  flush=True)

    # -- plumbing ---------------------------------------------------------
    def _authorized(self, parts):
        token = (parse_qs(parts.query).get("t") or [None])[0]
        return self.session.check_token(token)

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            self._json(400, {"error": "Missing request body."})
            return None
        if length > MAX_BODY_BYTES:
            self.close_connection = True  # body left unread: don't reuse
            self._json(413, {"error": "Request body too large."})
            return None
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json(400, {"error": "Invalid JSON."})
            return None
        if not isinstance(body, dict):
            self._json(400, {"error": "Invalid JSON."})
            return None
        return body

    def _json(self, code, payload):
        data = b"" if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        if data:
            self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _send(self, code, content_type, text):
        data = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)

    def _not_found(self):
        self._send(404, "text/plain; charset=utf-8", "Not Found")

    def log_message(self, fmt, *args):  # suppress per-request noise;
        pass                            # runs are printed explicitly

    def handle(self):
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError):
            pass  # client went away mid-response
        except Exception as e:  # never dump tracebacks onto the interview terminal
            print(f"[warn] request handler error: {type(e).__name__}: {e}",
                  file=sys.stderr, flush=True)
