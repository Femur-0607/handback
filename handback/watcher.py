"""Liveness lease for one recipient's ``inbox watch``.

The watcher refreshes a small per-recipient record so recovery hooks can tell
whether a Lead still has a live watcher. The newest watcher owns the lease; an
older one for the same recipient (for example, left behind by an expired
Monitor) exits on its next heartbeat instead of holding memory.
"""
import hashlib
import json
import os
from pathlib import Path
import time

from .state import atomic_json


LEASE_DIR = "watchers"
HEARTBEAT_SECONDS = 5.0
# Hooks treat a lease older than this as a dead watcher.
STALE_SECONDS = 30.0
DEFAULT_IDLE_EXIT = 20 * 60


def lease_path(project_path, recipient):
    # Recipients contain ':', which Windows forbids in file names.
    digest = hashlib.sha256(recipient.encode("utf-8")).hexdigest()[:32]
    return Path(project_path) / LEASE_DIR / (digest + ".json")


def is_fresh(record, recipient, now=None):
    if not isinstance(record, dict) or record.get("recipient") != recipient:
        return False
    heartbeat = record.get("heartbeat")
    if isinstance(heartbeat, bool) or not isinstance(heartbeat, (int, float)):
        return False
    age = (time.time() if now is None else now) - heartbeat
    return -STALE_SECONDS < age < STALE_SECONDS


def _read(path):
    try:
        with path.open("rb") as stream:
            return json.loads(stream.read(64 * 1024).decode("utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None


class Lease:
    def __init__(self, project_path, recipient, clock=time.time):
        self.path = lease_path(project_path, recipient)
        self.recipient = recipient
        self.clock = clock
        self.pid = os.getpid()
        self.started = clock()
        self.next_beat = 0.0

    def _mine(self, record):
        return (isinstance(record, dict) and record.get("pid") == self.pid
                and record.get("started") == self.started)

    def beat(self):
        """Refresh the lease; return False once a newer live watcher owns it."""
        now = self.clock()
        if now < self.next_beat:
            return True
        self.next_beat = now + HEARTBEAT_SECONDS
        current = _read(self.path)
        if (not self._mine(current) and is_fresh(current, self.recipient, now)
                and isinstance(current.get("started"), (int, float))
                and current["started"] > self.started):
            return False
        try:
            atomic_json(self.path, {"recipient": self.recipient, "pid": self.pid,
                                    "started": self.started, "heartbeat": now})
        except OSError:
            pass  # a missed heartbeat only makes hooks suggest another watcher
        return True

    def release(self):
        if self._mine(_read(self.path)):
            try:
                self.path.unlink()
            except OSError:
                pass
