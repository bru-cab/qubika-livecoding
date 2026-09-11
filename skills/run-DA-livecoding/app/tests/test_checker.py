"""Result comparison: the terminal's result column must be right for the same
reasons an interviewer would be — data, not SQL text."""
import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import checker
from checker import Expected, compare, is_exploratory, normalize_cell
from engine import RunResult

try:
    import duckdb  # noqa: F401
    HAS_DUCKDB = True
except ImportError:
    HAS_DUCKDB = False


def expected_of(columns, rows, ordered=False, key_indexes=(), decimals=2,
                row_cap=200):
    return Expected(columns=list(columns),
                    rows=[tuple(normalize_cell(v, decimals) for v in r) for r in rows],
                    ordered=ordered, key_indexes=list(key_indexes),
                    decimals=decimals, row_cap=row_cap)


def result_of(columns, rows, status="ok", truncated=False):
    return RunResult(status=status, columns=list(columns),
                     rows=[list(r) for r in rows], row_count=len(rows),
                     truncated=truncated, raw_rows=[tuple(r) for r in rows])


class TestNormalizeCell(unittest.TestCase):
    def test_numbers_agree_across_sql_types(self):
        for value in (Decimal("36113.63"), 36113.630000000005, "36113.63"):
            self.assertEqual(normalize_cell(value, 2),
                             normalize_cell(Decimal("36113.6300"), 2), value)

    def test_int_equals_two_decimal_form(self):
        self.assertEqual(normalize_cell(29, 2), normalize_cell(Decimal("29.00"), 2))

    def test_dates_equal_their_iso_string_and_midnight_timestamp(self):
        iso = normalize_cell("2026-03-04", 2)
        self.assertEqual(normalize_cell(date(2026, 3, 4), 2), iso)
        self.assertEqual(normalize_cell(datetime(2026, 3, 4, 0, 0), 2), iso)

    def test_timestamp_with_a_time_keeps_it(self):
        self.assertEqual(normalize_cell(datetime(2026, 3, 4, 9, 30), 2),
                         "2026-03-04 09:30:00")

    def test_rounding_to_fewer_decimals_is_a_real_difference(self):
        self.assertNotEqual(normalize_cell(Decimal("3281.54"), 2),
                            normalize_cell(Decimal("3282"), 2))

    def test_non_finite_numbers_stay_numbers_and_agree_across_types(self):
        # 'expected number, got text' would be a lie for a column of infinities.
        self.assertEqual(normalize_cell(float("inf"), 2),
                         normalize_cell(Decimal("Infinity"), 2))
        self.assertEqual(normalize_cell(float("nan"), 2),
                         normalize_cell(Decimal("NaN"), 2))
        for value in (float("nan"), float("inf"), Decimal("NaN")):
            self.assertEqual(checker._family([normalize_cell(value, 2)]),
                             "number", value)

    def test_huge_numbers_agree_across_types(self):
        # DECIMAL(38, n) and HUGEINT would overflow the default decimal context.
        self.assertEqual(normalize_cell(10 ** 30, 2), normalize_cell(1e30, 2))
        self.assertEqual(normalize_cell(Decimal("1E+30"), 2),
                         normalize_cell(10 ** 30, 2))
        self.assertEqual(checker._family([normalize_cell(Decimal("1" + "0" * 37), 2)]),
                         "number")

    def test_a_boolean_is_not_the_number_one(self):
        self.assertNotEqual(normalize_cell(True, 2), normalize_cell(Decimal("1.00"), 2))
        self.assertNotEqual(normalize_cell(False, 2), normalize_cell(0, 2))
        self.assertEqual(checker._family([normalize_cell(True, 2)]), "number")

    def test_a_timestamp_as_text_matches_the_timestamp_itself(self):
        self.assertEqual(normalize_cell("2026-04-07 00:00:00", 2),
                         normalize_cell(datetime(2026, 4, 7), 2))
        self.assertEqual(normalize_cell("2026-04-07T09:30:00", 2),
                         normalize_cell(datetime(2026, 4, 7, 9, 30), 2))
        self.assertEqual(normalize_cell("not a date", 2), "not a date")

    def test_text_keeps_case_and_empty_string_is_not_null(self):
        self.assertNotEqual(normalize_cell("Brazil", 2), normalize_cell("brazil", 2))
        self.assertNotEqual(normalize_cell("", 2), normalize_cell(None, 2))

    def test_output_is_hashable(self):
        for value in (None, True, 1, 2.5, Decimal("1.5"), "x", date(2026, 1, 1),
                      b"\x00\xff"):
            hash(normalize_cell(value, 2))


class TestIsExploratory(unittest.TestCase):
    def test_read_shapes_that_are_not_answers(self):
        for sql in ("EXPLAIN SELECT 1", "describe orders", "SHOW TABLES",
                    "SUMMARIZE SELECT 1", "PRAGMA database_list",
                    "-- look first\nDESCRIBE orders",
                    "/* hmm */\n  explain select 1"):
            self.assertTrue(is_exploratory(sql), sql)

    def test_answers_are_not_exploratory(self):
        for sql in ("SELECT 1", "  with t as (select 1) select * from t",
                    "FROM orders SELECT count(*)", "", None):
            self.assertFalse(is_exploratory(sql), sql)


class TestCompare(unittest.TestCase):
    def setUp(self):
        self.expected = expected_of(["name", "n"],
                                    [("Sophie", 6), ("Valerie", 5),
                                     ("Alice", 4), ("Thomas", 4)],
                                    ordered=True, key_indexes=[1])

    def test_identical_result_passes(self):
        got = compare(self.expected, result_of(["name", "n"],
                                               [("Sophie", 6), ("Valerie", 5),
                                                ("Alice", 4), ("Thomas", 4)]), "select 1")
        self.assertEqual(got["verdict"], "PASS")
        self.assertEqual(got["label"], "PASS")
        self.assertIsNone(got["reason"])

    def test_other_column_names_still_pass(self):
        got = compare(self.expected, result_of(["customer", "orders"],
                                               [("Sophie", 6), ("Valerie", 5),
                                                ("Alice", 4), ("Thomas", 4)]), "x")
        self.assertEqual(got["verdict"], "PASS")

    def test_tied_rows_may_come_back_in_either_order(self):
        got = compare(self.expected, result_of(["name", "n"],
                                               [("Sophie", 6), ("Valerie", 5),
                                                ("Thomas", 4), ("Alice", 4)]), "x")
        self.assertEqual(got["verdict"], "PASS")

    def test_wrong_order_is_near_not_fail(self):
        got = compare(self.expected, result_of(["name", "n"],
                                               [("Alice", 4), ("Thomas", 4),
                                                ("Valerie", 5), ("Sophie", 6)]), "x")
        self.assertEqual(got["label"], "NEAR:order")
        self.assertEqual(got["reason"], "same rows, different order")

    def test_unordered_exercise_ignores_row_order(self):
        unordered = expected_of(["country", "total"],
                                [("Argentina", Decimal("5759.82")),
                                 ("Brazil", Decimal("4370.21"))])
        got = compare(unordered, result_of(["country", "total"],
                                           [("Brazil", Decimal("4370.21")),
                                            ("Argentina", Decimal("5759.82"))]), "x")
        self.assertEqual(got["verdict"], "PASS")

    def test_ordered_without_keys_compares_whole_rows(self):
        strict = expected_of(["a"], [(1,), (2,)], ordered=True)
        self.assertEqual(compare(strict, result_of(["a"], [(2,), (1,)]), "x")["label"],
                         "NEAR:order")

    def test_swapped_columns_are_near_cols(self):
        got = compare(self.expected, result_of(["n", "name"],
                                               [(6, "Sophie"), (5, "Valerie"),
                                                (4, "Alice"), (4, "Thomas")]), "x")
        self.assertEqual(got["label"], "NEAR:cols")
        self.assertEqual(got["reason"], "columns in a different order")

    def test_column_count_mismatch(self):
        got = compare(self.expected, result_of(["name"], [("Sophie",)]), "x")
        self.assertEqual(got["label"], "FAIL:cols")
        self.assertEqual(got["reason"], "1 column, expected 2")

    def test_too_many_rows_says_extra(self):
        rows = [("Sophie", 6), ("Valerie", 5), ("Alice", 4), ("Thomas", 4),
                ("Ian", 3)]
        got = compare(self.expected, result_of(["name", "n"], rows), "x")
        self.assertEqual(got["label"], "FAIL:rows")
        self.assertEqual(got["reason"], "5 rows, expected 4 (extra rows)")

    def test_too_few_rows_says_missing(self):
        got = compare(self.expected, result_of(["name", "n"],
                                               [("Sophie", 6), ("Valerie", 5)]), "x")
        self.assertEqual(got["reason"], "2 rows, expected 4 (missing rows)")

    def test_duplicated_rows_are_named(self):
        dup = expected_of(["a"], [(1,), (2,)])
        got = compare(dup, result_of(["a"], [(1,), (1,), (2,)]), "x")
        self.assertEqual(got["label"], "FAIL:rows")
        self.assertIn("duplicates", got["reason"])

    def test_wrong_column_type_is_named(self):
        got = compare(self.expected, result_of(["customer_id", "n"],
                                               [(3, 6), (5, 5), (1, 4), (12, 4)]), "x")
        self.assertEqual(got["label"], "FAIL:vals")
        self.assertEqual(got["reason"], "col 1 (name): expected text, got number")

    def test_same_shape_different_numbers_shows_an_example(self):
        expected = expected_of(["country", "total"],
                               [("Argentina", Decimal("5759.82")),
                                ("Brazil", Decimal("4370.21"))])
        got = compare(expected, result_of(["country", "total"],
                                          [("Brazil", Decimal("4370.00")),
                                           ("Argentina", Decimal("5759.82"))]), "x")
        self.assertEqual(got["label"], "FAIL:vals")
        # The example must pair the two Brazil rows, not two unrelated ones.
        self.assertIn("Brazil", got["reason"])
        self.assertNotIn("Argentina", got["reason"])

    def test_null_is_not_an_empty_string(self):
        expected = expected_of(["country"], [(None,)])
        got = compare(expected, result_of(["country"], [("",)]), "x")
        self.assertEqual(got["label"], "FAIL:vals")

    def test_row_overflow_beyond_the_cap_fails(self):
        expected = expected_of(["a"], [(1,), (2,)], row_cap=3)
        got = compare(expected, result_of(["a"], [(1,), (2,), (3,), (4,)],
                                          truncated=True), "x")
        self.assertEqual(got["label"], "FAIL:rows")
        self.assertIn("more than 3 rows", got["reason"])

    def test_a_truncated_giant_cell_does_not_count_as_extra_rows(self):
        expected = expected_of(["big"], [("x" * 10,)])
        got = compare(expected, result_of(["big"], [("x" * 10,)], truncated=True), "x")
        self.assertEqual(got["verdict"], "PASS")

    def test_no_reference_is_not_applicable(self):
        got = compare(None, result_of(["a"], [(1,)]), "x")
        self.assertEqual(got["verdict"], "NA")
        self.assertEqual(got["label"], "n/a")

    def test_failed_or_exploratory_runs_get_no_cell(self):
        self.assertIsNone(compare(self.expected, result_of([], [], status="error"), "x"))
        self.assertIsNone(compare(self.expected, None, "x"))
        self.assertIsNone(compare(self.expected,
                                  result_of(["explain_key", "explain_value"],
                                            [("a", "b")]), "EXPLAIN SELECT 1"))

    def test_explain_is_blank_even_when_style_analysis_is_unavailable(self):
        # compare() decides this from the SQL itself, not from sqlstyle output.
        self.assertIsNone(compare(self.expected,
                                  result_of(["k", "v"], [("a", "b")]),
                                  "  -- peek\n explain select 1"))

    def test_labels_fit_the_console_column(self):
        cases = [
            compare(self.expected, result_of(["name"], [("x",)]), "x"),
            compare(self.expected, result_of(["name", "n"], [("x", 1)]), "x"),
            compare(None, result_of(["a"], [(1,)]), "x"),
        ]
        for got in cases:
            self.assertLessEqual(len(got["label"]), 10, got)

    def test_verdicts_are_json_serializable(self):
        got = compare(self.expected, result_of(["name", "n"], [("x", 1)]), "x")
        json.dumps(got)

    def test_reason_is_bounded(self):
        expected = expected_of(["a"], [("y" * 300,)])
        got = compare(expected, result_of(["a"], [("z" * 300,)]), "x")
        self.assertLessEqual(len(got["reason"]), checker.MAX_REASON_CHARS)

    def test_a_checker_bug_never_raises(self):
        broken = Expected(columns=["a"], rows=None, ordered=False,
                          key_indexes=[], decimals=2, row_cap=200)
        got = compare(broken, result_of(["a"], [(1,)]), "x")
        self.assertEqual(got["verdict"], "?")
        self.assertIn("checker error", got["reason"])


class FakeExercise:
    def __init__(self, name="ex", solution_sql="SELECT 1", check=None):
        self.name = name
        self.solution_sql = solution_sql
        self.check = check or {"enabled": True, "ordered": False,
                               "order_by": [], "decimals": 2}


class FakeEngine:
    row_cap = 200

    def __init__(self, result):
        self.result = result

    def run(self, name, sql):
        return self.result


class TestBuildExpected(unittest.TestCase):
    def test_missing_solution_switches_the_column_off(self):
        expected, off, degraded, warnings = checker.build_expected(
            FakeEngine(result_of(["a"], [(1,)])), [FakeExercise(solution_sql=None)])
        self.assertIsNone(expected["ex"])
        self.assertEqual(off["ex"], "no solution.sql")
        self.assertTrue(warnings)

    def test_disabled_check_is_silent(self):
        expected, off, degraded, warnings = checker.build_expected(
            FakeEngine(result_of(["a"], [(1,)])),
            [FakeExercise(check={"enabled": False, "ordered": False,
                                 "order_by": [], "decimals": 2})])
        self.assertIsNone(expected["ex"])
        self.assertEqual(warnings, [])

    def test_broken_solution_is_reported_not_raised(self):
        broken = RunResult(status="error", error="Binder Error: no such column\nmore")
        expected, off, degraded, warnings = checker.build_expected(
            FakeEngine(broken), [FakeExercise()])
        self.assertIsNone(expected["ex"])
        self.assertIn("Binder Error", off["ex"])
        self.assertNotIn("more", off["ex"])  # only the first line

    def test_solution_over_the_row_cap_is_not_usable(self):
        big = result_of(["a"], [(i,) for i in range(201)])
        expected, off, _, _ = checker.build_expected(FakeEngine(big), [FakeExercise()])
        self.assertIsNone(expected["ex"])
        self.assertIn("more than 200 rows", off["ex"])

    def test_unknown_order_by_column_is_reported_as_degraded(self):
        # The verdict still works, but the interviewer must not believe the
        # row order is being checked when it is not.
        ex = FakeExercise(check={"enabled": True, "ordered": True,
                                 "order_by": ["nope"], "decimals": 2})
        expected, off, degraded, warnings = checker.build_expected(
            FakeEngine(result_of(["a"], [(1,)])), [ex])
        self.assertIsNotNone(expected["ex"])
        self.assertFalse(expected["ex"].ordered)
        self.assertEqual(off, {})
        self.assertEqual(degraded, {"ex": "row order not checked"})
        self.assertTrue(any("nope" in w for w in warnings))

    def test_an_engine_that_explodes_only_disables_the_column(self):
        class Exploding:
            row_cap = 200

            def run(self, name, sql):
                raise RuntimeError("boom")

        expected, off, degraded, warnings = checker.build_expected(
            Exploding(), [FakeExercise()])
        self.assertIsNone(expected["ex"])
        self.assertIn("boom", off["ex"])


@unittest.skipUnless(HAS_DUCKDB, "duckdb not installed")
class TestAgainstTheSeedExercises(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from engine import DuckDBEngine
        from exercises import load_exercises
        cls.tmp = tempfile.TemporaryDirectory()
        cls.exercises = load_exercises()
        cls.engine = DuckDBEngine(cls.exercises, cls.tmp.name)
        cls.engine.prepare()
        cls.expected, cls.off, cls.degraded, cls.warnings = checker.build_expected(
            cls.engine, cls.exercises)

    @classmethod
    def tearDownClass(cls):
        cls.engine.close()
        cls.tmp.cleanup()

    def test_all_five_references_are_usable(self):
        self.assertEqual(self.warnings, [])
        self.assertEqual(self.off, {})
        self.assertEqual(self.degraded, {})
        self.assertEqual(sorted(self.expected), [e.name for e in self.exercises])

    def test_order_keys_resolve_to_the_solution_columns(self):
        self.assertEqual(self.expected["exercise_02"].key_indexes, [2])
        self.assertEqual(self.expected["exercise_04"].key_indexes, [1])
        self.assertEqual(self.expected["exercise_05"].key_indexes, [1])
        self.assertFalse(self.expected["exercise_01"].ordered)

    def _verdict(self, name, sql):
        result = self.engine.run(name, sql)
        self.assertEqual(result.status, "ok", result.error)
        return checker.compare(self.expected[name], result, sql)

    def test_each_solution_passes_against_itself(self):
        for ex in self.exercises:
            self.assertEqual(self._verdict(ex.name, ex.solution_sql)["verdict"],
                             "PASS", ex.name)

    def test_a_cte_answer_with_the_tie_reversed_passes(self):
        sql = ("with per_customer as (select customer_id, count(*) as n "
               "from orders group by customer_id) "
               "select c.name, p.n from per_customer p "
               "join customers c on c.id = p.customer_id "
               "where p.n > 3 order by p.n desc, c.name desc")
        self.assertEqual(self._verdict("exercise_04", sql)["verdict"], "PASS")

    def test_a_cast_to_double_still_passes(self):
        sql = ("select c.name, sum(o.order_usd_amount)::DOUBLE as total "
               "from orders o join customers c on c.id = o.customer_id "
               "where o.status = 'completed' group by c.name "
               "order by total desc limit 5")
        self.assertEqual(self._verdict("exercise_05", sql)["verdict"], "PASS")

    def test_a_date_formatted_as_text_still_passes(self):
        sql = ("select c.name, o.order_date::VARCHAR, o.order_usd_amount "
               "from orders o join customers c on c.id = o.customer_id "
               "where o.order_usd_amount > 500 order by 3 desc")
        self.assertEqual(self._verdict("exercise_02", sql)["verdict"], "PASS")

    def test_the_id_instead_of_the_name_is_caught(self):
        sql = ("select o.customer_id, count(*) as n from orders o "
               "group by o.customer_id having count(*) > 3 order by n desc")
        got = self._verdict("exercise_04", sql)
        self.assertEqual(got["label"], "FAIL:vals")
        self.assertIn("expected text, got number", got["reason"])

    def test_a_wrong_boundary_is_caught(self):
        sql = ("select c.name, count(*) as n from orders o "
               "join customers c on c.id = o.customer_id "
               "group by c.name having count(*) >= 3 order by n desc")
        self.assertEqual(self._verdict("exercise_04", sql)["label"], "FAIL:rows")

    def test_a_missing_limit_is_caught(self):
        sql = ("select c.name, sum(o.order_usd_amount) as total "
               "from orders o join customers c on c.id = o.customer_id "
               "where o.status = 'completed' group by c.name order by total desc")
        got = self._verdict("exercise_05", sql)
        self.assertEqual(got["label"], "FAIL:rows")
        self.assertIn("expected 5", got["reason"])


if __name__ == "__main__":
    unittest.main()
