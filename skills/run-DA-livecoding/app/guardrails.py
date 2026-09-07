"""SQL validation for candidate input.

This is a UX/telemetry layer: it gives friendly rejections for anything that
is not a single read statement. The real enforcement is the hardened
read-only DuckDB connection in engine.py (read_only=True,
enable_external_access=False, lock_configuration=true) — anything that slips
through here still cannot write, read local files, or change configuration.
"""

MAX_SQL_CHARS = 10_000

# duckdb.extract_statements maps DESCRIBE / SHOW / SUMMARIZE and read-only
# PRAGMAs to SELECT, so {SELECT, EXPLAIN} covers every read shape. Write
# PRAGMAs parse as SET and are rejected.
_ALLOWED_TYPES = {"SELECT", "EXPLAIN"}


def validate_sql(sql):
    """Return (ok, reason). reason is None when ok, user-facing English otherwise."""
    if sql is None or not sql.strip():
        return False, "The query is empty."
    if len(sql) > MAX_SQL_CHARS:
        return False, f"The query exceeds the {MAX_SQL_CHARS}-character limit."

    import duckdb  # lazy: engine.prepare() already guaranteed it's installed

    try:
        statements = duckdb.extract_statements(sql)
    except duckdb.Error as e:
        # Keep the parser's multi-line message (includes the LINE/caret
        # position) so the candidate sees the same detail a SQL console gives.
        return False, _first_lines(e)
    if not statements:
        return False, "The query is empty."
    if len(statements) > 1:
        return False, "One statement at a time (remove the intermediate ';')."

    statement_type = statements[0].type.name
    if statement_type not in _ALLOWED_TYPES:
        return False, (
            f"Only read-only queries are allowed "
            f"({statement_type} statements are not permitted)."
        )
    return True, None


def _first_lines(error, max_lines=5):
    text = str(error).strip()
    if not text:
        return "Invalid SQL: parse error"
    return "\n".join(text.splitlines()[:max_lines])
