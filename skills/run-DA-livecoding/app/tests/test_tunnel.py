import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import threading
import time

import tunnel as tunnel_mod
from tunnel import (DEFAULT_PROVIDERS, PROVIDERS, TunnelCancelled, TunnelError,
                    looks_scary, start_tunnel, verify_reachable)


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


class TestVerifyReachable(unittest.TestCase):
    """A printed link must be proven to reach us, not merely assigned."""

    def setUp(self):
        tunnel_mod.VERIFY_INTERVAL_S = 0  # no real sleeping in tests

    def tearDown(self):
        tunnel_mod.VERIFY_INTERVAL_S = 3

    def test_our_own_404_is_the_proof(self):
        ok, reason = verify_reachable("https://x.example",
                                      opener=lambda url: (True, None))
        self.assertTrue(ok)
        self.assertIsNone(reason)

    def test_a_provider_error_page_is_not_accepted(self):
        ok, reason = verify_reachable(
            "https://x.example", timeout_s=0,
            opener=lambda url: (False, "HTTP 530 from the tunnel provider"))
        self.assertFalse(ok)
        self.assertIn("530", reason)

    def test_it_keeps_polling_until_dns_catches_up(self):
        # Cloudflare's record routinely appears ~20s after the URL is printed.
        attempts = []

        def opener(url):
            attempts.append(url)
            return (len(attempts) >= 4, None if len(attempts) >= 4 else "not reachable yet")

        ok, _ = verify_reachable("https://x.example", timeout_s=30, opener=opener)
        self.assertTrue(ok)
        self.assertEqual(len(attempts), 4)

    def test_ctrl_c_during_verification_is_honoured(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(TunnelCancelled):
            verify_reachable("https://x.example", cancel=cancel,
                             opener=lambda url: (False, "nope"))


class TestProviderRace(unittest.TestCase):
    """Providers run at the same time; the first link proven reachable wins,
    every other tunnel is shut down, and a dead first choice never delays."""

    def setUp(self):
        self.opened = []
        self.said = []
        self._open, self._verify = tunnel_mod._open_named, tunnel_mod.verify_reachable
        tunnel_mod.PROGRESS_EVERY_S = 0.2

    def tearDown(self):
        tunnel_mod._open_named, tunnel_mod.verify_reachable = self._open, self._verify
        tunnel_mod.PROGRESS_EVERY_S = 5

    def _arrange(self, reachable, delay=None):
        """reachable: {provider: bool}; delay: {provider: seconds before answering}"""
        delay = delay or {}

        class FakeTunnel:
            def __init__(self, provider):
                self.provider = provider
                self.url = f"https://fake.{provider}"
                self.stopped = False

            def stop(self):
                self.stopped = True

        made = {}

        def fake_open(provider, port, timeout_s, max_attempts, cancel):
            self.opened.append(provider)
            made[provider] = FakeTunnel(provider)
            return made[provider]

        def fake_verify(url, cancel=None, timeout_s=None, opener=None):
            provider = url.split("fake.", 1)[1]
            if cancel is not None and cancel.wait(delay.get(provider, 0)):
                raise TunnelCancelled()
            ok = reachable.get(provider, False)
            return (True, None) if ok else (False, "HTTP 530 from the tunnel provider")

        tunnel_mod._open_named = fake_open
        tunnel_mod.verify_reachable = fake_verify
        return made

    def test_the_reachable_provider_wins_and_the_other_is_stopped(self):
        made = self._arrange({"cloudflare": True, "localhost.run": False})
        got = start_tunnel(8765, say=self.said.append)
        self.assertEqual(got.provider, "cloudflare")
        self.assertEqual(set(self.opened), set(DEFAULT_PROVIDERS))
        self.assertFalse(made["cloudflare"].stopped)
        self.assertTrue(made["localhost.run"].stopped)

    def test_a_dead_first_choice_does_not_delay_the_second(self):
        # Cloudflare would only answer after 30s; localhost.run answers now.
        made = self._arrange({"cloudflare": True, "localhost.run": True},
                             delay={"cloudflare": 30})
        started = time.monotonic()
        got = start_tunnel(8765, say=self.said.append)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(got.provider, "localhost.run")
        time.sleep(0.3)  # the loser is told to stand down asynchronously
        self.assertTrue(made["cloudflare"].stopped, "the losing tunnel kept running")

    def test_only_one_provider_is_ever_handed_over(self):
        made = self._arrange({"cloudflare": True, "localhost.run": True})
        got = start_tunnel(8765, say=self.said.append)
        time.sleep(0.3)
        live = [t for t in made.values() if not t.stopped]
        self.assertEqual(live, [got])

    def test_every_provider_failing_is_a_clear_error(self):
        self._arrange({"cloudflare": False, "localhost.run": False})
        with self.assertRaises(TunnelError) as caught:
            start_tunnel(8765, say=self.said.append)
        message = str(caught.exception)
        for expected in ("Cloudflare quick tunnel", "localhost.run", "--no-tunnel"):
            self.assertIn(expected, message)

    def test_one_provider_can_be_forced(self):
        self._arrange({"localhost.run": True})
        got = start_tunnel(8765, providers=("localhost.run",), say=self.said.append)
        self.assertEqual(got.provider, "localhost.run")
        self.assertEqual(self.opened, ["localhost.run"])

    def test_progress_is_printed_while_waiting(self):
        self._arrange({"cloudflare": True, "localhost.run": True},
                      delay={"cloudflare": 1, "localhost.run": 1})
        start_tunnel(8765, say=self.said.append)
        self.assertTrue(any("still checking" in m for m in self.said), self.said)

    def test_ctrl_c_during_the_race_stops_everything(self):
        made = self._arrange({"cloudflare": True, "localhost.run": True},
                             delay={"cloudflare": 30, "localhost.run": 30})
        cancel = threading.Event()
        threading.Timer(0.3, cancel.set).start()
        with self.assertRaises(TunnelCancelled):
            start_tunnel(8765, cancel=cancel, say=self.said.append)
        # No sleep: by the time the caller sees TunnelCancelled every child
        # process must already be gone, because the caller is about to exit.
        self.assertTrue(all(t.stopped for t in made.values()))


class TestProviderSpecs(unittest.TestCase):
    def test_each_provider_declares_what_it_needs(self):
        for name, spec in PROVIDERS.items():
            for key in ("label", "needs", "url_re", "command", "install_hint"):
                self.assertIn(key, spec, name)
            command = spec["command"](8765)
            self.assertEqual(command[0], spec["needs"])
            self.assertTrue(any("8765" in part for part in command), name)

    def test_the_url_patterns_match_their_own_provider_only(self):
        cf = "https://blue-mountain-coffee-shop.trycloudflare.com"
        lhr = "https://a825e805530653.lhr.life"
        self.assertTrue(PROVIDERS["cloudflare"]["url_re"].search(cf))
        self.assertFalse(PROVIDERS["cloudflare"]["url_re"].search(lhr))
        self.assertTrue(PROVIDERS["localhost.run"]["url_re"].search(lhr))
        self.assertFalse(PROVIDERS["localhost.run"]["url_re"].search(cf))


if __name__ == "__main__":
    unittest.main()
