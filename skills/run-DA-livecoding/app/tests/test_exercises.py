import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from exercises import (DEFAULT_CHECK, EXERCISES_DIR, ExerciseError,
                       list_exercise_names, load_exercise, load_exercises,
                       table_samples)

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


class TestSolutionAndCheck(unittest.TestCase):
    """solution.sql and the "check" block feed the interviewer's result column."""

    def test_every_seed_exercise_carries_its_solution(self):
        for ex in load_exercises():
            self.assertTrue(ex.solution_sql, ex.name)
            self.assertTrue(ex.solution_sql.lower().startswith("select"), ex.name)

    def test_seed_check_options(self):
        self.assertEqual(load_exercise("exercise_04").check,
                         {"enabled": True, "ordered": True,
                          "order_by": ["order_count"], "decimals": 2})
        self.assertFalse(load_exercise("exercise_01").check["ordered"])
        self.assertEqual(load_exercise("exercise_02").check["order_by"],
                         ["order_usd_amount"])

    def test_generator_still_reproduces_the_committed_files(self):
        # Guards the fixed seed: regenerating must not silently change the data
        # the reference results are computed from.
        import gen_exercises
        for name, spec in gen_exercises.EXERCISES.items():
            for relpath, content in gen_exercises.render_exercise(name, spec).items():
                path = os.path.join(EXERCISES_DIR, name, relpath)
                with open(path, "rb") as f:
                    self.assertEqual(f.read(), content, f"{name}/{relpath}")


class TestCheckValidation(unittest.TestCase):
    """A typo in exercise.json must fail at startup, not mid-interview."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = os.path.join(self.tmp.name, "ex")
        shutil.copytree(os.path.join(EXERCISES_DIR, "exercise_01"), self.folder)
        self.meta_path = os.path.join(self.folder, "exercise.json")
        with open(self.meta_path, encoding="utf-8") as f:
            self.meta = json.load(f)

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, check="__omit__"):
        meta = dict(self.meta)
        meta.pop("check", None)
        if check != "__omit__":
            meta["check"] = check
        with open(self.meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f)
        return lambda: load_exercise("ex", exercises_dir=self.tmp.name)

    def test_an_exercise_without_a_check_block_still_loads(self):
        self.assertEqual(self._write()().check, DEFAULT_CHECK)

    def test_an_exercise_without_a_solution_still_loads(self):
        load = self._write()
        os.remove(os.path.join(self.folder, "solution.sql"))
        self.assertIsNone(load().solution_sql)

    def test_partial_check_blocks_are_merged_over_the_defaults(self):
        self.assertEqual(self._write({"ordered": True})().check,
                         dict(DEFAULT_CHECK, ordered=True))

    def test_naming_order_columns_turns_order_checking_on(self):
        # order_by alone used to be inert, which silently stopped checking.
        self.assertTrue(self._write({"order_by": ["a"]})().check["ordered"])
        self.assertTrue(self._write({"order_by": ["a"], "ordered": True})()
                        .check["ordered"])
        self.assertFalse(self._write({"ordered": False})().check["ordered"])

    def test_the_default_check_is_never_aliased(self):
        first = self._write()().check["order_by"]
        second = self._write()().check["order_by"]
        self.assertIsNot(first, second)
        self.assertIsNot(first, DEFAULT_CHECK["order_by"])

    def test_bad_check_blocks_are_rejected(self):
        for bad in ("yes", ["ordered"], {"orderd": True}, {"ordered": "yes"},
                    {"order_by": "order_count"}, {"order_by": [1]},
                    {"decimals": 99}, {"decimals": "2"}, {"decimals": True},
                    {"enabled": 1}):
            with self.subTest(check=bad):
                with self.assertRaises(ExerciseError):
                    self._write(bad)()


if __name__ == "__main__":
    unittest.main()
