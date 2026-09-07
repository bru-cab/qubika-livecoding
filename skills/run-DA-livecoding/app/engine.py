"""Embedded DuckDB engine: seed once per exercise, reopen hardened read-only,
run candidate SQL with an interrupt-based timeout and a row cap.

Security model: the connection is the enforcement layer. Each exercise DB is
seeded with a normal connection (COPY needs file access), closed, and reopened
read_only with external access disabled and configuration locked — so writes,
local file reads (read_csv('/etc/passwd')), ATTACH/COPY TO and SET/PRAGMA all
fail at the engine regardless of what validation lets through.
"""
import math
import os
import shutil
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from datetime import time as dtime
from decimal import Decimal

DEFAULT_TIMEOUT_S = 30
DEFAULT_ROW_CAP = 200
MAX_CELL_CHARS = 2_000        # single result cell cap
MAX_PAYLOAD_CHARS = 2_000_000  # total result budget per run

_HARDENED_CONFIG = {
    "enable_external_access": False,
    "autoinstall_known_extensions": False,
    "autoload_known_extensions": False,
}
_HARDENED_SETTINGS = (
    "SET memory_limit='512MB'",
    "SET threads=2",
    "SET lock_configuration=true",  # must be last: locks all further SET/PRAGMA
)

INSTALL_HINT = (
    "The 'duckdb' package is missing. Install it with:\n"
    "  python3 -m pip install --break-system-packages duckdb"
)


class EngineError(Exception):
    pass


@dataclass
class RunResult:
    status: str  # "ok" | "error" | "timeout"
    columns: list = field(default_factory=list)
    rows: list = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    duration_ms: int = 0
    error: str = None

    def to_dict(self):
        return asdict(self)


class DuckDBEngine:
    def __init__(self, exercises, session_dir,
                 timeout_s=DEFAULT_TIMEOUT_S, row_cap=DEFAULT_ROW_CAP):
        self.exercises = exercises
        self.session_dir = session_dir
        self.timeout_s = timeout_s
        self.row_cap = row_cap
        self._conns = {}
        self._db_dir = None
        self._shutting_down = False

    def prepare(self):
        try:
            import duckdb
        except ImportError:
            raise EngineError(INSTALL_HINT)
        # DB files live in a neutral temp dir, NOT under the user's home:
        # DuckDB reports database paths (PRAGMA database_list, error messages)
        # and those are visible to the candidate.
        self._db_dir = tempfile.mkdtemp(prefix="qubika_sql_interview_")
        for ex in self.exercises:
            db_path = os.path.join(self._db_dir, f"{ex.name}.duckdb")
            conn = duckdb.connect(db_path)
            try:
                for stmt in ex.schema_sql.split(";"):
                    if stmt.strip():
                        conn.execute(stmt)
                for t in ex.tables:
                    conn.execute(
                        f"COPY {t['name']} FROM '{t['csv_path']}' (FORMAT CSV, HEADER)"
                    )
            except duckdb.Error as e:
                raise EngineError(f"error seeding exercise '{ex.name}': {e}")
            finally:
                conn.close()
            ro = duckdb.connect(db_path, read_only=True, config=dict(_HARDENED_CONFIG))
            for setting in _HARDENED_SETTINGS:
                ro.execute(setting)
            self._conns[ex.name] = ro

    def run(self, exercise_name, sql, _retry=True):
        """Execute sql on the exercise's hardened connection.

        Callers must serialize calls — the server holds a global run lock, so
        each connection only ever executes one query at a time.
        """
        import duckdb
        conn = self._conns.get(exercise_name)
        if conn is None:
            return RunResult(status="error",
                             error=f"Unknown exercise: '{exercise_name}'.")
        start = time.perf_counter()
        timer = threading.Timer(self.timeout_s, conn.interrupt)
        try:
            timer.start()
            cur = conn.execute(sql)
            raw = cur.fetchmany(self.row_cap + 1)
            columns = [d[0] for d in cur.description] if cur.description else []
        except duckdb.InterruptException:
            elapsed = time.perf_counter() - start
            if self._shutting_down:
                # interrupt_all() cancelled us: never retry, or shutdown would
                # wait out a whole fresh timeout with the terminal frozen.
                return RunResult(
                    status="cancelled", duration_ms=_ms(start),
                    error="The session was ended while this query was running.")
            if _retry and elapsed < self.timeout_s * 0.5:
                # A stale interrupt from a previous run's timer firing after its
                # query finished but before cancel(). Retry once on a fresh timer.
                timer.cancel()
                return self.run(exercise_name, sql, _retry=False)
            return RunResult(
                status="timeout", duration_ms=_ms(start),
                error=f"The query exceeded the {self.timeout_s}-second limit and was cancelled.")
        except duckdb.Error as e:
            return RunResult(status="error", duration_ms=_ms(start),
                             error=_short_error(e))
        except Exception as e:
            return RunResult(status="error", duration_ms=_ms(start),
                             error=f"{type(e).__name__}: {e}")
        finally:
            timer.cancel()
        truncated = len(raw) > self.row_cap
        rows = []
        budget = MAX_PAYLOAD_CHARS
        for row in raw[:self.row_cap]:
            safe = []
            for v in row:
                s = _json_safe(v)
                if isinstance(s, str) and len(s) > MAX_CELL_CHARS:
                    s = s[:MAX_CELL_CHARS] + "… [truncated]"
                    truncated = True
                safe.append(s)
                budget -= len(s) if isinstance(s, str) else 8
            rows.append(safe)
            if budget <= 0:
                truncated = True
                break
        return RunResult(status="ok", columns=columns, rows=rows,
                         row_count=len(rows), truncated=truncated,
                         duration_ms=_ms(start))

    def interrupt_all(self):
        """Cancel any in-flight query (used on shutdown)."""
        self._shutting_down = True  # an interrupted run must NOT be retried
        for conn in self._conns.values():
            try:
                conn.interrupt()
            except Exception:
                pass

    def close(self):
        for conn in self._conns.values():
            try:
                conn.close()
            except Exception:
                pass
        self._conns.clear()
        if self._db_dir:
            shutil.rmtree(self._db_dir, ignore_errors=True)
            self._db_dir = None


def _ms(start):
    return int((time.perf_counter() - start) * 1000)


def _short_error(error, max_lines=4):
    lines = str(error).strip().splitlines()
    text = "\n".join(lines[:max_lines])
    # Never leak the interviewer's home directory in candidate-visible errors.
    return text.replace(os.path.expanduser("~"), "~")


def _json_safe(v):
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        return v if math.isfinite(v) else str(v)
    if isinstance(v, (Decimal, date, datetime, dtime)):
        return str(v)
    if isinstance(v, (bytes, bytearray)):
        return v.hex()
    return str(v)
