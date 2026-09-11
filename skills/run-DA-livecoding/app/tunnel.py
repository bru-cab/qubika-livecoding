"""Publish the local interview server on an ephemeral public URL.

Both providers are free, need no account, and die with the process — exactly
the lifecycle we want: Ctrl+C on serve.py kills the link.

    cloudflare     cloudflared quick tunnel, *.trycloudflare.com
    localhost.run  an SSH remote forward, *.lhr.life (no install: just ssh)

A printed link is never trusted on faith. The provider hands over a URL as
soon as it has *assigned* one, which is not the same as the link working: the
DNS record can lag by ~20s, and during a trycloudflare outage it may never
appear at all, leaving the interviewer to paste a dead link into the Meet chat.
So start_tunnel fetches the URL itself until this very server answers through
it, and moves on to the next provider when it does not.

Cloudflare's subdomain is a random 4-word name. Occasionally the word lottery
produces something a candidate would never click (a real example:
spyware-cooper-retailer-facts.trycloudflare.com), so those are discarded and
another tunnel is minted.
"""
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque

URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
INSTALL_HINT = (
    "cloudflared is not installed. Install it with:\n"
    "  brew install cloudflared\n"
    "or run with --no-tunnel to use localhost only."
)

# How long a link may take to become reachable before we give up on it.
# Cloudflare's DNS record routinely takes ~20s. Because every provider is
# tried at the same time, a slow one never delays a fast one.
VERIFY_TIMEOUT_S = 75
VERIFY_INTERVAL_S = 3
PROGRESS_EVERY_S = 5  # a waiting interviewer must see that we are alive
# On Ctrl+C during startup, how long to wait for the losing providers to kill
# their child processes before the interpreter exits and orphans them.
TEARDOWN_WAIT_S = 8

PROVIDERS = {
    "cloudflare": {
        "label": "Cloudflare quick tunnel",
        "needs": "cloudflared",
        "url_re": URL_RE,
        "screen_names": True,
        "install_hint": INSTALL_HINT,
        "command": lambda port: [
            "cloudflared", "tunnel", "--url", f"http://127.0.0.1:{port}",
            "--no-autoupdate"],
    },
    "localhost.run": {
        "label": "localhost.run",
        "needs": "ssh",
        "url_re": re.compile(r"https://[a-z0-9-]+\.lhr\.life"),
        "screen_names": False,  # hex names: no word lottery to worry about
        "install_hint": "ssh is not available, so localhost.run cannot be used.",
        "command": lambda port: [
            "ssh", "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ServerAliveInterval=30", "-o", "ExitOnForwardFailure=yes",
            "-R", f"80:127.0.0.1:{port}", "nokey@localhost.run"],
    },
}
DEFAULT_PROVIDERS = ("cloudflare", "localhost.run")

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
    def __init__(self, proc, url, tail, provider="cloudflare"):
        self.proc = proc
        self.url = url
        self.provider = provider
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


def verify_reachable(url, cancel=None, timeout_s=VERIFY_TIMEOUT_S, opener=None):
    """Wait until THIS server answers through the public URL.

    Returns (True, None) once it does, or (False, reason) after timeout_s.
    Every route of ours needs a session token, so an unknown path returns a
    bland 404 — which is exactly the fingerprint we look for. A provider whose
    tunnel is not wired up yet answers 502/530 or does not resolve at all, and
    the candidate page is never fetched, so nothing lands in the session log.
    """
    fetch = opener or _probe
    deadline = time.monotonic() + timeout_s
    reason = "no answer yet"
    while True:
        if cancel is not None and cancel.is_set():
            raise TunnelCancelled()
        ok, reason = fetch(url)
        if ok:
            return True, None
        if time.monotonic() >= deadline:
            return False, reason
        if cancel is not None:
            if cancel.wait(VERIFY_INTERVAL_S):
                raise TunnelCancelled()
        else:
            time.sleep(VERIFY_INTERVAL_S)


def _probe(url):
    """(is it our server?, what came back instead)."""
    request = urllib.request.Request(
        url.rstrip("/") + "/", headers={"User-Agent": "qubika-sql-interview"})
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            return False, f"HTTP {response.status} from the tunnel provider"
    except urllib.error.HTTPError as e:
        body = b""
        try:
            body = e.read(64)
        except Exception:
            pass
        if e.code == 404 and b"Not Found" in body:
            return True, None
        return False, f"HTTP {e.code} from the tunnel provider"
    except urllib.error.URLError as e:
        return False, f"not reachable yet ({e.reason})"
    except Exception as e:
        return False, f"not reachable yet ({type(e).__name__})"


def start_tunnel(port, timeout_s=20, max_attempts=5, cancel=None,
                 providers=DEFAULT_PROVIDERS, verify_s=VERIFY_TIMEOUT_S,
                 say=None):
    """Bring up a public link that is proven to reach this server.

    Every provider is started at the same time and the first link that this
    server answers through wins; the others are shut down. So a trycloudflare
    outage costs nothing but the few seconds localhost.run needs, and the
    interviewer is never handed a dead link. Progress is printed while waiting
    because a silent 30-second pause looks exactly like a hang.

    `cancel` is an optional threading.Event: when it is set (Ctrl+C during
    startup) everything is torn down at once and TunnelCancelled is raised.
    """
    announce = say or (lambda message: print(message, file=sys.stderr, flush=True))
    stand_down = threading.Event()  # set by the winner; tells the rest to stop
    claim = threading.Lock()         # so two providers cannot both "win"
    outcomes = queue.Queue()         # (provider, tunnel | None, reason | None)
    failures = []
    racing = []
    workers = []

    for name in providers:
        spec = PROVIDERS[name]
        if shutil.which(spec["needs"]) is None:
            failures.append(f"{spec['label']}: {spec['install_hint']}")
            continue
        worker = threading.Thread(
            target=_race_one,
            args=(name, port, timeout_s, max_attempts, verify_s, stand_down,
                  claim, outcomes, announce),
            daemon=True)
        worker.start()
        workers.append(worker)
        racing.append(name)
    if not racing:
        raise TunnelError(
            "No public link could be established:\n  - "
            + "\n  - ".join(failures)
            + "\nRun with --no-tunnel and share your own screen instead.")

    started = time.monotonic()
    next_progress = started + PROGRESS_EVERY_S
    pending = set(racing)
    winner = None
    while pending and winner is None:
        if cancel is not None and cancel.is_set():
            stand_down.set()
            # The caller is about to exit the process: give every worker the
            # time to terminate its child, or the tunnel outlives the interview.
            _join_all(workers, TEARDOWN_WAIT_S)
            _drain_and_stop(outcomes)
            raise TunnelCancelled()
        try:
            name, tunnel, reason = outcomes.get(timeout=0.5)
        except queue.Empty:
            if time.monotonic() >= next_progress:
                elapsed = int(time.monotonic() - started)
                announce(f"  still checking… {elapsed}s "
                         f"({', '.join(PROVIDERS[n]['label'] for n in sorted(pending))})")
                next_progress += PROGRESS_EVERY_S
            continue
        pending.discard(name)
        if tunnel is not None:
            winner = tunnel
        else:
            failures.append(f"{PROVIDERS[name]['label']}: {reason}")

    stand_down.set()  # every other worker stops its tunnel and exits
    if winner is not None:
        # Anything that finished in the meantime must not be left running.
        _drain_and_stop(outcomes, keep=winner)
        return winner
    raise TunnelError(
        "No public link could be established:\n  - "
        + "\n  - ".join(failures)
        + "\nRun with --no-tunnel and share your own screen instead.")


def _race_one(name, port, timeout_s, max_attempts, verify_s, stand_down,
              claim, outcomes, announce):
    """One provider's attempt, start to verified link, reported on `outcomes`."""
    label = PROVIDERS[name]["label"]
    tunnel = None
    try:
        tunnel = _open_named(name, port, timeout_s, max_attempts, stand_down)
        announce(f"  {label}: link assigned, checking it is reachable…")
        ok, reason = verify_reachable(tunnel.url, stand_down, verify_s)
        if ok:
            with claim:
                if not stand_down.is_set():
                    stand_down.set()  # claimed: everyone else stands down
                    outcomes.put((name, tunnel, None))
                    return
            tunnel.stop()  # a rival claimed first, a moment ago
            outcomes.put((name, None, "stood down"))
            return
        tunnel.stop()
        announce(f"  {label}: never became reachable ({reason}).")
        outcomes.put((name, None, f"the link never became reachable ({reason})"))
    except TunnelCancelled:  # a rival won, or the interviewer pressed Ctrl+C
        if tunnel is not None:
            tunnel.stop()
        outcomes.put((name, None, "stood down"))
    except TunnelError as e:
        outcomes.put((name, None, str(e)))
    except Exception as e:  # never let a provider bug hang the startup
        if tunnel is not None:
            tunnel.stop()
        outcomes.put((name, None, f"{type(e).__name__}: {e}"))


def _join_all(workers, timeout_s):
    deadline = time.monotonic() + timeout_s
    for worker in workers:
        worker.join(max(0.0, deadline - time.monotonic()))


def _drain_and_stop(outcomes, keep=None):
    """Stop any tunnel a late worker may still hand over."""
    while True:
        try:
            _, tunnel, _ = outcomes.get_nowait()
        except queue.Empty:
            return
        if tunnel is not None and tunnel is not keep:
            tunnel.stop()


def _open_named(provider, port, timeout_s, max_attempts, cancel):
    """Spawn `provider`, retrying while the assigned name is off-putting."""
    if not PROVIDERS[provider]["screen_names"]:
        return _spawn_once(port, timeout_s, cancel, provider)
    rejected = []
    for _ in range(max_attempts):
        tunnel = _spawn_once(port, timeout_s, cancel, provider)
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
        f"(discarded: {', '.join(rejected)}).")


def _spawn_once(port, timeout_s, cancel=None, provider="cloudflare"):
    spec = PROVIDERS[provider]
    proc = subprocess.Popen(
        spec["command"](port),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        stdin=subprocess.DEVNULL)
    tail = deque(maxlen=20)
    found = threading.Event()
    url_holder = []

    def _drain():
        # Keep draining even after the URL is found: a full pipe buffer
        # would freeze cloudflared.
        for line in proc.stdout:
            tail.append(line.rstrip())
            if not found.is_set():
                m = spec["url_re"].search(line)
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
        if proc.poll() is not None:
            raise TunnelError(
                f"{spec['label']} exited before giving a URL. Output:\n"
                + "\n".join(tail))
        if time.monotonic() >= deadline:
            _kill()
            raise TunnelError(
                f"Could not get a URL from {spec['label']} within {timeout_s}s."
                " Output:\n" + "\n".join(tail))
    return Tunnel(proc, url_holder[0], tail, provider)
