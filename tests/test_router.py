import concurrent.futures
import contextlib
import errno
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from handback import cli, collector, config, envelope, hooks, inbox, router
from handback.adapters import antigravity
from handback.state import ProjectState, atomic_json, home_lock, _normal_path
import hashlib


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.root = base / "project"
        self.root.mkdir()
        self.home = base / "state"
        env = patch.dict(os.environ, {"HANDBACK_HOME": str(self.home),
                                     "GIT_CEILING_DIRECTORIES": str(base)})
        env.start()
        self.addCleanup(env.stop)
        for name in ("HANDBACK_LEAD", "HANDBACK_WORKERS", "CODEX_WINDOWS_SANDBOX_PACKAGE_FAMILY",
                     "CODEX_SANDBOX_NETWORK_DISABLED", "CODEX_SANDBOX"):
            os.environ.pop(name, None)
        self.state = ProjectState(self.root, home=self.home)
        atomic_json(self.home / "config.json", {"agents": {"antigravity": {"enabled": True}}})
        self.adapter = Mock()
        self.adapter.deliver_to_lead.return_value = {"accepted": True}
        self.factory = lambda a: self.adapter

    def mail(self, recipient="codex:thread", body="결과"):
        return inbox.put(self.state.path / "inbox", envelope.make("request", "antigravity:worker",
                         recipient, body))

    def route(self, mail, **kwargs):
        return router.route(self.state, mail, adapter_for=self.factory, **kwargs)

    def test_concurrent_three_paths_attempt_once(self):
        mail = self.mail()
        started, release = threading.Event(), threading.Event()
        def deliver(*a, **kw):
            started.set()
            self.assertTrue(release.wait(3))
            return {"accepted": True}
        self.adapter.deliver_to_lead.side_effect = deliver
        with concurrent.futures.ThreadPoolExecutor(3) as pool:
            first = pool.submit(self.route, mail)
            self.assertTrue(started.wait(3))
            others = [pool.submit(self.route, mail) for _ in range(2)]
            for result in others:
                result.result(3)
            release.set()
            self.assertEqual(first.result(3)["status"], "delivered")
        self.assertEqual(self.adapter.deliver_to_lead.call_count, 1)
        self.assertEqual(len(inbox.pending(self.state.path / "inbox")), 1)

    def test_busy_route_retries_record_read_without_replaying_delivery(self):
        mail = self.mail()
        path = self.state.path / "inbox/delivered" / (mail["id"] + ".json")
        original_open = Path.open
        for status in ("delivery_unknown", "delivered"):
            with self.subTest(status=status):
                entry = {"id": mail["id"], "status": status, "attempts": 1}
                atomic_json(path, entry)
                reads = []

                def open_record(candidate, *args, **kwargs):
                    if candidate == path:
                        reads.append(candidate)
                        if len(reads) == 1:
                            raise PermissionError(errno.EACCES, "sharing violation")
                    return original_open(candidate, *args, **kwargs)

                with patch.object(self.state, "lock", side_effect=TimeoutError("delivery busy")), \
                        patch.object(Path, "open", open_record), \
                        patch("handback.state.sys.platform", "win32"), \
                        patch("handback.state.time.sleep"):
                    self.assertEqual(self.route(mail), entry)
                self.assertEqual(len(reads), 2)
                self.assertEqual(self.route(mail), entry)
                self.adapter.deliver_to_lead.assert_not_called()
                self.assertEqual(len(inbox.pending(self.state.path / "inbox")), 1)

    def test_sandbox_pending_then_external_delivery(self):
        mail = self.mail()
        with patch.dict(os.environ, {"CODEX_WINDOWS_SANDBOX_PACKAGE_FAMILY": "sandbox"}):
            self.assertEqual(self.route(mail)["status"], "pending")
            self.adapter.deliver_to_lead.assert_not_called()
        self.assertEqual(self.route(mail)["status"], "delivered")

    def test_failed_automatically_retries(self):
        mail = self.mail()
        self.adapter.deliver_to_lead.return_value = {"accepted": False, "returncode": 1}
        self.assertEqual(self.route(mail)["status"], "failed")
        self.adapter.deliver_to_lead.return_value = {"accepted": True}
        self.assertEqual(self.route(mail)["attempts"], 2)

    def test_unknown_only_explicit_retry(self):
        mail = self.mail()
        self.adapter.deliver_to_lead.side_effect = TimeoutError()
        self.assertEqual(self.route(mail)["status"], "delivery_unknown")
        self.route(mail)
        self.assertEqual(self.adapter.deliver_to_lead.call_count, 1)
        self.adapter.deliver_to_lead.side_effect = None
        self.assertEqual(self.route(mail, explicit=True)["status"], "delivered")

    def test_crashed_attempt_stays_unknown(self):
        mail = self.mail()
        atomic_json(self.state.path / "inbox/delivered" / (mail["id"] + ".json"),
                    {"id": mail["id"], "recipient": mail["recipient"], "status": "delivery_unknown", "attempts": 1})
        self.route(mail)
        self.adapter.deliver_to_lead.assert_not_called()

    def test_fixed_text_and_large_body_path(self):
        mail = self.mail(body="가" * 5000)
        text = router.delivery_text(self.state, mail)
        self.assertIn("Not user input and not approval.", text)
        self.assertIn("From antigravity:worker for request request", text)
        self.assertIn(str(self.state.path / "inbox" / (mail["id"] + ".json")), text)
        self.assertIn("--for codex:thread --id " + mail["id"], text)

    def test_ack_instruction_runs_without_an_installed_cli_alias(self):
        root = self.root.parent / "project with spaces"
        root.mkdir()
        state = ProjectState(root, home=self.home)
        mail = inbox.put(state.path / "inbox", envelope.make(
            "request", "antigravity:worker", "codex:thread", "result"))
        command = router.delivery_text(state, mail).split("\nAfter processing: ", 1)[1]
        arguments = shlex.split(command)
        if sys.platform == "win32":
            self.assertEqual(arguments[0], "&")
            invocation = ["powershell", "-NoProfile", "-NonInteractive", "-Command", command]
        else:
            self.assertEqual(arguments[0], sys.executable)
            invocation = arguments
        result = subprocess.run(invocation, cwd=root,
                                capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(inbox.pending(state.path / "inbox", "codex:thread"), [])

    def test_busy_then_idle_then_preinvocation_pending(self):
        mail = self.mail("antigravity:lead-id")
        antigravity.observe_lead(self.home, "lead-id")
        self.assertEqual(self.route(mail)["status"], "pending")
        antigravity.record_observation(self.home, {"conversationId": "lead-id", "fullyIdle": True}, "Stop")
        self.assertEqual(self.route(mail)["status"], "delivered")
        antigravity.record_observation(self.home, {"conversationId": "lead-id"}, "PreInvocation")
        self.assertEqual(self.route(self.mail("antigravity:lead-id"))["status"], "pending")

    def test_antigravity_consumes_idle_before_io(self):
        antigravity.observe_lead(self.home, "lead-id")
        antigravity.record_observation(self.home, {"conversationId": "lead-id", "fullyIdle": True}, "Stop")
        adapter = antigravity.AntigravityAdapter()
        def delivered(*a, **kw):
            self.assertFalse(antigravity.lead_idle(self.home, "lead-id"))
            antigravity.record_observation(self.home, {"conversationId": "lead-id", "fullyIdle": True}, "Stop")
            return {"accepted": True}
        with patch.object(adapter, "deliver", side_effect=delivered):
            adapter.deliver_to_lead("lead-id", "text", state=self.state)
        self.assertTrue(antigravity.lead_idle(self.home, "lead-id"))

    def test_codex_recovery_exact_session_and_event(self):
        self.state.write_json("topology.json", {"lead": "codex:thread", "root": str(self.root)})
        self.mail()
        self.assertIsNone(hooks.process("codex", "SessionStart", {"session_id": "thread"}, home=self.home))
        self.assertIsNone(hooks.process("codex", "UserPromptSubmit", {"session_id": "other"}, home=self.home))
        output = hooks.process("codex", "UserPromptSubmit", {"session_id": "thread"}, home=self.home)
        self.assertIn("결과", output["hookSpecificOutput"]["additionalContext"])

    def test_antigravity_recovery_ephemeral_exact_lead(self):
        self.state.write_json("topology.json", {"lead": "antigravity:lead-id", "root": str(self.root)})
        self.mail("antigravity:lead-id")
        output = hooks.process("antigravity", "PreInvocation", {"conversationId": "lead-id"}, home=self.home)
        self.assertIn("결과", output["injectSteps"][0]["ephemeralMessage"])
        self.assertEqual(hooks.process("antigravity", "PreInvocation", {"conversationId": "other"}, home=self.home), {})

    def test_stop_spawns_only_managed_idle_router(self):
        config.use_topology(self.root, "antigravity:lead-id", ["codex"], home=self.home)
        with patch.object(router, "spawn_external") as spawn:
            hooks.process("antigravity", "Stop", {"conversationId": "other", "fullyIdle": True}, home=self.home)
            hooks.process("antigravity", "Stop", {"conversationId": "lead-id", "fullyIdle": False}, home=self.home)
            spawn.assert_not_called()
            hooks.process("antigravity", "Stop", {"conversationId": "lead-id", "fullyIdle": True}, home=self.home)
            self.assertEqual(spawn.call_count, 1)

    def test_external_router_pending_once(self):
        mail = self.mail()
        with patch.object(cli, "get_adapter", return_value=self.adapter):
            router.external_run(self.state, "worker")
            router.external_run(self.state, "worker")
        self.assertEqual(self.adapter.deliver_to_lead.call_count, 1)
        self.assertEqual(router.record(self.state, mail["id"])["status"], "delivered")

    def test_topology_rejects_alias_codex_worker_and_self(self):
        for lead, workers in (("codex:lead", ["antigravity"]), ("codex:t", ["codex"]),
                              ("antigravity:lead", ["codex"])):
            with self.assertRaises(ValueError):
                config.use_topology(self.root, lead, workers, home=self.home)
        self.state.register_thread({"handle": "antigravity:worker", "agent": "antigravity", "role": "worker"})
        with self.assertRaisesRegex(ValueError, "same handle"):
            config.use_topology(self.root, "antigravity:worker", ["antigravity"], home=self.home)

    def test_antigravity_self_uses_observed_environment(self):
        with patch.dict(os.environ, {"ANTIGRAVITY_CONVERSATION_ID": "self-id"}):
            result = config.use_topology(self.root, "antigravity:self", ["codex"], home=self.home)
        self.assertEqual(result["values"]["lead"], "antigravity:self-id")
        self.assertTrue((antigravity.observation_dir(self.home) / "self-id.jsonl").exists())

    def test_readonly_parent_lock_compatibility_process_exclusion(self):
        name = ".agent-relay-lock-" + hashlib.sha256(_normal_path(self.home).encode()).hexdigest()[:24] + ".lock"
        legacy = self.home.parent / name
        legacy.write_bytes(b"\0")
        script = """import sys
from handback.state import _home_os_lock, _home_os_unlock
with open(sys.argv[1], 'a+b') as stream:
    try:
        token = _home_os_lock(stream, True)
        _home_os_unlock(stream, token)
        print('acquired')
    except BlockingIOError:
        print('blocked')
"""
        with home_lock(self.home):
            before = (self.home / ".locks" / name).stat().st_ino
            result = subprocess.run([sys.executable, "-c", script, str(legacy)], capture_output=True, text=True)
            self.assertEqual(result.stdout.strip(), "blocked", result.stderr)
            atomic_json(self.home / "payload.json", {})
            self.assertEqual(before, (self.home / ".locks" / name).stat().st_ino)
        result = subprocess.run([sys.executable, "-c", script, str(legacy)], capture_output=True, text=True)
        self.assertEqual(result.stdout.strip(), "acquired", result.stderr)

    def test_missing_parent_lock_permission_rejects_split(self):
        with patch.object(Path, "open", side_effect=PermissionError("sandbox")):
            with self.assertRaisesRegex(ValueError, "split old-process ownership"):
                with home_lock(self.home):
                    pass

    def test_new_codex_lead_writable_roots(self):
        config.use_topology(self.root, "claude:session", ["antigravity"], home=self.home)
        self.adapter.new_thread.return_value = "new-lead"
        with patch.object(cli, "get_adapter", return_value=self.adapter), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["new", "--role", "lead", "--worker", "codex", "--cwd", str(self.root),
                                       "--name", "Lead", "--no-open"]), 0)
        roots = self.adapter.new_thread.call_args.kwargs["writable_roots"]
        self.assertIn(str(self.home), roots)
        self.assertIn(str(antigravity.gemini_home() / "config"), roots)
        self.assertEqual(self.state.read_json("topology.json")["lead"], "codex:new-lead")

    def test_lead_switch_preserves_original_reply_route(self):
        config.use_topology(self.root, "codex:old", ["antigravity"], home=self.home)
        request_id = "a" * 32
        outgoing = inbox.put(self.state.path / "inbox", envelope.make(request_id, "codex:old", "antigravity:worker", "task", kind="request"))
        request = {"id": request_id, "handle": "antigravity:worker", "return_to": "codex:old",
                   "outgoing_id": outgoing["id"], "created_utc": outgoing["created_utc"], "status": "accepted"}
        self.state.save_request(request)
        config.use_topology(self.root, "claude:new", ["antigravity"], home=self.home)
        with patch.object(cli, "get_adapter", return_value=self.adapter):
            current = collector.finish_request(self.state, request, {"outcome": "completed", "text": "result"})
        self.assertEqual(router.record(self.state, current["reply_id"])["recipient"], "codex:old")
        self.assertEqual(self.adapter.deliver_to_lead.call_args.args[0], "old")

    def test_original_recipient_uses_saved_lead_execution_after_topology_switch(self):
        saved = {"handle": "codex:old", "agent": "codex", "role": "lead",
                 "execution": {"model": "old-model", "reasoning_effort": "ultra"}}
        self.state.register_thread(saved)
        config.use_topology(self.root, "claude:new", ["antigravity"], home=self.home)
        config.configure_execution(self.root, "codex", "lead", model="new-model",
                                    reasoning_effort="low", home=self.home)
        mail = self.mail("codex:old")
        with patch.object(cli, "get_adapter", return_value=self.adapter) as factory:
            result = router.route(self.state, mail)
        self.assertEqual(result["status"], "delivered")
        self.assertEqual(factory.call_args.kwargs, {"role": "lead", "thread": saved})
        self.assertEqual(self.adapter.deliver_to_lead.call_args.args[0], "old")

    def test_watch_result_delivery_does_not_use_worker_factory(self):
        saved = {"handle": "codex:old", "agent": "codex", "role": "lead",
                 "execution": {"model": "lead-model", "reasoning_effort": "ultra"}}
        self.state.register_thread(saved)
        mail = self.mail("codex:old")
        worker_factory = Mock(side_effect=AssertionError("worker factory used for Lead"))
        watch = collector.WatchCollector(self.state, "codex:old", worker_factory)
        with patch.object(cli, "get_adapter", return_value=self.adapter) as factory:
            watch.poll()
        self.assertEqual(router.record(self.state, mail["id"])["status"], "delivered")
        worker_factory.assert_not_called()
        self.assertEqual(factory.call_args.kwargs, {"role": "lead", "thread": saved})

    def test_acknowledged_result_is_never_injected(self):
        mail = self.mail()
        inbox.acknowledge(self.state.path / "inbox", mail["id"], mail["recipient"])
        self.route(mail, explicit=True)
        self.adapter.deliver_to_lead.assert_not_called()

    def test_corrupt_delivery_record_blocks_retry(self):
        mail = self.mail()
        atomic_json(self.state.path / "inbox/delivered" / (mail["id"] + ".json"), {})
        with self.assertRaisesRegex(ValueError, "refusing to replay"):
            self.route(mail)
        self.adapter.deliver_to_lead.assert_not_called()

    def test_cli_explicit_redeliver_and_wrong_recipient(self):
        mail = self.mail()
        self.adapter.deliver_to_lead.return_value = {"accepted": False, "unknown": True}
        self.route(mail)
        self.adapter.deliver_to_lead.return_value = {"accepted": True}
        with patch.object(cli, "get_adapter", return_value=self.adapter), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["inbox", "redeliver", "--root", str(self.root), "--id", mail["id"]]), 0)
        self.assertEqual(router.record(self.state, mail["id"])["status"], "delivered")
        with self.assertRaisesRegex(ValueError, "different recipient"):
            router.redeliver(self.state, mail["id"], "codex:other")

    def test_external_collector_live_and_missing(self):
        request = {"id": "a" * 32, "agent": "antigravity", "thread": "worker", "handle": "antigravity:worker",
                   "status": "accepted", "root": str(self.root), "collector": {"pid": os.getpid()}}
        self.state.save_request(request)
        with patch.object(collector, "spawn") as spawn:
            router.external_run(self.state, "worker")
            spawn.assert_not_called()
            request.pop("collector")
            self.state.save_request(request)
            router.external_run(self.state, "worker")
            self.assertEqual(spawn.call_count, 1)

    def test_hook_child_delivers_pending_once(self):
        config.use_topology(self.root, "antigravity:lead-id", ["codex"], home=self.home)
        mail = self.mail()
        def child(state, conversation):
            router.external_run(state, conversation)
        with patch.object(router, "spawn_external", side_effect=child), patch.object(cli, "get_adapter", return_value=self.adapter):
            hooks.process("antigravity", "Stop", {"conversationId": "lead-id", "fullyIdle": True}, home=self.home)
            hooks.process("antigravity", "Stop", {"conversationId": "lead-id", "fullyIdle": True}, home=self.home)
        self.assertEqual(router.record(self.state, mail["id"])["status"], "delivered")
        self.assertEqual(self.adapter.deliver_to_lead.call_count, 1)

    def test_delivery_runs_outside_finish_project_lock(self):
        request_id = "c" * 32
        outgoing = inbox.put(self.state.path / "inbox", envelope.make(request_id, "codex:thread", "antigravity:worker", "task", kind="request"))
        request = {"id": request_id, "handle": "antigravity:worker", "return_to": "codex:thread",
                   "outgoing_id": outgoing["id"], "created_utc": outgoing["created_utc"], "status": "accepted"}
        self.state.save_request(request)
        def deliver(*a, **kw):
            def lock_project():
                with self.state.lock(timeout=.1):
                    return True
            with concurrent.futures.ThreadPoolExecutor(1) as pool:
                self.assertTrue(pool.submit(lock_project).result(1))
            return {"accepted": True}
        self.adapter.deliver_to_lead.side_effect = deliver
        with patch.object(cli, "get_adapter", return_value=self.adapter):
            collector.finish_request(self.state, request, {"outcome": "completed", "text": "result"})
        self.assertEqual(self.adapter.deliver_to_lead.call_count, 1)

    def test_new_antigravity_lead_binds_actual_id(self):
        self.adapter.new_thread.return_value = "pending-fake"
        self.adapter.deliver.return_value = {"accepted": True, "thread": "actual-lead"}
        with patch.object(cli, "get_adapter", return_value=self.adapter), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["new", "--role", "lead", "--worker", "antigravity", "--workers", "codex",
                                       "--cwd", str(self.root), "--name", "Lead"]), 0)
        self.assertEqual(self.state.read_json("topology.json")["lead"], "antigravity:actual-lead")

    def test_three_router_processes_deliver_once(self):
        mail = self.mail()
        script = """import json, sys, time
from pathlib import Path
from handback.state import ProjectState
from handback.router import route
state=ProjectState(sys.argv[1],home=sys.argv[2])
mail=json.loads((state.path/'inbox'/(sys.argv[3]+'.json')).read_text(encoding='utf-8'))
class Adapter:
    def deliver_to_lead(self,*args,**kwargs):
        with (state.path/'attempts.txt').open('a') as stream:
            stream.write('attempt\\n')
        time.sleep(.2)
        return {'accepted':True}
route(state,mail,adapter_for=lambda agent:Adapter())
"""
        children = [subprocess.Popen([sys.executable, "-c", script, str(self.root), str(self.home), mail["id"]],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(3)]
        results = []
        try:
            for child in children:
                _, err = child.communicate(timeout=10)
                results.append((child.returncode, err))
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.communicate(timeout=5)
        for code, err in results:
            self.assertEqual(code, 0, err)
        self.assertEqual((self.state.path / "attempts.txt").read_text().splitlines(), ["attempt"])

    def test_process_crash_during_delivery_prevents_automatic_replay(self):
        mail = self.mail()
        script = """import json, sys, os
from handback.state import ProjectState
from handback.router import route
state=ProjectState(sys.argv[1],home=sys.argv[2])
mail=json.loads((state.path/'inbox'/(sys.argv[3]+'.json')).read_text(encoding='utf-8'))
class Adapter:
    def deliver_to_lead(self,*args,**kwargs):
        os._exit(0)
route(state,mail,adapter_for=lambda agent:Adapter())
"""
        result = subprocess.run([sys.executable, "-c", script, str(self.root), str(self.home), mail["id"]],
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.route(mail)["status"], "delivery_unknown")
        self.adapter.deliver_to_lead.assert_not_called()

    def test_sandbox_codex_lead_no_wait_defers_collector_to_stop(self):
        config.use_topology(self.root, "codex:thread", ["antigravity"], home=self.home)
        self.adapter.needs_context = False
        self.adapter.deliver.return_value = {"accepted": True}
        with patch.dict(os.environ, {"CODEX_WINDOWS_SANDBOX_PACKAGE_FAMILY": "sandbox"}), \
                patch.object(cli, "get_adapter", return_value=self.adapter), \
                patch.object(collector, "spawn") as spawn, contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(["send", "--root", str(self.root), "--to", "antigravity:worker",
                                       "--text", "work", "--no-wait"]), 0)
        spawn.assert_not_called()
        self.assertIsNone(json.loads(out.getvalue())["collector_pid"])
        self.assertNotIn("collector", self.state.requests()[0])

    def test_old_antigravity_lead_stop_still_routes_original_requests(self):
        config.use_topology(self.root, "antigravity:old-id", ["codex"], home=self.home)
        self.state.save_request({"id": "e" * 32, "handle": "codex:worker", "status": "completed",
                                 "return_to": "antigravity:old-id", "root": str(self.root)})
        config.use_topology(self.root, "claude:new", ["codex"], home=self.home)
        with patch.object(router, "spawn_external") as spawn:
            hooks.process("antigravity", "Stop", {"conversationId": "old-id", "fullyIdle": True}, home=self.home)
        self.assertEqual(spawn.call_count, 1)
        self.assertEqual(hooks.process("antigravity", "PreInvocation", {"conversationId": "old-id"}, home=self.home), {})
