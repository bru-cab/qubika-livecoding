import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from exercises import (ExerciseError, list_exercise_names, load_exercise,
                       load_exercises, table_samples)

EXPECTED = ["exercise_01", "exercise_02", "exercise_03",
            "exercise_04", "exercise_05"]


class TestExercises(unittest.TestCase):
    def test_all_five_listed(self):
        self.assertEqual(list_exercise_names(), EXPECTED)

    def test_all_load_and_validate(self):
        for ex in load_exercises():
            self.assertTrue(ex.title)
            self.assertTrue(ex.statement_md)
            self.assertIn("create table", ex.schema_sql.lower())
            for t in ex.tables:
                self.assertTrue(os.path.isfile(t["csv_path"]))

    def test_table_samples_capped(self):
        ex = load_exercise("exercise_02")
        samples = table_samples(ex)
        self.assertEqual([s["name"] for s in samples], ["customers", "orders"])
        for s in samples:
            self.assertTrue(s["columns"])
            self.assertLessEqual(len(s["rows"]), ex.sample_rows)
            self.assertGreater(len(s["rows"]), 0)

    def test_unknown_exercise_raises(self):
        with self.assertRaises(ExerciseError):
            load_exercises(["no_such_exercise"])

    def test_subset_selection(self):
        exs = load_exercises(["exercise_01", "exercise_04"])
        self.assertEqual([e.name for e in exs],
                         ["exercise_01", "exercise_04"])

    def test_solutions_exist_for_all(self):
        for name in EXPECTED:
            path = os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "exercises", name, "solution.sql")
            self.assertTrue(os.path.isfile(path), f"{name} missing solution.sql")


if __name__ == "__main__":
    unittest.main()
