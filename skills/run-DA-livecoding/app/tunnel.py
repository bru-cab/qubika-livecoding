"""Spawn a Cloudflare quick tunnel and extract its ephemeral public URL.

Quick tunnels are free, need no account, and die with the process — which is
exactly the lifecycle we want: Ctrl+C on serve.py kills the link.

The subdomain is a random 4-word name assigned by Cloudflare. Occasionally the
word lottery produces something a candidate would never click (a real example:
spyware-cooper-retailer-facts.trycloudflare.com), so start_tunnel discards
off-putting names and mints a new tunnel until it gets a presentable one.
"""
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque

URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
INSTALL_HINT = (
    "cloudflared is not installed. Install it with:\n"
    "  brew install cloudflared\n"
    "or run with --no-tunnel to use localhost only."
)

# Words that make a link look malicious or inappropriate to a candidate.
SCARY_WORDS = {
    "spyware", "malware", "virus", "trojan", "worm", "ransom", "ransomware",
    "phishing", "phish", "scam", "spam", "fraud", "fake", "hack", "hacker",
    "hacked", "hacking", "exploit", "botnet", "keylogger", "attack", "threat",
    "danger", "dangerous", "illegal", "stolen", "steal", "leak", "breach",
    "porn", "sex", "adult", "nude", "casino", "gambling", "betting", "drug",
    "drugs", "weapon", "weapons", "gun", "guns", "bomb", "kill", "killer",
    "dead", "death", "suicide", "nazi", "terror", "terrorist",
}


class TunnelError(Exception):
    pass


class TunnelCancelled(Exception):
    """The caller asked to shut down while the tunnel was still starting."""


class Tunnel:
    def __init__(self, proc, url, tail):
        self.proc = proc
        self.url = url
        self._tail = tail

    def alive(self):
        return self.proc.poll() is None

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def looks_scary(url):
    """True if the random subdomain contains a word we won't send a candidate."""
    subdomain = url.split("//", 1)[-1].split(".", 1)[0]
    return any(word in SCARY_WORDS for word in subdomain.split("-"))


def start_tunnel(port, timeout_s=20, max_attempts=5, cancel=None):
    """Bring up a quick tunnel with a presentable name.

    `cancel` is an optional threading.Event: when it is set (Ctrl+C during
    startup) the wait is abandoned immediately and TunnelCancelled is raised,
    so the caller can shut down cleanly instead of hanging for timeout_s.
    """
    if shutil.which("cloudflared") is None:
        raise TunnelError(INSTALL_HINT)
    rejected = []
    for _ in range(max_attempts):
        tunnel = _spawn_once(port, timeout_s, cancel)
        if not looks_scary(tunnel.url):
            return tunnel
        rejected.append(tunnel.url)
        print(f"  (discarded {tunnel.url} — off-putting name, "
              f"minting another…)", file=sys.stderr, flush=True)
        tunnel.stop()
        if cancel is not None and cancel.is_set():
            raise TunnelCancelled()
    raise TunnelError(
        f"Could not get a presentable link name in {max_attempts} attempts "
        f"(discarded: {', '.join(rejected)}). Run serve.py again.")


def _spawn_once(port, timeout_s, cancel=None):
    proc = subprocess.Popen(
        ["cloudflared", "tunnel", "--url", f"http://127.0.0.1:{port}",
         "--no-autoupdate"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    tail = deque(maxlen=20)
    found = threading.Event()
    url_holder = []

    def _drain():
        # Keep draining even after the URL is found: a full pipe buffer
        # would freeze cloudflared.
        for line in proc.stdout:
            tail.append(line.rstrip())
            if not found.is_set():
                m = URL_RE.search(line)
                if m:
                    url_holder.append(m.group(0))
                    found.set()

    def _kill():
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    threading.Thread(target=_drain, daemon=True).start()
    # Poll instead of one long wait, so a shutdown request is honoured at once.
    deadline = time.monotonic() + timeout_s
    while not found.wait(0.2):
        if cancel is not None and cancel.is_set():
            _kill()
            raise TunnelCancelled()
        if time.monotonic() >= deadline:
            _kill()
            raise TunnelError(
                f"Could not get the tunnel URL within {timeout_s}s. "
                "cloudflared output:\n" + "\n".join(tail))
    return Tunnel(proc, url_holder[0], tail)
