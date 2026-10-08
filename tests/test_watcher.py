"""Idle exit and the per-recipient watcher lease free an unused Lead watcher."""
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_relay import inbox, watcher


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class IdleExitTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="watcher-test-", dir=Path(__file__).parent)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "mail"

    def run_watch(self, scans, **kwargs):
        """Each scan advances a synthetic clock by 60 seconds."""
        clock = Clock(0.0)
        count = []

        def before_scan():
            count.append(1)
            clock.now = 60.0 * (len(count) - 1)
            if len(count) > scans:
                raise AssertionError("watcher did not stop")

        output = io.StringIO()
        with contextlib.redirect_stdout(output), \
                patch.object(inbox.time, "monotonic", side_effect=lambda: clock.now), \
                patch.object(inbox.time, "sleep"):
            reason = inbox.watch(self.root, "claude:lead", before_scan=before_scan, **kwargs)
        return reason, len(count), output.getvalue()

    def test_stops_after_idle_period_without_open_work(self):
        reason, scans, output = self.run_watch(30, idle_exit=1200, is_active=lambda: False)
        self.assertEqual(reason, "idle")
        self.assertEqual(scans, 21)  # 0..1200 seconds in 60-second steps
        self.assertEqual(output, "")

    def test_open_requests_keep_the_watcher_alive(self):
        states = iter([True] * 10 + [False] * 100)
        reason, scans, _ = self.run_watch(60, idle_exit=1200, is_active=lambda: next(states))
        self.assertEqual(reason, "idle")
        self.assertEqual(scans, 30)  # last active at scan 10, then 20 idle minutes

    def test_new_mail_restarts_the_idle_period(self):
        sent = []
        calls = []

        def is_active():
            calls.append(1)
            if len(calls) == 5:
                sent.append(inbox.send(self.root, "probe", "codex:worker", "result", "done", "claude:lead"))
            return False

        reason, scans, output = self.run_watch(60, idle_exit=1200, is_active=is_active)
        self.assertEqual(reason, "idle")
        self.assertEqual(scans, 26)  # mail printed on scan 6
        self.assertIn(sent[0]["id"], output)

    def test_zero_idle_exit_keeps_previous_behavior(self):
        reason, scans, _ = self.run_watch(10, timeout=300, is_active=lambda: False)
        self.assertEqual(reason, "timeout")
        self.assertEqual(scans, 6)

    def test_newer_watcher_supersedes(self):
        beats = iter([True, True, False])
        reason, scans, _ = self.run_watch(10, heartbeat=lambda: next(beats))
        self.assertEqual((reason, scans), ("superseded", 3))

    def test_rejects_invalid_idle_exit(self):
        for value in (-1, float("inf"), "20"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                inbox.watch(self.root, "claude:lead", once=True, idle_exit=value)


class LeaseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="lease-test-", dir=Path(__file__).parent)
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name) / "project"
        self.clock = Clock()

    def lease(self, pid):
        with patch.object(watcher.os, "getpid", return_value=pid):
            return watcher.Lease(self.project, "claude:lead-session", clock=self.clock)

    def read(self):
        return watcher._read(watcher.lease_path(self.project, "claude:lead-session"))

    def test_beat_publishes_a_fresh_record_and_release_removes_it(self):
        lease = self.lease(10)
        self.assertTrue(lease.beat())
        record = self.read()
        self.assertEqual(record["pid"], 10)
        self.assertTrue(watcher.is_fresh(record, "claude:lead-session", now=self.clock.now))
        self.assertFalse(watcher.is_fresh(record, "claude:other", now=self.clock.now))
        self.assertFalse(watcher.is_fresh(record, "claude:lead-session",
                                          now=self.clock.now + watcher.STALE_SECONDS))
        lease.release()
        self.assertIsNone(self.read())

    def test_newest_live_watcher_owns_the_lease(self):
        old = self.lease(10)
        self.assertTrue(old.beat())
        self.clock.now += 1
        new = self.lease(11)
        self.assertTrue(new.beat())
        self.clock.now += watcher.HEARTBEAT_SECONDS
        self.assertFalse(old.beat())
        old.release()  # never removes the newer owner's lease
        self.assertEqual(self.read()["pid"], 11)
        self.assertTrue(new.beat())

    def test_stale_newer_lease_does_not_stop_a_live_watcher(self):
        old = self.lease(10)
        self.clock.now += 1
        new = self.lease(11)
        self.assertTrue(new.beat())
        self.clock.now += watcher.STALE_SECONDS + 1
        self.assertTrue(old.beat())
        self.assertEqual(self.read()["pid"], 10)

    def test_heartbeat_writes_are_throttled(self):
        lease = self.lease(10)
        lease.beat()
        first = self.read()["heartbeat"]
        self.clock.now += watcher.HEARTBEAT_SECONDS / 2
        self.assertTrue(lease.beat())
        self.assertEqual(self.read()["heartbeat"], first)
        self.clock.now += watcher.HEARTBEAT_SECONDS
        lease.beat()
        self.assertEqual(self.read()["heartbeat"], self.clock.now)


if __name__ == "__main__":
    unittest.main()
