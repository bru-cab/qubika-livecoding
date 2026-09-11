"""End-to-end shutdown tests: the interviewer must always be able to stop it.

Each signal is sent to a server started as a *background* job, which is the
worst case — a shell that backgrounds a process makes it inherit SIGINT as
ignored, so this also proves the explicit handlers override that.
"""
import http.client
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVE = os.path.join(APP_DIR, "serve.py")

try:
    import duckdb  # noqa: F401
    HAS_DUCKDB = True
except ImportError:
    HAS_DUCKDB = False


@unittest.skipUnless(HAS_DUCKDB, "duckdb not installed")
class TestCleanShutdown(unittest.TestCase):
    def _run_and_signal(self, sig, port):
        data_dir = tempfile.mkdtemp(prefix="qsi_shutdown_")
        env = dict(os.environ, DATA_ANALYTICS_LIVECODING_DATA_DIR=data_dir)
        proc = subprocess.Popen(
            [sys.executable, SERVE, "--no-tunnel", "--port", str(port)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env,
            start_new_session=True)  # detached, like a background job
        try:
            deadline = time.time() + 25
            while time.time() < deadline:
                if os.path.isdir(os.path.join(data_dir, "sessions")):
                    break
                if proc.poll() is not None:
                    self.fail(f"server exited early: {proc.communicate()[0]}")
                time.sleep(0.2)
            time.sleep(1.0)  # let the banner finish printing

            proc.send_signal(sig)
            try:
                out = proc.communicate(timeout=20)[0]
            except subprocess.TimeoutExpired:
                proc.kill()
                self.fail(f"server ignored signal {sig!r} and had to be killed")

            self.assertEqual(proc.returncode, 0, out)
            self.assertIn("SESSION ENDED", out)

            sessions = os.listdir(os.path.join(data_dir, "sessions"))
            log = os.path.join(data_dir, "sessions", sessions[0], "session.jsonl")
            with open(log, encoding="utf-8") as f:
                entries = [json.loads(line) for line in f if line.strip()]
            self.assertEqual(entries[-1]["type"], "session_end",
                             "session log was not finalized")
            return entries[-1]["reason"]
        finally:
            if proc.poll() is None:
                proc.kill()

    def test_ctrl_c(self):
        self.assertEqual(self._run_and_signal(signal.SIGINT, 8991), "ctrl_c")

    def test_window_closed(self):
        self.assertEqual(self._run_and_signal(signal.SIGHUP, 8992), "window_closed")

    def test_terminated(self):
        self.assertEqual(self._run_and_signal(signal.SIGTERM, 8993), "terminated")


@unittest.skipUnless(HAS_DUCKDB, "duckdb not installed")
class TestShutdownWithQueryInFlight(unittest.TestCase):
    """Ctrl+C must not wait out a heavy query the candidate just launched."""

    def test_ctrl_c_cancels_running_query_fast_and_logs_it(self):
        port = 8994
        data_dir = tempfile.mkdtemp(prefix="qsi_inflight_")
        env = dict(os.environ, DATA_ANALYTICS_LIVECODING_DATA_DIR=data_dir)
        proc = subprocess.Popen(
            [sys.executable, SERVE, "--no-tunnel", "--port", str(port)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env,
            start_new_session=True)
        try:
            token = _wait_for_token(self, proc, data_dir)
            heavy = ("SELECT count(*) FROM range(30000000000) "
                     "WHERE range % 7 = 3")
            threading.Thread(
                target=_post_run, args=(port, token, "exercise_01", heavy),
                daemon=True).start()
            time.sleep(2.5)  # let the query actually start

            t0 = time.monotonic()
            proc.send_signal(signal.SIGINT)
            try:
                out = proc.communicate(timeout=15)[0]
            except subprocess.TimeoutExpired:
                proc.kill()
                self.fail("Ctrl+C did not stop the server while a query ran")
            elapsed = time.monotonic() - t0

            self.assertLess(elapsed, 12, "shutdown waited out the query timeout")
            self.assertIn("SESSION ENDED", out)

            sessions = os.listdir(os.path.join(data_dir, "sessions"))
            log = os.path.join(data_dir, "sessions", sessions[0], "session.jsonl")
            with open(log, encoding="utf-8") as f:
                entries = [json.loads(line) for line in f if line.strip()]
            runs = [e for e in entries if e["type"] == "run"]
            self.assertTrue(runs, "the in-flight run was lost from the log")
            self.assertEqual(runs[-1]["status"], "cancelled")
            self.assertEqual(entries[-1]["type"], "session_end")
        finally:
            if proc.poll() is None:
                proc.kill()
            shutil.rmtree(data_dir, ignore_errors=True)


@unittest.skipUnless(HAS_DUCKDB, "duckdb not installed")
class TestCandidateAndChecksEndToEnd(unittest.TestCase):
    """The candidate's name reaches the folder, the banner and the log, and the
    result column comes up for every seed exercise."""

    def test_named_session(self):
        name = "Mar\u00eda P\u00e9rez"
        data_dir = tempfile.mkdtemp(prefix="qsi_named_")
        env = dict(os.environ, DATA_ANALYTICS_LIVECODING_DATA_DIR=data_dir)
        proc = subprocess.Popen(
            [sys.executable, SERVE, "--no-tunnel", "--port", "8996",
             "--candidate", name],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env,
            start_new_session=True)
        try:
            token = _wait_for_token(self, proc, data_dir)
            self.assertTrue(token)
            _post_run(8996, token, "exercise_01",
                      "select count(*) from orders where status='completed'")
            time.sleep(0.5)

            proc.send_signal(signal.SIGINT)
            try:
                out = proc.communicate(timeout=20)[0]
            except subprocess.TimeoutExpired:
                proc.kill()
                self.fail("the named session ignored Ctrl+C")
            self.assertEqual(proc.returncode, 0, out)
            self.assertNotIn("Traceback", out)

            sessions = os.listdir(os.path.join(data_dir, "sessions"))
            self.assertEqual(len(sessions), 1, sessions)
            self.assertTrue(sessions[0].endswith("_maria-perez"), sessions[0])

            # the banner and the farewell both name the candidate
            self.assertIn(name, out)
            self.assertIn("result column on for 5/5 exercises", out)
            # the feed shows the verdict and the style flags of that one run
            self.assertIn("PASS", out)

            log = os.path.join(data_dir, "sessions", sessions[0], "session.jsonl")
            with open(log, encoding="utf-8") as f:
                entries = [json.loads(line) for line in f if line.strip()]
            start = entries[0]
            self.assertEqual(start["type"], "session_start")
            self.assertEqual(start["candidate"], name)
            self.assertEqual(start["log_version"], 2)
            self.assertEqual(set(start["checks"].values()), {True})

            runs = [e for e in entries if e["type"] == "run"]
            self.assertEqual(runs[-1]["check"]["verdict"], "PASS")
            self.assertIn("flags", runs[-1]["style"])
            self.assertEqual(entries[-1]["type"], "session_end")
        finally:
            if proc.poll() is None:
                proc.kill()
            shutil.rmtree(data_dir, ignore_errors=True)


@unittest.skipUnless(HAS_DUCKDB, "duckdb not installed")
class TestShutdownDuringStartup(unittest.TestCase):
    """A signal during the tunnel wait must not escape as a traceback."""

    def test_ctrl_c_while_tunnel_starting(self):
        stub_dir = tempfile.mkdtemp(prefix="qsi_stub_")
        stub = os.path.join(stub_dir, "cloudflared")
        with open(stub, "w") as f:
            f.write("#!/bin/sh\nsleep 300\n")  # never prints a URL
        os.chmod(stub, 0o755)
        data_dir = tempfile.mkdtemp(prefix="qsi_startup_")
        env = dict(os.environ,
                   PATH=stub_dir + os.pathsep + os.environ["PATH"],
                   DATA_ANALYTICS_LIVECODING_DATA_DIR=data_dir)
        proc = subprocess.Popen(
            [sys.executable, SERVE, "--port", "8995"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env,
            start_new_session=True)
        try:
            deadline = time.time() + 30
            while time.time() < deadline:
                if os.path.isdir(os.path.join(data_dir, "sessions")):
                    break
                time.sleep(0.2)
            time.sleep(2)  # now inside the tunnel wait

            t0 = time.monotonic()
            proc.send_signal(signal.SIGINT)
            try:
                out = proc.communicate(timeout=15)[0]
            except subprocess.TimeoutExpired:
                proc.kill()
                self.fail("Ctrl+C was swallowed by the tunnel wait")
            elapsed = time.monotonic() - t0

            self.assertLess(elapsed, 10, "shutdown waited out the tunnel timeout")
            self.assertEqual(proc.returncode, 0, out)
            self.assertNotIn("Traceback", out)
            sessions_dir = os.path.join(data_dir, "sessions")
            self.assertEqual(os.listdir(sessions_dir), [],
                             "an empty session directory was left behind")
        finally:
            if proc.poll() is None:
                proc.kill()
            shutil.rmtree(data_dir, ignore_errors=True)
            shutil.rmtree(stub_dir, ignore_errors=True)


def _wait_for_token(test, proc, data_dir, timeout_s=30):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        sessions = os.path.join(data_dir, "sessions")
        if os.path.isdir(sessions):
            for name in os.listdir(sessions):
                log = os.path.join(sessions, name, "session.jsonl")
                if not os.path.isfile(log):
                    continue
                with open(log, encoding="utf-8") as f:
                    for line in f:
                        entry = json.loads(line)
                        if entry.get("type") == "session_start":
                            return entry["url"].split("t=")[1]
        if proc.poll() is not None:
            test.fail(f"server exited early: {proc.communicate()[0]}")
        time.sleep(0.2)
    test.fail("server never logged session_start")


def _post_run(port, token, exercise, sql):
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
        conn.request("POST", f"/api/run?t={token}",
                     body=json.dumps({"exercise": exercise, "sql": sql}),
                     headers={"Content-Type": "application/json"})
        conn.getresponse().read()
    except Exception:
        pass  # the server is being shut down under us; that is the point


if __name__ == "__main__":
    unittest.main()
