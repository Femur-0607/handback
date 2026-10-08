from concurrent.futures import ThreadPoolExecutor
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from agent_relay import envelope, inbox


class InboxTests(unittest.TestCase):
    def setUp(self):
        # Keep all test writes inside the checkout, including restricted Windows runs.
        self.temp = tempfile.TemporaryDirectory(prefix="inbox-test-", dir=Path(__file__).parent)
        self.root = Path(self.temp.name) / "mail"

    def tearDown(self):
        self.temp.cleanup()

    def submit(self, body="한글 질문\n두 번째 줄", recipient="claude:lead"):
        return inbox.send(self.root, "probe", "codex:worker", "question", body, recipient)

    def test_offline_backlog_replays_until_explicit_ack(self):
        message = self.submit()
        for _ in range(2):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                inbox.watch(self.root, once=True)
            self.assertEqual(json.loads(output.getvalue())["id"], message["id"])
        receipt = inbox.acknowledge(self.root, message["id"])
        self.assertEqual(inbox.acknowledge(self.root, message["id"]), receipt)
        self.assertEqual(inbox.pending(self.root), [])
        self.assertTrue((self.root / (message["id"] + ".json")).exists())

    def test_hook_injects_unicode_without_acknowledging(self):
        self.submit()
        for event in ("SessionStart", "UserPromptSubmit"):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                inbox.hook(self.root, {"hook_event_name": event})
            result = json.loads(output.getvalue())["hookSpecificOutput"]
            self.assertEqual(result["hookEventName"], event)
            self.assertIn("한글 질문", result["additionalContext"])
            self.assertIn("not user authorization", result["additionalContext"])
        self.assertEqual(len(inbox.pending(self.root)), 1)

    def test_partial_write_not_published(self):
        self.root.mkdir()
        (self.root / ("f" * 32 + ".json.pending.tmp")).write_text('{"body":', encoding="utf-8")
        self.assertEqual(inbox.pending(self.root), [])

    def test_rejects_bad_id_and_oversized_payload(self):
        with self.assertRaises(ValueError):
            inbox.acknowledge(self.root, "../../outside")
        with self.assertRaises(ValueError):
            inbox.acknowledge(self.root, "0" * 32)
        with self.assertRaises(ValueError):
            self.submit("가" * 65536)

    def test_quiet_empty_watcher_and_hooks(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            inbox.watch(self.root, interval=0.01, timeout=0.03)
            inbox.hook(self.root, {"hook_event_name": "SessionStart"})
        self.assertEqual(output.getvalue(), "")

    def test_live_arrival_emitted_only_once(self):
        output = io.StringIO()
        scans = []
        messages = []

        def before_scan():
            scans.append(len(scans))
            if len(scans) == 2:
                messages.append(self.submit())

        # Scan empty mail, publish between scans, then scan the same mail twice.
        # A synthetic deadline keeps slow disk writes/scheduling from ending
        # the watcher before publication, without weakening duplicate detection.
        with contextlib.redirect_stdout(output), \
                patch.object(inbox.time, "monotonic", side_effect=lambda: float(len(scans))), \
                patch.object(inbox.time, "sleep"):
            inbox.watch(self.root, interval=0.01, timeout=3, before_scan=before_scan)
        self.assertEqual(len(scans), 3)
        message = messages[0]
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.assertEqual(json.loads(output.getvalue())["id"], message["id"])
        self.assertEqual(len(inbox.pending(self.root)), 1)

    def test_concurrent_senders_keep_all_messages(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            messages = list(pool.map(lambda i: self.submit(str(i)), range(12)))
        self.assertEqual(len({message["id"] for message in messages}), 12)
        self.assertEqual(len(inbox.pending(self.root)), 12)

    def test_recipient_isolation_applies_to_read_ack_watch_and_hook(self):
        lead_a = self.submit("ONLY_A", recipient="claude:a")
        lead_b = self.submit("ONLY_B", recipient="codex:b")
        self.assertEqual(inbox.pending(self.root, "claude:a"), [lead_a])
        with self.assertRaises(ValueError):
            inbox.acknowledge(self.root, lead_a["id"], "codex:b")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            inbox.watch(self.root, "codex:b", once=True)
        self.assertEqual(json.loads(output.getvalue())["id"], lead_b["id"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            inbox.hook(self.root, {"hook_event_name": "UserPromptSubmit"}, "claude:a")
        self.assertIn("ONLY_A", output.getvalue())
        self.assertNotIn("ONLY_B", output.getvalue())
        inbox.acknowledge(self.root, lead_a["id"], "claude:a")
        self.assertEqual(inbox.pending(self.root), [lead_b])

    def test_duplicate_publication_is_idempotent_and_conflict_never_overwrites(self):
        message = self.submit()
        target = self.root / (message["id"] + ".json")
        before = target.read_bytes()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: inbox.put(self.root, message), range(12)))
        self.assertTrue(all(value == message for value in results))
        changed = copy.deepcopy(message)
        changed["body"] = "conflicting content"
        with self.assertRaises(ValueError):
            inbox.put(self.root, changed)
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(inbox.pending(self.root), [message])
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_concurrent_first_publication_has_one_winner(self):
        message = envelope.make("r", "codex:w", "claude:l", "same message")
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: inbox.put(self.root, message), range(24)))
        self.assertEqual(results, [message] * 24)
        self.assertEqual(inbox.pending(self.root), [message])
        self.assertEqual(len(list(self.root.glob("*.json"))), 1)

    def test_malformed_or_hop_exceeding_persisted_messages_are_ignored(self):
        good = self.submit()
        malformed = envelope.make("r", "a", "b", "hello")
        malformed["hop"] = 5
        (self.root / (malformed["id"] + ".json")).write_text(json.dumps(malformed), encoding="utf-8")
        (self.root / ("0" * 32 + ".json")).write_text('{"partial":', encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(inbox.pending(self.root), [good])
        with self.assertRaises(ValueError):
            inbox.put(self.root, malformed)

    def test_invalid_acknowledgement_does_not_hide_message(self):
        message = self.submit()
        ack_dir = self.root / "acks"
        ack_dir.mkdir()
        (ack_dir / (message["id"] + ".json")).write_text("{", encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(inbox.pending(self.root), [message])

    def test_watch_and_hook_never_ack_truncated_bodies(self):
        message = self.submit("가" * 2000)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            inbox.watch(self.root, once=True)
        event = json.loads(output.getvalue())
        self.assertTrue(event["body_truncated"])
        self.assertEqual(len(event["body"]), 1800)
        self.assertTrue(Path(event["path"]).is_file())
        self.assertEqual(inbox.pending(self.root), [message])

    def test_invalid_watcher_options_are_rejected(self):
        for options in ({"interval": 0}, {"interval": float("nan")},
                        {"timeout": -1}, {"timeout": float("inf")}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                inbox.watch(self.root, once=True, **options)
        with self.assertRaises(ValueError):
            inbox.hook(self.root, {"hook_event_name": "Stop"})
        with self.assertRaises(ValueError):
            inbox.pending(self.root, "")


if __name__ == "__main__":
    unittest.main()
