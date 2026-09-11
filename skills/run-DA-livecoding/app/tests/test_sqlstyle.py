"""SQL style flags: each rule must fire on the real thing and stay quiet on
legitimate formatting. Casing is deliberately not a rule."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sqlstyle

try:
    import duckdb  # noqa: F401
    HAS_DUCKDB = True
except ImportError:
    HAS_DUCKDB = False

CLEAN = "SELECT count(*) AS n\nFROM orders\nWHERE status = 'completed'"


def flags(sql, result_columns=None):
    got = sqlstyle.analyze(sql, result_columns)
    assert got is not None, "analyzer returned None"
    return got["flags"]


@unittest.skipUnless(HAS_DUCKDB, "duckdb not installed")
class TestCasingIsNotJudged(unittest.TestCase):
    def test_lowercase_uppercase_and_mixed_are_all_clean(self):
        for sql in (CLEAN, CLEAN.lower(), "Select count(*) As n\nFrom orders"):
            self.assertEqual(flags(sql), "", sql)


@unittest.skipUnless(HAS_DUCKDB, "duckdb not installed")
class TestRules(unittest.TestCase):
    def test_clean_query_has_no_flags(self):
        self.assertEqual(flags(CLEAN), "")

    def test_tabs(self):
        self.assertIn("T", flags("select 1\n\tfrom orders"))
        self.assertNotIn("T", flags(CLEAN))

    def test_a_tab_inside_a_string_literal_is_not_indentation(self):
        self.assertNotIn("T", flags("select 'a\tb' as x from t"))

    def test_inconsistent_indentation(self):
        # Verbatim from a real interview: the select list is indented with one
        # space on one line and two on the next.
        sql = ("with order_count as (\nselect\n customer_id,\n"
               "  count(distinct id) as number_of_orders\nfrom orders\n"
               "group by customer_id\n)\nselect c.name, o.number_of_orders\n"
               "from order_count o\ninner join customers c\n"
               "on o.customer_id = c.id\nwhere number_of_orders > 3\n"
               "order by number_of_orders desc")
        got = sqlstyle.analyze(sql)
        self.assertEqual(got["flags"], "I")
        self.assertIn("1, 2", got["issues"][0]["message"])

    def test_consistent_indentation_is_clean(self):
        for sql in ("select\n  a,\n  b,\n    c\nfrom t",   # 2 then 4
                    "select\n a,\n b\nfrom t",              # one width only
                    "select\n    a,\n      b\nfrom t"):     # 4 then 6
            self.assertNotIn("I", flags(sql), sql)

    def test_alignment_under_an_opening_paren_is_not_flagged(self):
        for sql in ("select c.name,\n       o.total\nfrom customers c\n"
                    "join (\n    select customer_id,\n"
                    "           sum(amount) as total\n    from orders\n"
                    "    group by customer_id\n) o on o.customer_id = c.id",
                    "select\n    a,\n    coalesce(b,\n             c) as bc\nfrom t"):
            self.assertNotIn("I", flags(sql), sql)

    def test_river_alignment_is_not_flagged(self):
        sql = ("SELECT c.name,\n       COUNT(*) AS n\n  FROM orders o\n"
               "  JOIN customers c ON c.id = o.customer_id\n GROUP BY c.name\n"
               "HAVING COUNT(*) > 3\n ORDER BY n DESC")
        self.assertNotIn("I", flags(sql))

    def test_long_line(self):
        self.assertIn("L", flags("select " + "a" * 110 + "\nfrom t"))

    def test_one_line_warmup_answer_is_clean(self):
        # exercise_01's own reference answer, joined onto one line.
        sql = "SELECT COUNT(*) AS completed_orders FROM orders WHERE status = 'completed'"
        self.assertNotIn("L", flags(sql))

    def test_a_short_one_line_filter_is_clean(self):
        for sql in ("select 1 from t where a = 1 and b = 2",
                    "select name from customers order by name limit 10",
                    "select count(*) from orders where status = 'x' and id > 5"):
            self.assertNotIn("L", flags(sql), sql)

    def test_one_line_heavy_query_is_flagged(self):
        sql = ("select c.name, sum(o.order_usd_amount) as t from orders o "
               "join customers c on c.id = o.customer_id group by c.name "
               "order by t desc limit 5")
        self.assertIn("L", flags(sql))

    def test_spacing_around_operators(self):
        self.assertIn("S", flags("select 1 from orders where status='completed'"))
        self.assertNotIn("S", flags(CLEAN))

    def test_spacing_accepts_operators_spelled_with_spaces(self):
        for sql in ("select 1 from t where a >= 1", "select 1 from t where a <> 1",
                    "select 1 from t where a != 1", "select 1 from t where a <= 1"):
            self.assertNotIn("S", flags(sql), sql)

    def test_a_newline_after_a_comma_is_not_a_spacing_problem(self):
        self.assertNotIn("S", flags("select a,\n       b\nfrom t"))

    def test_a_comma_run_together_is(self):
        self.assertIn("S", flags("select a,b from t"))

    def test_select_star(self):
        self.assertIn("*", flags("select * from orders"))
        self.assertIn("*", flags("select o.* from orders o"))

    def test_count_star_is_not_select_star(self):
        self.assertNotIn("*", flags("select count(*) from orders"))
        self.assertNotIn("*", flags("select sum(a * b) from t"))

    def test_multiplication_is_not_select_star(self):
        # The star's real neighbour is the literal, not SELECT.
        for sql in ("select 100.0 * count(*) / sum(amount) as pct from orders",
                    "select 100 * a from t", "select 2*3",
                    "select a, b*2 from t", "select (a+b) * 2 from t"):
            self.assertNotIn("*", flags(sql), sql)

    def test_a_qualified_star_anywhere_in_the_select_list(self):
        for sql in ("select a, t.* from t", "select o.name, o.* from orders o",
                    "select distinct a, t.* from t", "select *, a from t",
                    "select db.sch.t.* from t"):
            self.assertIn("*", flags(sql), sql)

    def test_unaliased_expression_column(self):
        self.assertIn("A", flags(CLEAN, ["count(id)"]))
        self.assertIn("A", flags(CLEAN, ["count_star()"]))
        self.assertNotIn("A", flags(CLEAN, ["n", "name"]))
        self.assertNotIn("A", flags(CLEAN, None))

    def test_flags_come_back_in_canonical_order(self):
        sql = ("select *,\n a,\n  b\n\tfrom orders\nwhere x=1 and "
               + "y" * 110 + "=2")
        self.assertEqual(flags(sql, ["count(id)"]), "TILS*A")

    def test_every_issue_carries_a_severity_and_message(self):
        got = sqlstyle.analyze("select * from orders where a=1", ["count(id)"])
        self.assertTrue(got["issues"])
        for issue in got["issues"]:
            self.assertIn(issue["severity"], ("minor", "major"))
            self.assertTrue(issue["message"])
            self.assertIn(issue["code"], sqlstyle.CANONICAL_ORDER)


@unittest.skipUnless(HAS_DUCKDB, "duckdb not installed")
class TestTokenizerEdges(unittest.TestCase):
    """duckdb.tokenize reports UTF-8 byte offsets and emits no comment tokens."""

    def test_keywords_inside_a_string_literal_are_ignored(self):
        self.assertNotIn("*", flags("select 'select *' as note from t"))

    def test_a_non_ascii_literal_does_not_shift_later_tokens(self):
        # Offsets are UTF-8 byte offsets: a two-byte Ñ must not move the
        # operators the spacing and star rules look at.
        self.assertEqual(flags("select 'Peña' as x from t where a = 1"), "")
        self.assertIn("S", flags("select 'Peña' as x from t where a=1"))
        self.assertIn("*", flags("with t as (select 'Ñandú' as x)\nselect * from t"))

    def test_comments_do_not_break_later_rules(self):
        self.assertIn("*", flags("select -- pick everything\n  * from t"))
        self.assertNotIn("S", flags("select 1 -- a=b\nfrom t"))

    def test_casts_quoted_and_non_ascii_identifiers_do_not_raise(self):
        for sql in ('select x::DOUBLE from t', 'select "name" from t',
                    'select ñ from t', 'select "weird col" from t'):
            self.assertIsNotNone(sqlstyle.analyze(sql), sql)

    def test_trailing_operators_do_not_raise(self):
        for sql in ("select a,", "select 1 =", "select 1 from t where a >"):
            self.assertIsNotNone(sqlstyle.analyze(sql), sql)

    def test_malformed_input_never_raises(self):
        for sql in ("", "   ", "not sql at all !!!", "select 'unterminated",
                    "\x00select 1", "select 1 /* unterminated", "x" * 10_000):
            self.assertIsNotNone(sqlstyle.analyze(sql), repr(sql[:20]))


@unittest.skipUnless(HAS_DUCKDB, "duckdb not installed")
class TestOverlongSqlIsRefused(unittest.TestCase):
    """Over-long SQL is rejected upstream and is never a real answer, and
    tokenizing it can take minutes while the run lock is held."""

    def test_sql_over_the_guardrail_limit_is_not_analyzed(self):
        import guardrails
        self.assertIsNone(sqlstyle.analyze("x" * (guardrails.MAX_SQL_CHARS + 1)))
        self.assertIsNotNone(sqlstyle.analyze("x" * guardrails.MAX_SQL_CHARS))

    def test_a_pasted_wall_of_comment_openers_returns_at_once(self):
        import time
        start = time.perf_counter()
        self.assertIsNone(sqlstyle.analyze("/*" * 125_000))
        self.assertLess(time.perf_counter() - start, 1.0)


class TestAnalyzerUnavailable(unittest.TestCase):
    def test_missing_duckdb_gives_none_not_an_error(self):
        saved = sys.modules.get("duckdb")
        sqlstyle._warned = True  # keep the one-time warning out of the test output
        sys.modules["duckdb"] = None
        try:
            self.assertIsNone(sqlstyle.analyze("select 1"))
        finally:
            if saved is None:
                del sys.modules["duckdb"]
            else:
                sys.modules["duckdb"] = saved
            sqlstyle._warned = False


if __name__ == "__main__":
    unittest.main()
