import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from guardrails import MAX_SQL_CHARS, validate_sql

ACCEPTED = [
    "SELECT 1",
    "  select *\nfrom orders  ",
    "SELECT 1;",  # single trailing semicolon is fine
    "WITH t AS (SELECT 1) SELECT * FROM t",
    "(SELECT 1) UNION ALL (SELECT 2)",
    "EXPLAIN SELECT 1",
    "DESCRIBE orders",
    "SHOW TABLES",
    "SUMMARIZE SELECT 1",
    "SELECT 'please DROP me' AS note",  # keyword inside a string literal
    "SELECT 1 -- DROP TABLE x",
    "-- comment first\nSELECT 1",
    "/* block comment */ SELECT 1",
    "SELECT created_at FROM (SELECT 1 AS created_at)",  # no keyword false-positive
]

REJECTED = [
    "",
    "   \n  ",
    None,
    "INSERT INTO t VALUES (1)",
    "UPDATE t SET x = 1",
    "DELETE FROM t",
    "DROP TABLE t",
    "CREATE TABLE x (i INTEGER)",
    "SELECT 1; SELECT 2",  # multi-statement
    "SELECT 1; DROP TABLE t",
    "/* sneaky */ INSERT INTO t VALUES (1)",
    "SET threads=8",
    "PRAGMA memory_limit='10GB'",
    "ATTACH 'x.db'",
    "COPY t TO 'out.csv'",
    "LOAD httpfs",
    "INSTALL httpfs",
    "CALL pragma_database_list()",
    "BEGIN TRANSACTION",
    "EXPORT DATABASE 'd'",
    "this is not sql at all !!!",
    "x" * (MAX_SQL_CHARS + 1),
]


class TestValidateSql(unittest.TestCase):
    def test_accepted(self):
        for sql in ACCEPTED:
            with self.subTest(sql=sql[:60]):
                ok, reason = validate_sql(sql)
                self.assertTrue(ok, f"expected accept, got: {reason}")

    def test_rejected(self):
        for sql in REJECTED:
            with self.subTest(sql=repr(sql)[:60]):
                ok, reason = validate_sql(sql)
                self.assertFalse(ok, "expected reject")
                self.assertTrue(reason, "rejection must carry a reason")

    def test_explain_analyze_insert_is_engine_blocked_anyway(self):
        # EXPLAIN ANALYZE INSERT parses as EXPLAIN (allowed here), but the
        # read-only connection blocks the underlying write — documented
        # defense-in-depth, exercised in test_engine.py.
        ok, _ = validate_sql("EXPLAIN ANALYZE SELECT 1")
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
