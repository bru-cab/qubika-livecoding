"""Session extras: the candidate's name and the slug it contributes to the
session folder."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from session import Session, slugify


class TestSlugify(unittest.TestCase):
    def test_accents_are_folded_to_ascii(self):
        self.assertEqual(slugify("María Pérez"), "maria-perez")
        self.assertEqual(slugify("Ana María Pérez-López"), "ana-maria-perez-lopez")

    def test_only_lowercase_letters_digits_and_dashes_survive(self):
        self.assertEqual(slugify("Jane Doe (candidate #2)"), "jane-doe-candidate-2")

    def test_path_separators_cannot_escape_the_sessions_folder(self):
        for name in ("../../etc", "/etc/passwd", "..", "./.."):
            slug = slugify(name)
            self.assertNotIn("/", slug)
            self.assertNotIn("..", slug)

    def test_a_name_with_no_ascii_letters_yields_nothing(self):
        for name in ("李雷", "", "   ", None, "!!!"):
            self.assertEqual(slugify(name), "")

    def test_length_is_capped_without_a_trailing_dash(self):
        slug = slugify("Alexandra Bartholomew Christopherson Delacroix Everett")
        self.assertLessEqual(len(slug), 40)
        self.assertFalse(slug.endswith("-"))


class TestSessionCandidate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _session(self, **kwargs):
        return Session(["ex1"], ttl_min=60, session_dir=self.tmp.name, **kwargs)

    def test_no_candidate_by_default(self):
        self.assertIsNone(self._session().candidate)

    def test_candidate_is_kept_verbatim(self):
        self.assertEqual(self._session(candidate="María Pérez").candidate,
                         "María Pérez")


if __name__ == "__main__":
    unittest.main()
