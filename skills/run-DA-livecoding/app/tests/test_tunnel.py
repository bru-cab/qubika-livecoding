import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tunnel import looks_scary


class TestScaryNameFilter(unittest.TestCase):
    def test_scary_names_rejected(self):
        for url in [
            "https://spyware-cooper-retailer-facts.trycloudflare.com",
            "https://nice-malware-blue-sky.trycloudflare.com",
            "https://casino-royale-week-end.trycloudflare.com",
            "https://one-two-attack-plan.trycloudflare.com",
        ]:
            self.assertTrue(looks_scary(url), url)

    def test_normal_names_accepted(self):
        for url in [
            "https://predicted-bedroom-civil-african.trycloudflare.com",
            "https://blue-mountain-coffee-shop.trycloudflare.com",
            "https://quiet-river-summer-days.trycloudflare.com",
        ]:
            self.assertFalse(looks_scary(url), url)

    def test_substring_does_not_false_positive(self):
        # "hackberry" contains "hack" but the filter matches whole words
        # between hyphens, so unrelated words must pass.
        self.assertFalse(looks_scary("https://hackberry-tree-green-leaf.trycloudflare.com"))


if __name__ == "__main__":
    unittest.main()
