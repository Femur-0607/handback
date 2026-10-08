import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from handback import cli, config, inbox
from handback.state import ProjectState


class FakeAdapter:
    def __init__(self):
        self.deliveries = []
        self.result = {"outcome": "completed", "text": "한글 답"}
        self.delivery = {"accepted": True, "returncode": 0, "stdout": "ok", "stderr": ""}

    def deliver(self, thread, mail):
        self.deliveries.append((thread, mail))
        return self.delivery

    def fallback_collect(self, request, timeout=0):
        return self.result

    def new_thread(self, *args, **kwargs):
        return "fake-thread"


class RequestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "project"
        self.root.mkdir()
        self.home = Path(self.temp.name) / "state"
        self.env = patch.dict(os.environ, {"HANDBACK_HOME": str(self.home)}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        for key in ("HANDBACK_LEAD", "HANDBACK_WORKERS"):
            self.env2 = patch.dict(os.environ)
            self.env2.start()
            self.addCleanup(self.env2.stop)
            os.environ.pop(key, None)
        self.adapter = FakeAdapter()
        self.mock = patch.object(cli, "get_adapter", return_value=self.adapter)
        self.mock.start()
        self.addCleanup(self.mock.stop)
        # Detached collectors are covered in test_collector; keep these in-process.
        self.spawn = patch.object(cli.collector, "spawn", return_value={"pid": 0})
        self.spawn.start()
        self.addCleanup(self.spawn.stop)
        self.state = ProjectState(self.root)

    def invoke(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main([*args, "--root", str(self.root)])
        return code, stdout.getvalue(), stderr.getvalue()

    def invoke_new(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main(["new", "--cwd", str(self.root), "--name", "4.3 short goal", *args])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_new_without_initial_instruction_preserves_legacy_output(self):
        self.assertEqual(self.invoke_new()[:2], (0, "fake-thread\n"))
        self.assertEqual(self.invoke_new("--worker", "codex")[:2], (0, "codex:fake-thread\n"))
        self.assertEqual(self.adapter.deliveries, [])

    def test_new_text_sync_emits_combined_json_and_honors_send_options(self):
        destination = self.root / "answer.txt"
        with patch.object(self.adapter, "fallback_collect", wraps=self.adapter.fallback_collect) as collect:
            code, raw, err = self.invoke_new("--text", "first task", "--timeout", "7",
                                             "--label", "minor lead", "--return-file", str(destination))
        self.assertEqual(code, 0, err)
        data = json.loads(raw)
        self.assertEqual(data["created_handle"], "codex:fake-thread")
        self.assertEqual((data["handle"], data["id"], data["status"]),
                         ("codex:fake-thread", "fake-thread", "completed"))
        self.assertEqual(data["result"]["text"], "한글 답")
        self.assertEqual(destination.read_text(encoding="utf-8"), "한글 답")
        self.assertEqual(collect.call_args.kwargs["timeout"], 7)
        self.assertIn("From minor lead", self.adapter.deliveries[0][1]["body"])
        self.assertEqual(self.state.load_request(data["request_id"])["result"], data["result"])

    def test_new_file_no_wait_dispatches_once_with_durable_request(self):
        brief = self.root / "brief.md"
        brief.write_text("read-only task", encoding="utf-8")
        code, raw, err = self.invoke_new("--worker", "auto", "--file", str(brief), "--no-wait")
        self.assertEqual(code, 0, err)
        data = json.loads(raw)
        self.assertEqual((data["status"], data["collector_pid"]), ("accepted", 0))
        self.assertIn("Read " + str(brief.resolve()), self.adapter.deliveries[0][1]["body"])
        self.assertEqual(len(self.adapter.deliveries), 1)
        self.assertEqual(len(self.state.requests()), 1)

    def test_new_timeout_and_unknown_delivery_keep_recovery_ids(self):
        self.adapter.result = {"outcome": "timeout", "text": ""}
        code, raw, err = self.invoke_new("--text", "task", "--timeout", "1")
        self.assertEqual(code, 3, err)
        data = json.loads(raw)
        self.assertEqual(data["status"], "accepted")
        self.assertEqual(self.state.load_request(data["request_id"])["status"], "accepted")
        # Use another adapter native ID for an independent minor unit.
        self.adapter.delivery = {"accepted": False, "unknown": True, "stderr": "uncertain"}
        with patch.object(self.adapter, "new_thread", return_value="second"):
            code, raw, err = self.invoke_new("--text", "task", "--no-wait")
        self.assertEqual(code, 4, err)
        self.assertEqual(json.loads(raw)["status"], "delivery_unknown")
        self.assertEqual(len(self.adapter.deliveries), 2)

    def test_new_invalid_initial_instruction_does_not_create_thread(self):
        with patch.object(self.adapter, "new_thread") as create:
            for args in (("--text", " "), ("--file", str(self.root / "absent")),
                         ("--role", "lead", "--text", "task"), ("--text", "task", "--timeout", "-1")):
                with self.subTest(args=args):
                    try:
                        code, _, _ = self.invoke_new(*args)
                        self.assertNotEqual(code, 0)
                    except SystemExit as error:
                        self.assertEqual(error.code, 2)
            with self.assertRaises(SystemExit):
                self.invoke_new("--text", "task", "--file", str(self.root / "absent"))
        create.assert_not_called()

    def test_new_antigravity_initial_send_binds_provisional_handle(self):
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "config.json").write_text('{"agents":{"antigravity":{"enabled":true}}}', encoding="utf-8")
        config.use_topology(self.root, "claude:lead", ["antigravity"])

        class AntigravityFake(FakeAdapter):
            needs_context = True

            def new_thread(self, *args, **kwargs):
                return "pending-fake"

            def deliver(self, thread, mail, **kwargs):
                self.deliveries.append((thread, mail))
                self.context = kwargs
                return {"accepted": True, "thread": "actual-conversation"}

        adapter = AntigravityFake()
        with patch.object(cli, "get_adapter", return_value=adapter):
            code, raw, err = self.invoke_new("--worker", "antigravity", "--text", "task", "--no-wait")
        self.assertEqual(code, 0, err)
        data = json.loads(raw)
        self.assertEqual(data["created_handle"], "antigravity:pending-fake")
        self.assertEqual((data["handle"], data["id"]), ("antigravity:actual-conversation", "actual-conversation"))
        self.assertEqual(adapter.context["title"], "4.3 short goal")
        self.assertEqual(len(adapter.deliveries), 1)
        self.assertEqual(self.state.threads()[data["created_handle"]]["bound_to"], data["handle"])

    def test_legacy_no_wait_marker_and_durable_wait(self):
        code, marker, error = self.invoke("send", "--thread", "fake", "--text", "work", "--no-wait")
        self.assertEqual(code, 0, error)
        self.assertRegex(marker.strip(), r"^\[relay [0-9a-f]{8}\]$")
        request = self.state.requests()[0]
        self.assertEqual(request["status"], "accepted")
        code, text, error = self.invoke("wait", "--thread", "fake", "--marker", marker.strip(), "--timeout", "1")
        self.assertEqual((code, text.strip()), (0, "한글 답"), error)
        self.assertEqual(len(self.adapter.deliveries), 1)
        self.assertEqual(len(inbox.pending(self.state.path / "inbox", "claude:lead")), 1)
        # Re-reading a completed request never sends again or duplicates the reply.
        self.invoke("wait", "--request", request["id"])
        self.assertEqual(len(self.adapter.deliveries), 1)
        self.assertEqual(len(inbox.pending(self.state.path / "inbox", "claude:lead")), 1)

    def test_timeout_preserves_single_open_request(self):
        self.adapter.result = {"outcome": "timeout", "text": ""}
        code, _, _ = self.invoke("send", "--to", "codex:fake", "--text", "work", "--timeout", "1")
        self.assertEqual(code, 3)
        self.assertEqual(self.state.requests()[0]["status"], "accepted")
        code, _, error = self.invoke("send", "--to", "codex:fake", "--text", "duplicate")
        self.assertEqual(code, 4)
        self.assertIn("already has an open request", error)
        self.assertEqual(len(self.adapter.deliveries), 1)

    def test_switch_preserves_request_return_address(self):
        config.use_topology(self.root, "claude:old", ["codex"])
        code, raw, _ = self.invoke("send", "--to", "codex:fake", "--text", "work", "--no-wait")
        self.assertEqual(code, 0)
        request_id = json.loads(raw)["request_id"]
        config.use_topology(self.root, "claude:new", ["codex"])
        self.invoke("wait", "--request", request_id)
        self.assertEqual(len(inbox.pending(self.state.path / "inbox", "claude:old")), 1)
        self.assertEqual(inbox.pending(self.state.path / "inbox", "claude:new"), [])

    def test_failed_turn_and_rejected_send_have_legacy_exit_codes(self):
        self.adapter.result = {"outcome": "failed", "error": "aborted", "text": ""}
        self.assertEqual(self.invoke("send", "--thread", "a", "--text", "work")[0], 2)
        self.adapter.delivery = {"accepted": False, "returncode": 1, "stderr": "not accepted"}
        self.assertEqual(self.invoke("send", "--thread", "b", "--text", "work")[0], 4)

    def test_uncertain_delivery_is_not_automatically_retried(self):
        with patch.object(self.adapter, "deliver", side_effect=TimeoutError("ambiguous")):
            self.assertEqual(self.invoke("send", "--thread", "a", "--text", "work")[0], 4)
        self.assertEqual(self.state.requests()[0]["status"], "delivery_unknown")
        self.assertEqual(self.invoke("send", "--thread", "a", "--text", "work")[0], 4)
        self.assertEqual(self.adapter.deliveries, [])

    def test_reply_recovery_after_published_mail_before_saved_request(self):
        self.invoke("send", "--thread", "a", "--text", "work", "--no-wait")
        request = self.state.requests()[0]
        original = self.state.save_request

        def fail_completion(value):
            if value["status"] == "completed":
                raise OSError("simulated disk error")
            original(value)

        with patch.object(self.state, "save_request", side_effect=fail_completion):
            with self.assertRaises(OSError):
                cli.finish_request(self.state, request, self.adapter.result)
        completed = cli.finish_request(self.state, request, self.adapter.result)
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(len(inbox.pending(self.state.path / "inbox", "claude:lead")), 1)

    def test_fast_completion_during_delivery_is_not_overwritten(self):
        def deliver_and_collect(thread, mail):
            request = self.state.load_request(mail["request_id"])
            cli.finish_request(self.state, request, self.adapter.result)
            return self.adapter.delivery

        with patch.object(self.adapter, "deliver", side_effect=deliver_and_collect):
            self.assertEqual(self.invoke("send", "--thread", "a", "--text", "work", "--no-wait")[0], 0)
        self.assertEqual(self.state.requests()[0]["status"], "completed")
        self.assertEqual(self.state.requests()[0]["result"]["text"], "한글 답")

    def test_large_reply_keeps_full_output_and_links_mail_to_report(self):
        self.adapter.result = {"outcome": "completed", "text": "가" * 30000}
        code, text, error = self.invoke("send", "--thread", "a", "--text", "work")
        self.assertEqual(code, 0, error)
        self.assertEqual(text.strip(), "가" * 30000)
        mail = inbox.pending(self.state.path / "inbox", "claude:lead")[0]
        self.assertIn("Read the complete result", mail["body"])
        request = self.state.requests()[0]
        report = self.state.path / "results" / (request["id"] + ".json")
        self.assertEqual(json.loads(report.read_text(encoding="utf-8")), self.adapter.result)

    def test_json_escaping_is_counted_for_large_reply(self):
        self.adapter.result = {"outcome": "completed", "text": '"' * 40000}
        code, text, error = self.invoke("send", "--thread", "a", "--text", "work")
        self.assertEqual(code, 0, error)
        self.assertEqual(text.strip(), '"' * 40000)

    def test_disabled_worker_does_not_prevent_collecting_existing_request(self):
        self.invoke("send", "--thread", "a", "--text", "work", "--no-wait")
        request = self.state.requests()[0]
        (self.home / "config.json").write_text('{"agents":{"codex":{"enabled":false}}}', encoding="utf-8")
        self.assertEqual(self.invoke("wait", "--request", request["id"])[0], 0)

    def test_outbox_write_failure_releases_unsent_claim(self):
        with patch.object(inbox, "put", side_effect=OSError("disk failure")):
            self.assertEqual(self.invoke("send", "--thread", "a", "--text", "work")[0], 4)
        self.assertEqual(self.state.requests()[0]["status"], "send_failed")
        self.assertEqual(self.adapter.deliveries, [])
        self.assertEqual(self.invoke("send", "--thread", "a", "--text", "work")[0], 0)

    def test_crashed_prepared_request_can_be_recovered_without_sending(self):
        self.invoke("send", "--thread", "a", "--text", "work", "--no-wait")
        request = self.state.requests()[0]
        request["status"] = "prepared"
        self.state.save_request(request)
        before = len(self.adapter.deliveries)
        self.assertEqual(self.invoke("wait", "--request", request["id"])[0], 4)
        self.assertEqual(self.state.requests()[0]["status"], "send_failed")
        self.assertEqual(len(self.adapter.deliveries), before)

    def test_configure_cli_sets_and_clears_role_defaults(self):
        code, raw, error = self.invoke("configure", "--agent", "codex", "--role", "worker",
                                        "--model", "selected-model", "--reasoning-effort", "ultra")
        self.assertEqual(code, 0, error)
        self.assertEqual(config.execution_settings(json.loads(raw)["values"], "codex"),
                         {"model": "selected-model", "reasoning_effort": "ultra"})
        code, raw, error = self.invoke("configure", "--agent", "codex", "--role", "worker",
                                        "--clear-model", "--clear-reasoning-effort")
        self.assertEqual(code, 0, error)
        self.assertEqual(config.execution_settings(json.loads(raw)["values"], "codex"),
                         {"model": None, "reasoning_effort": None})

    def test_new_execution_snapshot_survives_default_changes_and_initial_send(self):
        config.configure_execution(self.root, "codex", "worker", model="default-model", reasoning_effort="high")
        with patch.object(cli, "get_adapter", return_value=self.adapter) as factory:
            code, raw, error = self.invoke_new("--model", "thread-model", "--reasoning-effort", "ultra",
                                                "--text", "first", "--no-wait")
        self.assertEqual(code, 0, error)
        saved = self.state.threads()["codex:fake-thread"]["execution"]
        self.assertEqual(saved, {"model": "thread-model", "reasoning_effort": "ultra"})
        self.assertEqual(factory.call_args.kwargs["thread"]["execution"], saved)
        request_id = json.loads(raw)["request_id"]
        self.invoke("wait", "--request", request_id)
        config.configure_execution(self.root, "codex", "worker", model="later-model", reasoning_effort="low")
        with patch.object(cli, "get_adapter", return_value=self.adapter) as factory:
            code, _, error = self.invoke("send", "--to", "codex:fake-thread", "--text", "later", "--no-wait")
        self.assertEqual(code, 0, error)
        self.assertEqual(factory.call_args.kwargs["thread"]["execution"], saved)

    def test_new_captures_transport_resolved_native_defaults(self):
        self.adapter.execution_settings = {"model": "native-model", "reasoning_effort": "ultra"}
        code, _, error = self.invoke_new()
        self.assertEqual(code, 0, error)
        self.assertEqual(self.state.threads()["codex:fake-thread"]["execution"], self.adapter.execution_settings)

    def test_send_override_persists_only_after_unambiguous_acceptance(self):
        original = {"model": "old", "reasoning_effort": "high"}
        self.state.register_thread({"handle": "codex:worker", "agent": "codex", "id": "worker",
                                    "name": "old name", "role": "worker", "execution": original})
        self.adapter.delivery = {"accepted": False, "returncode": 1}
        code, _, _ = self.invoke("send", "--to", "codex:worker", "--text", "task",
                                  "--model", "new", "--reasoning-effort", "ultra", "--no-wait")
        self.assertEqual(code, 4)
        self.assertEqual(self.state.threads()["codex:worker"]["execution"], original)
        self.adapter.delivery = {"accepted": True}

        def deliver(thread, mail):
            self.state.register_thread({"handle": "codex:worker", "name": "edited during delivery"})
            return self.adapter.delivery

        with patch.object(self.adapter, "deliver", side_effect=deliver):
            code, _, error = self.invoke("send", "--to", "codex:worker", "--text", "task",
                                          "--model", "new", "--reasoning-effort", "ultra", "--no-wait")
        self.assertEqual(code, 0, error)
        current = self.state.threads()["codex:worker"]
        self.assertEqual(current["execution"], {"model": "new", "reasoning_effort": "ultra"})
        self.assertEqual(current["name"], "edited during delivery")

    def test_unknown_send_override_does_not_replace_saved_execution(self):
        original = {"model": "old", "reasoning_effort": "high"}
        self.state.register_thread({"handle": "codex:worker", "agent": "codex", "execution": original})
        self.adapter.delivery = {"accepted": False, "unknown": True}
        code, _, _ = self.invoke("send", "--to", "codex:worker", "--text", "task",
                                  "--reasoning-effort", "ultra", "--no-wait")
        self.assertEqual(code, 4)
        self.assertEqual(self.state.threads()["codex:worker"]["execution"], original)
        self.assertEqual(self.state.requests()[0]["execution"]["reasoning_effort"], "ultra")

    def test_unknown_override_confirmed_by_wait_is_used_on_next_send(self):
        original = {"model": "old", "reasoning_effort": "high"}
        self.state.register_thread({"handle": "codex:worker", "agent": "codex", "execution": original})
        self.adapter.delivery = {"accepted": False, "unknown": True}
        self.invoke("send", "--to", "codex:worker", "--text", "task",
                    "--reasoning-effort", "ultra", "--no-wait")
        request = self.state.requests()[0]
        self.assertEqual(self.invoke("wait", "--request", request["id"])[0], 0)
        self.adapter.delivery = {"accepted": True}
        with patch.object(cli, "get_adapter", return_value=self.adapter) as factory:
            code, _, error = self.invoke("send", "--to", "codex:worker", "--text", "next", "--no-wait")
        self.assertEqual(code, 0, error)
        self.assertEqual(factory.call_args.kwargs["thread"]["execution"],
                         {"model": "old", "reasoning_effort": "ultra"})

    def test_delayed_sender_response_cannot_rollback_later_execution(self):
        later = {"model": "later", "reasoning_effort": "low"}

        def deliver_and_complete(thread, mail):
            request = self.state.load_request(mail["request_id"])
            cli.finish_request(self.state, request, self.adapter.result)
            self.state.register_thread({"handle": request["handle"], "execution": later,
                                        "execution_request": "f" * 32})
            return {"accepted": True}

        with patch.object(self.adapter, "deliver", side_effect=deliver_and_complete):
            code, _, error = self.invoke("send", "--to", "codex:worker", "--text", "task",
                                          "--model", "first", "--reasoning-effort", "ultra", "--no-wait")
        self.assertEqual(code, 0, error)
        self.assertEqual(self.state.threads()["codex:worker"]["execution"], later)

    def test_fast_completion_is_enriched_with_resolved_native_execution(self):
        self.adapter.execution_settings = {"model": "native-model", "reasoning_effort": "ultra"}

        def deliver_and_complete(thread, mail):
            cli.finish_request(self.state, self.state.load_request(mail["request_id"]), self.adapter.result)
            return {"accepted": False, "unknown": True}

        with patch.object(self.adapter, "deliver", side_effect=deliver_and_complete):
            code, _, error = self.invoke("send", "--to", "codex:worker", "--text", "task",
                                          "--reasoning-effort", "ultra", "--no-wait")
        self.assertEqual(code, 0, error)
        self.assertEqual(self.state.threads()["codex:worker"]["execution"], self.adapter.execution_settings)

    def test_existing_antigravity_model_override_is_rejected_before_delivery(self):
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "config.json").write_text('{"agents":{"antigravity":{"enabled":true}}}', encoding="utf-8")
        config.use_topology(self.root, "claude:lead", ["antigravity"])
        code, _, error = self.invoke("send", "--to", "antigravity:existing", "--text", "task",
                                      "--model", "pro", "--no-wait")
        self.assertEqual(code, 4)
        self.assertIn("only be selected when creating", error)
        self.assertEqual(self.adapter.deliveries, [])
        self.assertEqual(self.state.requests(), [])


class AdapterSelectionTests(unittest.TestCase):
    def test_codex_adapter_receives_role_and_saved_thread_execution(self):
        values = {"agents": {"codex": {"executable": "codex.exe", "model": "shared",
                                       "lead": {"model": "lead", "reasoning_effort": "high"},
                                       "worker": {"reasoning_effort": "ultra"}}}}
        with patch.object(cli, "CodexAdapter") as adapter:
            cli.get_adapter("codex", values, role="lead", thread={"execution": {"reasoning_effort": "low"}})
        adapter.assert_called_once_with(executable="codex.exe", model="lead", reasoning_effort="low")

    def test_unavailable_control_is_not_silently_ignored(self):
        for agent, setting in (("claude", {"model": "opus"}),
                                ("antigravity", {"reasoning_effort": "ultra"})):
            with self.subTest(agent=agent), self.assertRaises(ValueError):
                cli.get_adapter(agent, {"agents": {agent: setting}})


if __name__ == "__main__":
    unittest.main()
