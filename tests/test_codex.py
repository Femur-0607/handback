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

from handback.adapters import codex
from handback.adapters.base import AdapterError, AdapterUnavailable


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
        with mock.patch.dict(os.environ, {"HANDBACK_CODEX": "env-codex"}), \
                mock.patch.object(codex.shutil, "which", side_effect=lambda value: value), \
                mock.patch.object(codex, "_version", return_value="version") as version:
            self.assertEqual(codex.resolve_codex("argument-codex"), "argument-codex")
            version.assert_called_once_with("argument-codex")
        with mock.patch.dict(os.environ, {"HANDBACK_CODEX": "broken"}), \
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
    def server(self, config=None, thread=None, models=None):
        server = mock.Mock()
        configuration = {"model": "model-a", "model_reasoning_effort": "ultra"} if config is None else config
        catalog = models if models is not None else [
            {"id": "model-a", "model": "model-a", "isDefault": True, "defaultReasoningEffort": "low",
             "supportedReasoningEfforts": [{"reasoningEffort": value} for value in ("low", "medium", "ultra")]},
            {"id": "model-b", "model": "model-b", "isDefault": False, "defaultReasoningEffort": "medium",
             "supportedReasoningEfforts": [{"reasoningEffort": value} for value in ("low", "medium")]},
        ]
        existing = {"id": "thread", "cwd": os.path.abspath("project")}
        existing.update(thread or {})

        def call(method, params):
            if method == "config/read":
                return {"config": configuration}
            if method == "model/list":
                return {"data": catalog, "nextCursor": None}
            if method == "thread/read":
                return {"thread": existing}
            if method == "thread/start":
                return {"thread": {"id": "thread"}, "model": params["model"],
                        "reasoningEffort": params["config"]["model_reasoning_effort"]}
            if method == "thread/name/set":
                return {}
            if method == "thread/settings/update":
                existing.update(model=params["model"], reasoningEffort=params["effort"])
                return {}
            if method == "thread/resume":
                return {"thread": existing}
            raise AssertionError(method)
        server.call.side_effect = call
        return server

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
                mock.patch.object(codex, "AppServer", return_value=self.server()), \
                mock.patch.object(codex.subprocess, "run", side_effect=subprocess.TimeoutExpired("codex", 30)):
            result = codex.CodexAdapter().deliver("codex:thread", {"body": "[relay abc] message"})
        self.assertFalse(result["accepted"])
        self.assertTrue(result["unknown"])

    def test_deliver_preserves_caller_marker_and_strips_handle_prefix(self):
        body = "[relay abc] 한글 request"
        with mock.patch.object(codex, "resolve_codex", return_value="codex"), \
                mock.patch.object(codex, "AppServer", return_value=self.server()), \
                mock.patch.object(codex.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "queued", "")) as run:
            result = codex.CodexAdapter().deliver("codex:thread", {"body": body})
        self.assertTrue(result["accepted"])
        self.assertEqual(run.call_args.args[0], ["codex", "queue", "--thread", "thread", "--message", body])

    def test_explicit_settings_survive_creation_first_and_later_queue(self):
        server = self.server(config={"model": "model-b", "model_reasoning_effort": "medium"})
        adapter = codex.CodexAdapter(model="model-a", reasoning_effort="ultra")
        with mock.patch.object(codex, "resolve_codex", return_value="codex"), \
                mock.patch.object(codex, "AppServer", return_value=server) as create_server, \
                mock.patch.object(codex.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "queued", "")) as run:
            thread = adapter.new_thread("project", "worker", open_app=False, writable_roots=["state"])
            self.assertTrue(adapter.deliver(thread, {"body": "[relay first] brief"})["accepted"])
            self.assertTrue(adapter.deliver(thread, {"body": "[relay later] correction"})["accepted"])
        create_server.assert_called_once()
        start = next(call.args[1] for call in server.call.call_args_list if call.args[0] == "thread/start")
        self.assertEqual(start["model"], "model-a")
        self.assertEqual(start["config"], {"model_reasoning_effort": "ultra", "sandbox_workspace_write": {
            "writable_roots": [os.path.abspath("state")]}})
        self.assertEqual(start["sandbox"], "workspace-write")
        self.assertEqual(start["approvalPolicy"], "never")
        server.call.assert_any_call("thread/settings/update", {"threadId": "thread", "model": "model-a", "effort": "ultra"})
        self.assertEqual([call.args[0] for call in run.call_args_list], [
            ["codex", "queue", "--thread", "thread", "--message", "[relay first] brief"],
            ["codex", "queue", "--thread", "thread", "--message", "[relay later] correction"],
        ])
        self.assertEqual(adapter.execution_settings, {"model": "model-a", "reasoning_effort": "ultra"})
        server.close.assert_called_once()

    def test_creation_inherits_effective_project_config_and_records_settings(self):
        server = self.server()
        adapter = codex.CodexAdapter()
        with mock.patch.object(codex, "resolve_codex", return_value="codex"), \
                mock.patch.object(codex, "AppServer", return_value=server):
            adapter.new_thread("project", "lead", open_app=False)
        server.call.assert_any_call("config/read", {"cwd": os.path.abspath("project"), "includeLayers": False})
        start = next(call.args[1] for call in server.call.call_args_list if call.args[0] == "thread/start")
        self.assertEqual(start["model"], "model-a")
        self.assertEqual(start["config"]["model_reasoning_effort"], "ultra")
        self.assertEqual(adapter.execution_settings, {"model": "model-a", "reasoning_effort": "ultra"})

    def test_missing_config_uses_catalog_default_instead_of_hardcoded_medium(self):
        server = self.server(config={})
        adapter = codex.CodexAdapter()
        with mock.patch.object(codex, "resolve_codex", return_value="codex"), \
                mock.patch.object(codex, "AppServer", return_value=server):
            adapter.new_thread("project", "worker", open_app=False)
        self.assertEqual(adapter.execution_settings, {"model": "model-a", "reasoning_effort": "low"})

    def test_existing_thread_settings_override_changed_user_config(self):
        server = self.server(config={"model": "model-b", "model_reasoning_effort": "medium"},
                             thread={"model": "model-a", "reasoningEffort": "ultra"})
        with mock.patch.object(codex, "resolve_codex", return_value="codex"), \
                mock.patch.object(codex, "AppServer", return_value=server), \
                mock.patch.object(codex.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "queued", "")) as run:
            adapter = codex.CodexAdapter()
            self.assertTrue(adapter.deliver("thread", {"body": "[relay later] correction"})["accepted"])
        server.call.assert_any_call("thread/read", {"threadId": "thread", "includeTurns": False})
        server.call.assert_any_call("config/read", {"cwd": os.path.abspath("project"), "includeLayers": False})
        self.assertEqual(adapter.execution_settings, {"model": "model-a", "reasoning_effort": "ultra"})
        self.assertFalse(any(call.args[0] == "thread/resume" for call in server.call.call_args_list))

    def test_explicit_delivery_overrides_native_thread_settings(self):
        server = self.server(thread={"model": "model-b", "reasoningEffort": "medium"})
        with mock.patch.object(codex, "resolve_codex", return_value="codex"), \
                mock.patch.object(codex, "AppServer", return_value=server), \
                mock.patch.object(codex.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "queued", "")) as run:
            adapter = codex.CodexAdapter(model="model-a", reasoning_effort="ultra")
            self.assertTrue(adapter.deliver("thread", {"body": "[relay next] correction"})["accepted"])
        server.call.assert_any_call("thread/resume", {"threadId": "thread", "excludeTurns": True})
        server.call.assert_any_call("thread/settings/update", {"threadId": "thread", "model": "model-a", "effort": "ultra"})
        self.assertEqual(run.call_args.args[0], ["codex", "queue", "--thread", "thread", "--message", "[relay next] correction"])

    def test_catalog_efforts_are_dynamic(self):
        catalog = [{"id": "future-model", "model": "future-model", "isDefault": True,
                    "defaultReasoningEffort": "future-effort",
                    "supportedReasoningEfforts": [{"reasoningEffort": "future-effort"}]}]
        server = self.server(config={}, models=catalog)
        adapter = codex.CodexAdapter(model="future-model", reasoning_effort="future-effort")
        with mock.patch.object(codex, "resolve_codex", return_value="codex"), \
                mock.patch.object(codex, "AppServer", return_value=server):
            adapter.new_thread("project", "worker", open_app=False)
        self.assertEqual(adapter.execution_settings["reasoning_effort"], "future-effort")

    def test_unsupported_settings_fail_before_thread_creation_or_queue(self):
        for model, effort in (("unknown-model", "ultra"), ("model-b", "ultra")):
            with self.subTest(model=model):
                server = self.server()
                adapter = codex.CodexAdapter(model=model, reasoning_effort=effort)
                with mock.patch.object(codex, "resolve_codex", return_value="codex"), \
                        mock.patch.object(codex, "AppServer", return_value=server), \
                        mock.patch.object(codex.subprocess, "run") as run:
                    with self.assertRaises(AdapterError):
                        adapter.new_thread("project", "worker", open_app=False)
                    result = adapter.deliver("thread", {"body": "[relay next] correction"})
                self.assertFalse(result["accepted"])
                self.assertNotIn("unknown", result)
                self.assertFalse(any(call.args[0] == "thread/start" for call in server.call.call_args_list))
                run.assert_not_called()

    def test_config_timeout_is_definite_failure_and_never_queues(self):
        server = self.server()
        original = server.call.side_effect
        server.call.side_effect = lambda method, params: (_ for _ in ()).throw(AdapterError("config/read timed out")) \
            if method == "config/read" else original(method, params)
        with mock.patch.object(codex, "resolve_codex", return_value="codex"), \
                mock.patch.object(codex, "AppServer", return_value=server), \
                mock.patch.object(codex.subprocess, "run") as run:
            result = codex.CodexAdapter().deliver("thread", {"body": "[relay first] brief"})
        self.assertFalse(result["accepted"])
        self.assertNotIn("unknown", result)
        self.assertIn("timed out", result["stderr"])
        server.close.assert_called_once()
        run.assert_not_called()

    def test_invalid_constructor_settings_and_extra_roots_do_not_start_server(self):
        for field in ("model", "reasoning_effort"):
            for value in ("", " ", 5):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    codex.CodexAdapter(**{field: value})
        with mock.patch.object(codex, "AppServer") as server, self.assertRaises(ValueError):
            codex.CodexAdapter().new_thread("project", "worker", "read-only", writable_roots=["state"])
        server.assert_not_called()

    def test_shutdown_waits_for_native_settings_flush_before_termination(self):
        server = codex.AppServer.__new__(codex.AppServer)
        server.proc = mock.Mock()
        server.proc.poll.return_value = None
        server.proc.wait.return_value = 0
        server._reader = mock.Mock()
        self.assertTrue(server.close())
        server.proc.stdin.close.assert_called_once()
        server.proc.wait.assert_called_once_with(timeout=5)
        server.proc.terminate.assert_not_called()
        server.proc.kill.assert_not_called()

    def test_stuck_server_shutdown_remains_bounded(self):
        server = codex.AppServer.__new__(codex.AppServer)
        server.proc = mock.Mock()
        server.proc.poll.return_value = None
        server.proc.wait.side_effect = [subprocess.TimeoutExpired("codex", 5),
                                        subprocess.TimeoutExpired("codex", 5), 0]
        server._reader = mock.Mock()
        self.assertFalse(server.close())
        server.proc.terminate.assert_called_once()
        server.proc.kill.assert_called_once()
        self.assertEqual(server.proc.wait.call_count, 3)

    def test_settings_update_failure_never_queues(self):
        server = self.server(thread={"model": "model-b", "reasoningEffort": "medium"})
        original = server.call.side_effect
        def call(method, params):
            if method == "thread/settings/update":
                raise AdapterError("settings update timed out")
            return original(method, params)
        server.call.side_effect = call
        with mock.patch.object(codex, "resolve_codex", return_value="codex"), \
                mock.patch.object(codex, "AppServer", return_value=server), \
                mock.patch.object(codex.subprocess, "run") as run:
            result = codex.CodexAdapter(model="model-a", reasoning_effort="ultra").deliver("thread", {"body": "[relay first] brief"})
        self.assertFalse(result["accepted"])
        self.assertNotIn("unknown", result)
        run.assert_not_called()

    def test_settings_flush_timeout_prevents_queue_and_app_open(self):
        server = self.server()
        server.close.return_value = False
        with mock.patch.object(codex, "resolve_codex", return_value="codex"), \
                mock.patch.object(codex, "AppServer", return_value=server), \
                mock.patch.object(codex.subprocess, "run") as run, \
                mock.patch.object(codex, "open_in_app") as open_app:
            adapter = codex.CodexAdapter()
            with self.assertRaisesRegex(AdapterError, "thread thread is unconfirmed"):
                adapter.new_thread("project", "lead")
            result = codex.CodexAdapter().deliver("thread", {"body": "[relay first] brief"})
        self.assertFalse(result["accepted"])
        self.assertIn("did not exit cleanly", result["stderr"])
        self.assertNotIn("unknown", result)
        open_app.assert_not_called()
        run.assert_not_called()

    def test_abnormal_server_exit_does_not_confirm_settings_flush(self):
        for running in (False, True):
            with self.subTest(running=running):
                server = codex.AppServer.__new__(codex.AppServer)
                server.proc = mock.Mock()
                server.proc.poll.return_value = None if running else 1
                server.proc.wait.return_value = 1
                server._reader = mock.Mock()
                self.assertFalse(server.close())
                server.proc.terminate.assert_not_called()

    def test_creation_rpc_failure_keeps_original_error_when_server_also_exits_abnormally(self):
        server = self.server()
        server.call.side_effect = AdapterError("specific config read failure")
        server.close.return_value = False
        with mock.patch.object(codex, "resolve_codex", return_value="codex"), \
                mock.patch.object(codex, "AppServer", return_value=server):
            with self.assertRaisesRegex(AdapterError, "specific config read failure"):
                codex.CodexAdapter().new_thread("project", "lead", open_app=False)


if __name__ == "__main__":
    unittest.main()
