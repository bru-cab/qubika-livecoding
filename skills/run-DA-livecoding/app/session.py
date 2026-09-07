"""Session state: token, TTL, per-exercise editor text, run history, JSONL log."""
import hmac
import json
import os
import secrets
import shutil
import threading
import time


class Session:
    def __init__(self, exercise_names, ttl_min, session_dir):
        self.token = secrets.token_urlsafe(16)
        self.created_at = time.time()
        self.expires_at = self.created_at + ttl_min * 60
        self.ttl_min = ttl_min
        self.shutdown = threading.Event()
        self.shutdown_reason = None  # set by whoever requests the shutdown
        self.exercise_names = list(exercise_names)
        self.editor_texts = {name: "" for name in exercise_names}
        self.runs = []
        self.run_lock = threading.Lock()
        self.last_run_ts = 0.0
        self.session_dir = session_dir
        os.makedirs(session_dir, exist_ok=True)
        self.log_path = os.path.join(session_dir, "session.jsonl")
        self._lock = threading.Lock()
        self._log_file = open(self.log_path, "a", encoding="utf-8")
        self._ended = False

    def check_token(self, token):
        # Compare as bytes: str compare_digest raises TypeError on non-ASCII
        # input, and the token arrives percent-decoded from the URL.
        if not isinstance(token, str) or not token:
            return False
        return hmac.compare_digest(token.encode("utf-8"),
                                   self.token.encode("utf-8"))

    def expired(self):
        return time.time() >= self.expires_at

    def active(self):
        return not self.expired() and not self.shutdown.is_set()

    def set_editor_text(self, exercise, text):
        with self._lock:
            if exercise not in self.editor_texts:
                return False
            self.editor_texts[exercise] = text
            return True

    def editor_snapshot(self):
        with self._lock:
            return dict(self.editor_texts)

    def record_run(self, entry):
        with self._lock:
            self.runs.append(entry)
        self.log(dict(entry, type="run"))

    def log(self, obj):
        line = json.dumps(obj, ensure_ascii=False)
        with self._lock:
            if self._log_file.closed:
                return
            self._log_file.write(line + "\n")
            self._log_file.flush()

    def discard(self):
        """Drop a session that never started: close the log, remove its dir."""
        with self._lock:
            self._ended = True
            if not self._log_file.closed:
                self._log_file.close()
        shutil.rmtree(self.session_dir, ignore_errors=True)
        self.shutdown.set()

    def end(self, reason):
        with self._lock:
            if self._ended:
                return
            self._ended = True
        self.log({"type": "editor_final", "ts": time.time(),
                  "exercises": self.editor_snapshot()})
        self.log({"type": "session_end", "ts": time.time(), "reason": reason})
        with self._lock:
            self._log_file.close()
        self.shutdown.set()
