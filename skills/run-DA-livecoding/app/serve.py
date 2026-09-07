#!/usr/bin/env python3
"""Qubika SQL Interview — local livecoding SQL server for interviews.

Usage:
    python3 serve.py                          # all exercises, with tunnel
    python3 serve.py --exercise exercise_01,exercise_02
    python3 serve.py --no-tunnel              # localhost only (testing)
    python3 serve.py --list                   # list available exercises

Flow: starts the server + a Cloudflare quick tunnel, prints the candidate
link to paste in the Meet chat, and shows every executed query live.
Ctrl+C ends the session and kills the link instantly.
"""
import argparse
import errno
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime

from engine import DuckDBEngine, EngineError
from exercises import ExerciseError, list_exercise_names, load_exercise, load_exercises, table_samples
from server import build_server
from session import Session
from tunnel import TunnelCancelled, TunnelError, start_tunnel

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))


def _default_data_dir():
    """Where session logs go when the env var isn't set.

    In the dev checkout that's <workspace>/data/data_analytics_livecoding.
    Anywhere else (e.g. installed as a plugin) logs must NOT land inside the
    install directory, so they go to ~/qubika-sql-interviews — which also
    means the run command needs no environment variable.
    """
    workspace = os.path.dirname(os.path.dirname(PROJECT_DIR))
    if os.path.isdir(os.path.join(workspace, "data")):
        return os.path.join(workspace, "data", "data_analytics_livecoding")
    return os.path.join(os.path.expanduser("~"), "qubika-sql-interviews")


DATA_DIR = os.environ.get("DATA_ANALYTICS_LIVECODING_DATA_DIR",
                          _default_data_dir())
SESSIONS_DIR = os.path.join(DATA_DIR, "sessions")
STATIC_DIR = os.path.join(PROJECT_DIR, "static")
EXERCISES_DIR = os.path.join(PROJECT_DIR, "exercises")

DEFAULT_PORT = 8765
DEFAULT_TTL_MIN = 180
LINE = "─" * 72
DLINE = "═" * 72


def main():
    parser = argparse.ArgumentParser(
        description="Local livecoding SQL server for interviews.")
    parser.add_argument("--exercise", help="comma-separated list (default: all)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--ttl", type=int, default=DEFAULT_TTL_MIN,
                        help=f"session lifetime in minutes (default {DEFAULT_TTL_MIN})")
    parser.add_argument("--no-tunnel", action="store_true",
                        help="no public tunnel, localhost only")
    parser.add_argument("--list", action="store_true",
                        help="list available exercises and exit")
    args = parser.parse_args()

    if args.list:
        names = list_exercise_names()
        if not names:
            print("No exercises found in exercises/.")
            return 0
        for name in names:
            ex = load_exercise(name)
            print(f"  {name:<15} [{ex.difficulty:<10}] {ex.focus}")
        return 0

    names = [s.strip() for s in args.exercise.split(",") if s.strip()] if args.exercise else None
    try:
        exercises = load_exercises(names)
    except ExerciseError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    if not exercises:
        print("ERROR: no exercises to serve.", file=sys.stderr)
        return 1

    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    session_dir = os.path.join(SESSIONS_DIR, stamp)
    session = Session([e.name for e in exercises], args.ttl, session_dir)
    # Armed before anything can block: startup (seeding, the tunnel wait) is
    # long enough that a Ctrl+C there would otherwise escape as a traceback
    # and leave cloudflared and temp databases behind.
    _install_signal_handlers(session)

    eng = DuckDBEngine(exercises, session_dir)
    try:
        eng.prepare()
    except EngineError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return _abandon_startup(session, eng)
    if session.shutdown.is_set():
        return _abandon_startup(session, eng)

    html_path = os.path.join(STATIC_DIR, "candidate.html")
    if not os.path.isfile(html_path):
        print(f"ERROR: missing {html_path}", file=sys.stderr)
        return _abandon_startup(session, eng)
    with open(html_path, encoding="utf-8") as f:
        candidate_html = f.read()

    bootstrap = {
        "exercises": [
            {"name": e.name, "title": e.title, "difficulty": e.difficulty,
             "statement_md": e.statement_md, "schema_sql": e.schema_sql,
             "tables": table_samples(e)}
            for e in exercises
        ],
        "row_cap": eng.row_cap,
        "timeout_s": eng.timeout_s,
    }

    try:
        httpd = build_server(session, eng, bootstrap, candidate_html, args.port)
    except OSError as e:
        if e.errno == errno.EADDRINUSE:
            print(f"ERROR: port {args.port} is already in use — most likely a "
                  f"previous serve.py is still running in another terminal. Close "
                  f"it with Ctrl+C or run with --port <other port>.", file=sys.stderr)
        else:
            print(f"ERROR: could not open port {args.port}: {e}", file=sys.stderr)
        return _abandon_startup(session, eng)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    caffeinate = _start_caffeinate()

    tunnel = None
    base_url = f"http://127.0.0.1:{port}"
    if not args.no_tunnel:
        print("Starting Cloudflare tunnel… (Ctrl+C to abort)", flush=True)
        try:
            tunnel = start_tunnel(port, cancel=session.shutdown)
            base_url = tunnel.url
        except TunnelCancelled:
            return _abandon_startup(session, eng, httpd, caffeinate)
        except TunnelError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return _abandon_startup(session, eng, httpd, caffeinate)
    if session.shutdown.is_set():
        return _abandon_startup(session, eng, httpd, caffeinate)

    candidate_url = f"{base_url}/c?t={session.token}"
    session.log({"type": "session_start", "ts": time.time(),
                 "exercises": [e.name for e in exercises],
                 "ttl_min": args.ttl, "url": candidate_url})

    _print_banner(candidate_url, exercises, session, args, port, tunnel)

    tunnel_warned = False
    try:
        while session.active():
            session.shutdown.wait(timeout=5)
            if tunnel and not tunnel_warned and not tunnel.alive():
                tunnel_warned = True
                print("\n⚠️  The Cloudflare tunnel went down — the public link stopped "
                      "working. Restart serve.py to generate a new link.\n",
                      flush=True)
    except KeyboardInterrupt:  # belt and braces if a handler didn't install
        session.shutdown_reason = "ctrl_c"
    finally:
        reason = session.shutdown_reason or "ttl_expired"
        _say("\nEnding session…")
        # Order matters: stop accepting, kill the public link, cancel any
        # in-flight query, wait for its log entry, and only then close the
        # log file — otherwise the last run can be lost.
        session.shutdown.set()
        if tunnel:
            tunnel.stop()
        eng.interrupt_all()
        if session.run_lock.acquire(timeout=10):
            session.run_lock.release()
        httpd.shutdown()
        session.end(reason)
        _close_engine(eng)
        if caffeinate:
            caffeinate.terminate()

    _print_farewell(reason, session)
    return 0


def _say(message):
    """print() that survives a terminal window that is already gone."""
    try:
        print(message, flush=True)
    except OSError:
        pass


def _close_engine(eng, timeout_s=5):
    """Close DuckDB without ever letting it hold the terminal hostage.

    Every other thread is a daemon, so if a connection is wedged in native
    code we can simply stop waiting and let the process exit.
    """
    closer = threading.Thread(target=eng.close, daemon=True)
    closer.start()
    closer.join(timeout=timeout_s)


def _abandon_startup(session, eng, httpd=None, caffeinate=None):
    """Tear down a session that never reached the interview. Returns exit code.

    Nothing was ever served, so the half-written session directory is dropped
    rather than left behind as an empty log.
    """
    session.shutdown.set()
    if httpd is not None:
        httpd.shutdown()
    _close_engine(eng)
    if caffeinate is not None:
        caffeinate.terminate()
    session.discard()
    if session.shutdown_reason:  # a signal, not an error — the user is waiting
        _say("Cancelled before the session started — nothing was served.")
        return 0
    return 1


def _print_banner(candidate_url, exercises, session, args, port, tunnel):
    """Everything the interviewer needs, without leaving this window."""
    n = len(exercises)
    first, last = exercises[0].name, exercises[-1].name
    span = first if n == 1 else f"{first} … {last}"

    print()
    print(DLINE)
    print(f"  QUBIKA SQL INTERVIEW · {n} exercise{'' if n == 1 else 's'} · ready")
    print(DLINE)
    print()
    if tunnel:
        print("  1. COPY THIS LINK and paste it in the Meet chat:")
    else:
        print("  1. LOCAL MODE — this link only works on this machine:")
    print()
    print(f"       {candidate_url}")
    print()
    if tunnel:
        print("  2. Ask the candidate to SHARE THEIR SCREEN on Meet.")
        print(f"     They will see {span} and can move between them freely.")
    else:
        print("     The candidate CANNOT open it. Restart without --no-tunnel")
        print("     to get a public link they can open.")
        print()
        print("  2. SHARE YOUR OWN SCREEN on Meet with this page open and have")
        print("     the candidate dictate the SQL — they type nothing.")
        print(f"     You have {span} and can move between them freely.")
    print()
    print("  3. Every query run appears here, live, as it happens.")
    print()
    print("  4. Press Ctrl+C IN THIS WINDOW when you are done.")
    print("     The link dies instantly and nobody can reopen it.")
    print()
    print(LINE)
    print("  Keep this window open — closing it ends the interview.")
    print(f"  Answers    {os.path.join(EXERCISES_DIR, '<exercise>', 'solution.sql')}")
    print(f"  Log        {session.log_path}")
    if tunnel:
        print(f"  Local      http://127.0.0.1:{port}/c?t={session.token}")
    print(f"  Expires    in {args.ttl} min, if you forget the Ctrl+C")
    print(f"  Process    pid {os.getpid()} on port {port}")
    print(DLINE)
    print(f"  {'time':<10}{'exercise':<18}{'status':<10}{'rows':>6}{'ms':>8}   query")
    print(LINE, flush=True)


def _print_farewell(reason, session):
    why = {"ctrl_c": "you pressed Ctrl+C",
           "ttl_expired": "the time limit was reached",
           "terminated": "the process was stopped",
           "window_closed": "the terminal window was closed"}.get(reason, reason)
    ran = len(session.runs)
    _say("\n" + DLINE
         + f"\n  SESSION ENDED — {why}. The link is dead."
         + f"\n  {ran} quer{'y' if ran == 1 else 'ies'} run · log: {session.log_path}"
         + "\n" + DLINE)


def _install_signal_handlers(session):
    """Shut down cleanly on Ctrl+C, `kill`, or the terminal window closing.

    Relying on KeyboardInterrupt alone is not enough: SIGHUP (window closed)
    and SIGTERM would kill the process without finalizing the session log,
    and a shell that starts us in the background inherits SIGINT as ignored.
    Installing explicit handlers makes every exit path run the same cleanup.
    """
    reasons = {signal.SIGINT: "ctrl_c",
               signal.SIGTERM: "terminated",
               signal.SIGHUP: "window_closed"}

    def _handler(signum, _frame):
        session.shutdown_reason = reasons.get(signum, f"signal {signum}")
        session.shutdown.set()
        # Restore the defaults: if cleanup somehow wedges, a second Ctrl+C
        # must kill the process outright instead of being swallowed.
        for s in reasons:
            try:
                signal.signal(s, signal.SIG_DFL)
            except (ValueError, OSError):
                pass

    for sig in reasons:
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            pass  # not the main thread, or the platform lacks the signal


def _start_caffeinate():
    """Keep the Mac awake while the interview runs; dies with this process."""
    if shutil.which("caffeinate") is None:
        return None
    return subprocess.Popen(
        ["caffeinate", "-dims", "-w", str(os.getpid())],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    sys.exit(main())
