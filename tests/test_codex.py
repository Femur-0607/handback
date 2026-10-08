"""Regression coverage for byte offsets, turn ownership, and executable discovery."""
import contextlib
import io
import json
import os
from pathlib import Path
import queue
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from agent_relay.adapters import codex
from agent_relay.adapters.base import AdapterError, AdapterUnavailable


def event(kind, **fields):
    return {"type": "event_msg", "payload": {"type": kind, **fields}}


def user(text):
    return {"type": "response_item", "payload": {"type": "message", "role": "user",
                                                 "content": [{"type": "input_text", "text": text}]}}


def encoded(records, newline=b"\r\n"):
    return b"".join(json.dumps(record, ensure_ascii=False).encode("utf-8") + newline for record in records)


class BinaryTailerTests(unittest.TestCase):
    def test_crlf_unicode_and_partial_multibyte_line_keep_exact_offsets(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "rollout.jsonl"
            first = encoded([user("첫째 줄\n한글")])
            last = encoded([event("task_complete", last_agent_message="완료 한글 🐱")])
            split = last.index("한".encode("utf-8")) + 1
            path.write_bytes(first + last[:split])
            tailer = codex.BinaryRolloutTailer(path)
            self.assertEqual(list(tailer.read_records()), [user("첫째 줄\n한글")])
            self.assertEqual(tailer.offset, len(first))
            self.assertEqual(list(tailer.read_records()), [])
            with path.open("ab") as stream:
                stream.write(last[split:])
            self.assertEqual(list(tailer.read_records()), [event("task_complete", last_agent_message="완료 한글 🐱")])
            self.assertEqual(tailer.offset, len(first + last))
            self.assertEqual(list(tailer.read_records()), [])

    def test_truncation_restarts_at_complete_record_boundary(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "rollout.jsonl"
            path.write_bytes(encoded([user("a long old message"), user("another old message")]))
            tailer = codex.BinaryRolloutTailer(path)
            list(tailer.read_records())
            path.write_bytes(encoded([user("new")]))
            self.assertEqual(list(tailer.read_records()), [user("new")])
            self.assertEqual(tailer.generation, 1)

    def test_malformed_complete_line_does_not_hide_valid_following_record(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "rollout.jsonl"
            path.write_bytes(b"\xff\r\n{bad json}\r\n" + encoded([user("valid")]))
            self.assertEqual(list(codex.BinaryRolloutTailer(path).read_records()), [user("valid")])


class ReplyCorrelationTests(unittest.TestCase):
    marker = "[relay unique]"

    def test_assistant_quoting_marker_is_not_user_input(self):
        record = {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                                                         "content": [{"text": self.marker}]}}
        self.assertFalse(codex.carries_marker(record, self.marker))
        self.assertFalse(codex.carries_marker(user("anything"), ""))

    def test_active_turn_is_bound_and_unrelated_completion_is_ignored(self):
        tracker = codex.ReplyTracker(self.marker)
        tracker.feed(event("task_started", turn_id="target"))
        tracker.feed(user(self.marker))
        self.assertIsNone(tracker.feed(event("task_complete", turn_id="other", last_agent_message="wrong")))
        self.assertIsNone(tracker.feed(event("error", turn_id="other", message="wrong error")))
        self.assertEqual(tracker.feed(event("task_complete", turn_id="target", last_agent_message="right")),
                         {"outcome": "completed", "text": "right", "turn": "target"})

    def test_marker_before_task_start_binds_following_turn(self):
        tracker = codex.ReplyTracker(self.marker)
        tracker.feed(user(self.marker))
        tracker.feed(event("task_started", turn_id="target"))
        self.assertEqual(tracker.feed(event("task_complete", turn_id="target", last_agent_message="right"))["turn"],
                         "target")

    def test_duplicate_user_event_for_marked_prompt_is_allowed(self):
        tracker = codex.ReplyTracker(self.marker)
        tracker.feed(event("task_started", turn_id="target"))
        tracker.feed(user(self.marker))
        self.assertIsNone(tracker.feed(event("user_message", message=self.marker)))
        self.assertEqual(tracker.feed(event("task_complete", turn_id="target"))["outcome"], "completed")

    def test_new_user_turn_cannot_be_collected_as_original_request(self):
        tracker = codex.ReplyTracker(self.marker)
        tracker.feed(event("task_started", turn_id="target"))
        tracker.feed(user(self.marker))
        tracker.feed(event("task_started", turn_id="user-turn"))
        result = tracker.feed(user("This is a direct user request"))
        self.assertEqual(result["outcome"], "failed")
        self.assertEqual(result["text"], "")
        self.assertIn("later user", result["error"])

    def test_same_turn_user_reply_does_not_abort_collection(self):
        # Regression (4.1): the app records the user's answer to an in-turn question
        # as a user-role item inside the same turn.
        tracker = codex.ReplyTracker(self.marker)
        tracker.feed(event("task_started", turn_id="target"))
        tracker.feed(user(self.marker))
        self.assertIsNone(tracker.feed(user("<send_user_message_question_reply>[...]</send_user_message_question_reply>")))
        result = tracker.feed(event("task_complete", turn_id="target", last_agent_message="done"))
        self.assertEqual((result["outcome"], result["text"]), ("completed", "done"))

    def test_missing_ids_fallback_still_stops_at_next_user_input(self):
        tracker = codex.ReplyTracker(self.marker)
        tracker.feed(user(self.marker))
        self.assertEqual(tracker.feed(event("user_message", message="a new prompt"))["outcome"], "failed")

    def test_completed_prior_turn_is_not_bound_to_new_marker(self):
        tracker = codex.ReplyTracker(self.marker)
        tracker.feed(event("task_started", turn_id="old"))
        tracker.feed(event("task_complete", turn_id="old"))
        tracker.feed(user(self.marker))
        tracker.feed(event("task_started", turn_id="new"))
        self.assertEqual(tracker.feed(event("task_complete", turn_id="new"))["turn"], "new")

    def test_wait_reply_returns_legacy_codes_and_utf8_file(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory, mock.patch.dict(os.environ, {"CODEX_HOME": directory}):
            path = Path(directory) / "sessions/2026/rollout-example-thread.jsonl"
            path.parent.mkdir(parents=True)
            path.write_bytes(encoded([event("task_started", turn_id="t"), user(self.marker),
                                      event("task_complete", turn_id="t", last_agent_message="한글 완료 🐱")]))
            output_file = Path(directory) / "reply.txt"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(codex.wait_reply("thread", self.marker, output_file, 1), 0)
            self.assertEqual(output_file.read_text(encoding="utf-8"), "한글 완료 🐱")
            path.write_bytes(encoded([user(self.marker), event("turn_aborted", reason="cancelled")]))
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(codex.wait_reply("thread", self.marker, None, 1), 2)
                self.assertEqual(codex.wait_reply("missing-thread", self.marker, None, 0.01), 3)


class DiscoveryTests(unittest.TestCase):
    def test_userprofile_bundle_is_found_when_localappdata_is_sandboxed(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            profile = Path(directory)
            bundled = profile / "AppData/Local/OpenAI/Codex/bin/hash/codex.exe"
            bundled.parent.mkdir(parents=True)
            bundled.touch()
            environment = {"LOCALAPPDATA": str(profile / "sandbox/AC"), "USERPROFILE": str(profile)}
            with mock.patch.dict(os.environ, environment, clear=True), mock.patch.object(codex.Path, "home", return_value=profile), \
                    mock.patch.object(codex, "_version", return_value="codex-cli test"), mock.patch.object(codex.shutil, "which", return_value=None):
                self.assertEqual(Path(codex.resolve_codex()), bundled)

    def test_explicit_argument_precedes_environment_and_invalid_override_does_not_fallback(self):
        with mock.patch.dict(os.environ, {"AGENT_RELAY_CODEX": "env-codex"}), \
                mock.patch.object(codex.shutil, "which", side_effect=lambda value: value), \
                mock.patch.object(codex, "_version", return_value="version") as version:
            self.assertEqual(codex.resolve_codex("argument-codex"), "argument-codex")
            version.assert_called_once_with("argument-codex")
        with mock.patch.dict(os.environ, {"AGENT_RELAY_CODEX": "broken"}), \
                mock.patch.object(codex.shutil, "which", return_value=None), mock.patch.object(codex, "_version", return_value=""), \
                mock.patch.object(codex, "_bundled_candidates") as candidates:
            with self.assertRaises(AdapterUnavailable):
                codex.resolve_codex()
            candidates.assert_not_called()

    def test_nonworking_bundle_falls_through_to_path(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(codex, "_bundled_candidates", return_value=["broken"]), \
                mock.patch.object(codex, "_version", side_effect=lambda value: "v" if value == "path-codex" else ""), \
                mock.patch.object(codex.shutil, "which", return_value="path-codex"):
            self.assertEqual(codex.resolve_codex(), "path-codex")


class AppServerAndDeliveryTests(unittest.TestCase):
    def test_missing_rpc_reply_has_deadline(self):
        # A blocked readline lives on the reader thread; call() waits on a bounded queue.
        server = codex.AppServer.__new__(codex.AppServer)
        server.proc = mock.Mock(stdin=io.StringIO())
        server._responses = queue.Queue()
        server.timeout = 0.01
        server.next_id = 0
        start = time.monotonic()
        with self.assertRaisesRegex(AdapterError, "timed out"):
            server.call("thread/start", {})
        self.assertLess(time.monotonic() - start, 1)

    def test_rpc_ignores_other_ids_and_reports_error(self):
        server = codex.AppServer.__new__(codex.AppServer)
        server.proc = mock.Mock(stdin=io.StringIO())
        server._responses = queue.Queue()
        server.timeout = 0.1
        server.next_id = 0
        server._responses.put({"id": 99, "result": {}})
        server._responses.put({"id": 1, "error": {"message": "invalid sandbox"}})
        with self.assertRaisesRegex(AdapterError, "invalid sandbox"):
            server.call("thread/start", {})

    def test_queue_timeout_is_ambiguous_not_safe_to_retry(self):
        with mock.patch.object(codex, "resolve_codex", return_value="codex"), \
                mock.patch.object(codex.subprocess, "run", side_effect=subprocess.TimeoutExpired("codex", 30)):
            result = codex.CodexAdapter().deliver("codex:thread", {"body": "[relay abc] message"})
        self.assertFalse(result["accepted"])
        self.assertTrue(result["unknown"])

    def test_deliver_preserves_caller_marker_and_strips_handle_prefix(self):
        body = "[relay abc] 한글 request"
        with mock.patch.object(codex, "resolve_codex", return_value="codex"), \
                mock.patch.object(codex.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "queued", "")) as run:
            result = codex.CodexAdapter().deliver("codex:thread", {"body": body})
        self.assertTrue(result["accepted"])
        self.assertEqual(run.call_args.args[0], ["codex", "queue", "--thread", "thread", "--message", body])


if __name__ == "__main__":
    unittest.main()
