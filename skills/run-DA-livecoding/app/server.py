"""HTTP server: token-gated routes for the candidate page and the run API.

Every route requires ?t=<session token> — cloudflared forwards remote traffic
to localhost, so source IP means nothing and the token is the only gate.
Anything else (including / and /favicon.ico) gets a bland 404.
"""
import json
import shutil
import sys
import time
import unicodedata
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import checker
import guardrails
import sqlstyle

MAX_BODY_BYTES = 256 * 1024
EDITOR_MAX_CHARS = 20_000
MIN_SECONDS_BETWEEN_RUNS = 2.0
SQL_SNIPPET_CHARS = 80


def _char_width(c):
    """Columns one character occupies; CJK and emoji take two."""
    return 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1


def _display_width(text):
    return sum(_char_width(c) for c in text)


def _clip(text, limit):
    """Cut `text` to at most `limit` terminal columns, not characters.

    Counting characters would let a line of CJK text run twice as wide as the
    window and wrap, which breaks the alignment of every row below it.
    """
    if limit <= 0:
        return ""
    out, used = [], 0
    for c in text:
        width = _char_width(c)
        if used + width > limit:
            break
        out.append(c)
        used += width
    return "".join(out)


def _terminal_safe(sql, limit):
    """One-line, control-character-free excerpt of candidate SQL.

    The live feed is the interviewer's record of what was run, so the
    candidate must not be able to smuggle ANSI escapes, backspaces or bells
    into it and repaint or erase the terminal.
    """
    flat = " ".join(sql.split())
    return _clip("".join(c if c.isprintable() else " " for c in flat), limit)


def _safe(label, fn, *args):
    """Run an interviewer-only extra; never let it break a candidate's run."""
    try:
        return fn(*args)
    except Exception as e:
        print(f"[warn] {label} failed: {type(e).__name__}: {e}",
              file=sys.stderr, flush=True)
        return None


MIN_SNIPPET_CHARS = 20  # below this, dropping ms buys more query text
                        # than it costs (7 columns back for the snippet)


class Layout:
    """Column widths of the live feed.

    The header, every run row and the reason line are built here, so they
    cannot drift apart. Geometry is recomputed for each line, so resizing the
    window mid-interview cannot leave the rows misaligned with the header. A
    window too narrow for every column drops `ms` first and then trims the
    query, and no line is ever wider than the window: a wrapped row would
    break the alignment of everything below it.
    """

    def __init__(self, exercise_names=(), width_fn=None):
        self.width_fn = width_fn or (
            lambda: shutil.get_terminal_size((100, 24)).columns)
        self.ex_w = min(20, max(13, max((len(n) for n in exercise_names),
                                        default=0) + 2))

    # -- geometry ---------------------------------------------------------
    def _geometry(self):
        """(terminal width, ms column width, width of everything before the query)"""
        width = self.width_fn()
        full = self._prefix_for(7)
        ms_w = 0 if width - full < MIN_SNIPPET_CHARS else 7
        return width, ms_w, self._prefix_for(ms_w)

    def _prefix_for(self, ms_w):
        return 2 + 10 + self.ex_w + 10 + 5 + ms_w + 2 + 11 + 7 + 1

    @property
    def compact(self):
        return self._geometry()[1] == 0

    @property
    def prefix(self):
        return self._geometry()[2]

    @property
    def detail_indent(self):
        width, ms_w, prefix = self._geometry()
        indent = prefix - 11 - 7 - 1  # under the 'result' column
        return indent if indent + 8 <= width else 2

    # -- lines ------------------------------------------------------------
    def header(self):
        return self._line("time", "exercise", "status", "rows", "ms",
                          "result", "style", "query")

    def rule(self, char="\u2500"):
        width, _, prefix = self._geometry()
        return char * max(20, min(width, max(72, prefix + 5)))

    def row(self, stamp, exercise, status, rows, ms, result, style, sql):
        width, _, prefix = self._geometry()
        room = min(SQL_SNIPPET_CHARS, max(0, width - prefix))
        return self._line(stamp, self._fit(exercise), status, rows, ms,
                          result, style, _terminal_safe(sql, room))

    def detail(self, text):
        indent = self.detail_indent
        room = max(0, self.width_fn() - indent - 2)
        return " " * indent + "\u21b3 " + _terminal_safe(text, room)

    def _fit(self, name):
        return name if len(name) < self.ex_w else name[:self.ex_w - 2] + "\u2026"

    def _line(self, stamp, exercise, status, rows, ms, result, style, tail):
        width, ms_w, _ = self._geometry()
        ms_cell = f"{ms:>{ms_w}}" if ms_w else ""
        line = (f"  {stamp:<10}{exercise:<{self.ex_w}}{status:<10}{rows:>5}"
                f"{ms_cell}  {result:<11}{style:<7} {tail}")
        return _clip(line, width)


ENDED_HTML = (
    "<!doctype html><meta charset='utf-8'>"
    "<body style='font-family:sans-serif;display:grid;place-items:center;height:90vh'>"
    "<h2>Session ended</h2></body>"
)


def build_server(session, engine, bootstrap, candidate_html, port=0, quiet=False,
                 expected=None, layout=None):
    handler = type("Handler", (_Handler,), {
        "session": session,
        "engine": engine,
        "bootstrap": bootstrap,
        "candidate_html": candidate_html,
        "quiet": quiet,
        # Interviewer-only: reference results per exercise and the feed layout.
        "expected": expected or {},
        "layout": layout or Layout(session.exercise_names),
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
    expected = {}
    layout = None

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
                self._record(exercise, "rejected", sql, error=reason,
                             style=_safe("style check", sqlstyle.analyze, sql))
                return self._json(400, {"status": "rejected", "error": reason})

            # Stamp only for queries that reach the engine, so an instant
            # rejection doesn't force the candidate to wait out the window.
            self.session.last_run_ts = now
            result = self.engine.run(exercise, sql)
            # Both are interviewer-only and go to the terminal and the log;
            # the response below stays exactly the candidate-visible fields.
            style = _safe("style check", sqlstyle.analyze, sql,
                          result.columns if result.status == "ok" else None)
            check = _safe("result check", checker.compare,
                          self.expected.get(exercise), result, sql)
            self._record(exercise, result.status, sql,
                         row_count=result.row_count, truncated=result.truncated,
                         duration_ms=result.duration_ms, error=result.error,
                         check=check, style=style)
            return self._json(200, result.to_dict())
        finally:
            self.session.run_lock.release()

    def _record(self, exercise, status, sql, row_count=None, truncated=None,
                duration_ms=None, error=None, check=None, style=None):
        self.session.record_run({
            "ts": time.time(), "exercise": exercise, "status": status,
            "sql": sql, "row_count": row_count, "truncated": truncated,
            "duration_ms": duration_ms, "error": error,
            "check": check, "style": style,
        })
        if not self.quiet:
            self._print_row(exercise, status, sql, row_count, duration_ms,
                            check, style)

    def _print_row(self, exercise, status, sql, row_count, duration_ms,
                   check, style):
        """Print one line of the live feed.

        Guarded on purpose: the run is already executed and logged by now, so
        a formatting bug must not cost the candidate their response.
        """
        try:
            print(self.layout.row(
                time.strftime("%H:%M:%S"), exercise, status,
                "" if row_count is None else str(row_count),
                "" if duration_ms is None else str(duration_ms),
                (check or {}).get("label") or "",
                "" if style is None else (style["flags"] or "ok"),
                sql), flush=True)
            if check and check["verdict"] in ("FAIL", "NEAR", "?") and check.get("reason"):
                print(self.layout.detail(check["reason"]), flush=True)
        except Exception as e:
            print(f"[warn] console row failed: {type(e).__name__}: {e}",
                  file=sys.stderr, flush=True)

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
