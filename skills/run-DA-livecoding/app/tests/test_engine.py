import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import duckdb  # noqa: F401
    HAS_DUCKDB = True
except ImportError:
    HAS_DUCKDB = False

from engine import DuckDBEngine
from exercises import load_exercises


@unittest.skipUnless(HAS_DUCKDB, "duckdb not installed")
class TestDuckDBEngine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.exercises = load_exercises(["exercise_01", "exercise_05"])
        cls.engine = DuckDBEngine(cls.exercises, cls.tmp.name)
        cls.engine.prepare()

    @classmethod
    def tearDownClass(cls):
        cls.engine.close()
        cls.tmp.cleanup()

    def test_basic_count(self):
        r = self.engine.run("exercise_01", "SELECT COUNT(*) AS n FROM orders")
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.columns, ["n"])
        self.assertEqual(r.rows[0][0], 52)

    def test_solutions_run_and_return_rows(self):
        for ex in self.exercises:
            path = os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "exercises", ex.name, "solution.sql")
            with open(path, encoding="utf-8") as f:
                sql = f.read()
            r = self.engine.run(ex.name, sql)
            self.assertEqual(r.status, "ok", f"{ex.name}: {r.error}")
            self.assertGreater(r.row_count, 0, ex.name)

    def test_write_blocked_by_read_only(self):
        r = self.engine.run("exercise_01", "INSERT INTO orders VALUES (999, 1, '2026-01-01', 'completed', 10)")
        self.assertEqual(r.status, "error")
        # engine still healthy afterwards
        self.assertEqual(self.engine.run("exercise_01", "SELECT 1").status, "ok")

    def test_local_file_read_blocked(self):
        r = self.engine.run("exercise_01", "SELECT * FROM read_csv('/etc/passwd')")
        self.assertEqual(r.status, "error")
        self.assertIn("Permission", r.error)

    def test_set_blocked_by_locked_configuration(self):
        r = self.engine.run("exercise_01", "SET memory_limit='10GB'")
        self.assertEqual(r.status, "error")

    def test_explain_analyze_write_blocked(self):
        # Parses as EXPLAIN (passes validation) but the write inside cannot run.
        r = self.engine.run("exercise_01",
                            "EXPLAIN ANALYZE INSERT INTO orders VALUES (999, 1, '2026-01-01', 'x', 1)")
        self.assertEqual(r.status, "error")

    def test_row_cap_truncation(self):
        r = self.engine.run("exercise_01",
                            "SELECT o1.id FROM orders o1, orders o2")  # 52*52 rows
        self.assertEqual(r.status, "ok")
        self.assertTrue(r.truncated)
        self.assertEqual(r.row_count, self.engine.row_cap)

    def test_sql_error_keeps_engine_alive(self):
        r = self.engine.run("exercise_01", "SELECT definitely_not_a_column FROM orders")
        self.assertEqual(r.status, "error")
        self.assertTrue(r.error)
        self.assertEqual(self.engine.run("exercise_01", "SELECT 1").status, "ok")

    def test_unknown_exercise(self):
        r = self.engine.run("nope", "SELECT 1")
        self.assertEqual(r.status, "error")

    def test_giant_cell_is_capped(self):
        r = self.engine.run("exercise_01", "SELECT repeat('x', 1000000) AS big")
        self.assertEqual(r.status, "ok")
        self.assertTrue(r.truncated)
        self.assertLess(len(r.rows[0][0]), 2100)

    def test_db_paths_do_not_leak_home_dir(self):
        # PRAGMA database_list is a valid read statement; the paths it
        # reports must not expose the interviewer's home directory.
        r = self.engine.run("exercise_01", "PRAGMA database_list")
        self.assertEqual(r.status, "ok")
        home = os.path.expanduser("~")
        self.assertNotIn(home, str(r.rows))
        # error messages are sanitized too
        r2 = self.engine.run("exercise_01", "SELECT * FROM no_such_table")
        self.assertEqual(r2.status, "error")
        self.assertNotIn(home, r2.error)

    def test_json_safe_types(self):
        r = self.engine.run("exercise_05",
                            "SELECT order_date, order_usd_amount FROM orders LIMIT 1")
        self.assertEqual(r.status, "ok")
        for v in r.rows[0]:
            self.assertIsInstance(v, (str, int, float, bool, type(None)))

    def test_timeout_interrupts_long_query(self):
        eng = DuckDBEngine(load_exercises(["exercise_01"]),
                           os.path.join(self.tmp.name, "t2"), timeout_s=1)
        eng.prepare()
        try:
            r = eng.run("exercise_01",
                        "WITH RECURSIVE t(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM t WHERE i < 200000000) "
                        "SELECT max(i) FROM t")
            self.assertEqual(r.status, "timeout")
            # connection usable after an interrupt
            self.assertEqual(eng.run("exercise_01", "SELECT 1").status, "ok")
        finally:
            eng.close()


if __name__ == "__main__":
    unittest.main()
