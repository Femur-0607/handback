"""Antigravity adapter: settings safety, completion contract, sidecar delivery."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from agent_relay import cli, hooks
from agent_relay.adapters import antigravity as agy
from agent_relay.adapters.base import AdapterError, AdapterUnavailable
from agent_relay.state import ProjectState

CONVERSATION = "11111111-2222-3333-4444-555555555555"


def step(i, kind, content="", tools=0, status="DONE", at="2026-10-07T09:00:00Z"):
    record = {"step_index": i, "source": "MODEL", "type": kind, "status": status, "created_at": at, "content": content}
    if tools:
        record["tool_calls"] = [{"name": "view_file", "args": {}}] * tools
    return record


def message(i, text, at="2026-10-07T09:00:00Z"):
    body = ("The following is a <SYSTEM_MESSAGE> not actually sent by the user.\n\n<SYSTEM_MESSAGE>\n"
            "[Message] timestamp=x sender=system priority=MESSAGE\n" + text + "\n</SYSTEM_MESSAGE>")
    return step(i, "SYSTEM_MESSAGE", body, at=at)


def blocked(i, reason, at="2026-10-07T09:00:00Z"):
    body = ("The following is a <SYSTEM_MESSAGE> not actually sent by the user.\n\n<SYSTEM_MESSAGE>\n"
            "Stop hook blocked termination: " + reason + "\n</SYSTEM_MESSAGE>")
    return step(i, "SYSTEM_MESSAGE", body, at=at)


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.gemini = base / "gemini"
        self.relay = base / "relay"
        self.root = base / "project"
        self.root.mkdir()
        (self.gemini / "config" / "projects").mkdir(parents=True)
        env = patch.dict(os.environ, {"GEMINI_HOME": str(self.gemini), "AGENT_RELAY_HOME": str(self.relay)})
        env.start()
        self.addCleanup(env.stop)
        for key in ("AGENT_RELAY_LEAD", "AGENT_RELAY_WORKERS", "ANTIGRAVITY_PROJECT_ID", "ANTIGRAVITY_AGENTAPI_EXE"):
            os.environ.pop(key, None)
        self.now = 1_000_000.0

    def register_project(self, pid="proj-1", root=None):
        uri = "file:///" + str(root or self.root).replace("\\", "/").replace(":", "%3A", 1)
        (self.gemini / "config" / "projects" / (pid + ".json")).write_text(json.dumps(
            {"name": "p", "projectResources": {"resources": [{"gitFolder": {"folderUri": uri}}]}}), encoding="utf-8")

    def transcript(self, *records, conversation=CONVERSATION):
        path = agy.transcript_path(conversation, self.gemini)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("ab") as stream:
            for record in records:
                stream.write(json.dumps(record, ensure_ascii=False).encode("utf-8") + b"\n")

    def observe(self, event, at, conversation=CONVERSATION, **fields):
        spool = agy.observation_dir(self.relay) / (conversation + ".jsonl")
        spool.parent.mkdir(parents=True, exist_ok=True)
        with spool.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"at": at, "event": event, **fields}) + "\n")

    def collector(self, marker="[relay abcd1234]"):
        return agy.TranscriptCollector(CONVERSATION, marker, self.relay, root=self.root, gemini=self.gemini,
                                       clock=lambda: self.now)


class UserJsonTests(Base):
    def test_preserves_other_keys_crlf_and_backs_up(self):
        path = self.gemini / "config" / "config.json"
        original = b'{\r\n  "sidecars": {\r\n    "old": {\r\n      "enabled": false\r\n    }\r\n  },\r\n  "userSettings": {"a": 1}\r\n}'
        path.write_bytes(original)
        backup = agy.update_user_json(path, lambda v: v["sidecars"].update(new={"enabled": True}), self.relay / "b")
        self.assertEqual(Path(backup).read_bytes(), original)
        raw = path.read_bytes()
        self.assertIn(b"\r\n", raw)
        self.assertFalse(raw.endswith(b"\n"))
        value = json.loads(raw)
        self.assertEqual(value["userSettings"], {"a": 1})
        self.assertEqual(set(value["sidecars"]), {"old", "new"})

    def test_concurrent_change_is_refused(self):
        path = self.gemini / "config" / "config.json"
        path.write_text("{}", encoding="utf-8")

        def change(value):
            path.write_text('{"someone": "else"}', encoding="utf-8")
            value["x"] = 1

        with self.assertRaises(AdapterError):
            agy.update_user_json(path, change, self.relay / "b")
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"someone": "else"})


class ProjectAndHookConfigTests(Base):
    def test_project_must_match_exactly_once(self):
        with self.assertRaises(AdapterUnavailable):
            agy.resolve_project(self.root, self.gemini)
        self.register_project()
        self.assertEqual(agy.resolve_project(self.root, self.gemini), "proj-1")
        self.register_project("proj-2")
        with self.assertRaises(AdapterUnavailable):
            agy.resolve_project(self.root, self.gemini)

    def test_other_stop_hooks_counts_everything_but_relay(self):
        hooks_file = self.gemini / "config" / "hooks.json"
        hooks_file.write_text(json.dumps({
            agy.HOOK_GROUP: {"Stop": [{"command": "relay"}]},
            "flat": {"Stop": [{"command": "a", "timeout": 12}]},
            "nested": {"Stop": [{"matcher": "x", "hooks": [{"command": "b", "timeout": 45}]}]},
            "disabled": {"enabled": False, "Stop": [{"command": "c", "timeout": 99}]},
            "pre": {"PreToolUse": [{"command": "d"}]}}), encoding="utf-8")
        self.assertEqual(agy.other_stop_hooks(self.root, self.gemini), (2, 45))
        (self.root / ".agents").mkdir()
        (self.root / ".agents" / "hooks.json").write_text("{broken", encoding="utf-8")
        self.assertEqual(agy.other_stop_hooks(self.root, self.gemini), (3, 45))

    def test_install_and_uninstall_touch_only_relay_group(self):
        path = self.gemini / "config" / "hooks.json"
        path.write_text(json.dumps({"mine": {"Stop": [{"command": "x"}]}}), encoding="utf-8")
        with patch.object(agy, "hook_command", side_effect=lambda event, home: "py relay " + event):
            first = agy.install_hooks(self.relay)
            second = agy.install_hooks(self.relay)
        self.assertTrue(first["changed"])
        self.assertFalse(second["changed"])
        value = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(value["mine"], {"Stop": [{"command": "x"}]})
        self.assertEqual(value[agy.HOOK_GROUP]["Stop"][0]["command"], "py relay Stop")
        agy.uninstall_hooks(self.relay)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"mine": {"Stop": [{"command": "x"}]}})

    def test_hook_command_refuses_unquotable_paths(self):
        with patch.object(agy, "_short_path", side_effect=str), \
                patch.object(agy.sys, "executable", "C:/has space/python.exe"):
            with self.assertRaises(AdapterUnavailable):
                agy.hook_command("Stop", self.relay)


class ObservationTests(Base):
    def test_only_relay_conversations_are_recorded(self):
        payload = {"conversationId": CONVERSATION, "fullyIdle": True, "executionNum": 0, "transcriptPath": "x"}
        self.assertEqual(hooks.process("antigravity", "Stop", payload, home=self.relay), {})
        self.assertFalse(agy.observation_dir(self.relay).exists())
        spool = agy.observation_dir(self.relay) / (CONVERSATION + ".jsonl")
        spool.parent.mkdir(parents=True)
        spool.touch()
        hooks.process("antigravity", "Stop", payload, home=self.relay)
        hooks.process("antigravity", "PreInvocation", {"conversationId": CONVERSATION, "invocationNum": 1}, home=self.relay)
        hooks.process("antigravity", "Stop", {"conversationId": "../escape"}, home=self.relay)
        records = [json.loads(line) for line in spool.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([r["event"] for r in records], ["Stop", "PreInvocation"])
        self.assertNotIn("transcriptPath", records[0])


class CompletionContractTests(Base):
    marker = "[relay abcd1234]"
    start = "2026-10-07T09:00:00Z"
    t0 = 1791363600.0  # epoch of start

    def test_marked_turn_completes_on_idle_stop_without_other_hooks(self):
        self.transcript(step(0, "USER_INPUT", self.marker + " do it", at=self.start),
                        step(1, "PLANNER_RESPONSE", tools=1), step(2, "GENERIC", "tool output"),
                        step(3, "PLANNER_RESPONSE", "답 한글"))
        follower = self.collector()
        self.assertIsNone(follower.poll())  # no Stop observed yet
        self.observe("Stop", self.t0 + 5, fullyIdle=True, executionNum=0, terminationReason="NO_TOOL_CALL", error="")
        result = follower.poll()
        self.assertEqual((result["outcome"], result["text"]), ("completed", "답 한글"))

    def test_send_message_marker_and_continuation_resume(self):
        self.transcript(step(0, "USER_INPUT", "earlier", at="2026-10-07T08:00:00Z"),
                        step(1, "PLANNER_RESPONSE", "old"),
                        message(2, self.marker + " next", at=self.start), step(3, "PLANNER_RESPONSE", "first"))
        self.observe("Stop", self.t0 - 3600, fullyIdle=True, executionNum=0)  # previous turn
        follower = self.collector()
        self.assertIsNone(follower.poll())
        self.observe("Stop", self.t0 + 3, fullyIdle=True, executionNum=0, terminationReason="NO_TOOL_CALL")
        # Another hook continued the loop: a PreInvocation follows the Stop.
        self.observe("PreInvocation", self.t0 + 3.2, invocationNum=1)
        self.transcript(blocked(4, "one more"), step(5, "PLANNER_RESPONSE", "second"))
        self.assertIsNone(follower.poll())
        self.observe("Stop", self.t0 + 9, fullyIdle=True, executionNum=1, terminationReason="NO_TOOL_CALL")
        self.assertEqual(follower.poll()["text"], "second")

    def test_other_stop_hooks_delay_confirmation_until_their_timeout(self):
        (self.gemini / "config" / "hooks.json").write_text(
            json.dumps({"other": {"Stop": [{"command": "x", "timeout": 20}]}}), encoding="utf-8")
        self.transcript(step(0, "USER_INPUT", self.marker, at=self.start), step(1, "PLANNER_RESPONSE", "done"))
        self.observe("Stop", self.t0 + 4, fullyIdle=True, executionNum=0, terminationReason="NO_TOOL_CALL")
        follower = self.collector()
        self.now = self.t0 + 4 + 20 + agy.CONFIRM_MARGIN - 0.1
        self.assertIsNone(follower.poll())
        self.now += 0.2
        result = follower.poll()
        self.assertEqual(result["text"], "done")
        self.assertEqual(result["confirmation"], {"other_stop_hooks": 1, "waited_for_timeout": 20})

    def test_background_task_notification_is_not_a_new_input(self):
        # Regression (4.3): a finished background command posts a [Message] from
        # sender=<conversation>/task-N inside the same turn.
        note = ("The following is a <SYSTEM_MESSAGE> not actually sent by the user.\n\n<SYSTEM_MESSAGE>\n"
                "[Message] timestamp=x sender=" + CONVERSATION + "/task-8 priority=MESSAGE_PRIORITY_HIGH "
                "content=Task id 8 finished, exit 0\n</SYSTEM_MESSAGE>")
        self.transcript(step(0, "USER_INPUT", self.marker, at=self.start), step(1, "PLANNER_RESPONSE", tools=1),
                        step(2, "SYSTEM_MESSAGE", note), step(3, "PLANNER_RESPONSE", "after the task"))
        self.observe("Stop", self.t0 + 9, fullyIdle=True, terminationReason="NO_TOOL_CALL")
        result = self.collector().poll()
        self.assertEqual((result["outcome"], result["text"]), ("completed", "after the task"))

    def test_late_collector_settles_turn_that_went_idle_before_next_input(self):
        # Regression (4.3 S1 replay): the next relay input is already in the
        # transcript when collection starts; the marked turn ended idle first.
        self.transcript(step(0, "USER_INPUT", self.marker, at=self.start), step(1, "PLANNER_RESPONSE", "S1 answer"),
                        message(2, "[relay ffff0000] next request", at="2026-10-07T09:00:10Z"),
                        step(3, "PLANNER_RESPONSE", "S2 answer"))
        self.observe("Stop", self.t0 + 3, fullyIdle=True, terminationReason="NO_TOOL_CALL")
        result = self.collector().poll()
        self.assertEqual((result["outcome"], result["text"]), ("completed", "S1 answer"))

    def test_input_injected_while_busy_still_fails(self):
        self.transcript(step(0, "USER_INPUT", self.marker, at=self.start), step(1, "PLANNER_RESPONSE", "partial"),
                        message(2, "injected", at="2026-10-07T09:00:02Z"), step(3, "PLANNER_RESPONSE", "mixed"))
        self.observe("Stop", self.t0 + 9, fullyIdle=True, terminationReason="NO_TOOL_CALL")
        self.assertEqual(self.collector().poll()["outcome"], "failed")

    def test_later_unmarked_input_fails_instead_of_collecting_it(self):
        self.transcript(step(0, "USER_INPUT", self.marker, at=self.start), step(1, "PLANNER_RESPONSE", tools=1),
                        message(2, "unmarked"), step(3, "PLANNER_RESPONSE", "not ours"))
        self.observe("Stop", self.t0 + 9, fullyIdle=True)
        result = self.collector().poll()
        self.assertEqual(result["outcome"], "failed")
        self.assertIn("later input", result["error"])

    def test_error_stop_without_reply_fails(self):
        self.transcript(step(0, "USER_INPUT", self.marker, at=self.start), step(1, "PLANNER_RESPONSE", tools=1),
                        step(2, "GENERIC", "boom", status="ERROR"))
        self.observe("Stop", self.t0 + 2, fullyIdle=True, terminationReason="error", error="model failed")
        result = self.collector().poll()
        self.assertEqual(result["outcome"], "failed")
        self.assertIn("model failed", result["error"])

    def test_busy_stop_is_not_completion(self):
        self.transcript(step(0, "USER_INPUT", self.marker, at=self.start), step(1, "PLANNER_RESPONSE", "done"))
        self.observe("Stop", self.t0 + 2, fullyIdle=False)
        self.assertIsNone(self.collector().poll())


class SidecarTests(Base):
    def job(self, args, project="proj-1"):
        folder = self.relay / "job"
        agy._write_json(folder / "job.json", {"request_id": "r", "project_id": project, "args": args})
        return folder

    def test_runs_once_with_executable_not_bat(self):
        folder = self.job(["send-message", "c", "line1\nline2 \"q\" %x% & y"])
        calls = []

        def fake_run(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, '{"ok":1}', "")

        with patch.dict(os.environ, {"ANTIGRAVITY_AGENTAPI_EXE": "C:/x/language_server.exe",
                                     "ANTIGRAVITY_PROJECT_ID": "proj-1"}), \
                patch.object(agy.subprocess, "run", side_effect=fake_run):
            agy.run_sidecar_job(folder / "job.json")
            agy.run_sidecar_job(folder / "job.json")  # a restart must not send again
        self.assertEqual(calls, [["C:/x/language_server.exe", "agentapi", "send-message", "c",
                                  "line1\nline2 \"q\" %x% & y"]])
        self.assertEqual(json.loads((folder / "result.json").read_text(encoding="utf-8"))["exit"], 0)

    def test_project_mismatch_is_recorded_not_sent(self):
        folder = self.job(["send-message", "c", "x"])
        with patch.dict(os.environ, {"ANTIGRAVITY_AGENTAPI_EXE": "a", "ANTIGRAVITY_PROJECT_ID": "other"}), \
                patch.object(agy.subprocess, "run") as run:
            agy.run_sidecar_job(folder / "job.json")
        run.assert_not_called()
        self.assertIn("different Antigravity project",
                      json.loads((folder / "result.json").read_text(encoding="utf-8"))["error"])


class DeliveryTests(Base):
    def setUp(self):
        super().setUp()
        self.register_project()
        config = self.gemini / "config" / "config.json"
        config.write_text(json.dumps({"sidecars": {"old": {"enabled": False, "projectId": "p"}},
                                      "userSettings": {"k": "v"}}), encoding="utf-8")
        self.config = config
        self.state = ProjectState(self.root)
        self.request = {"id": "ab" * 16, "root": str(self.root), "marker": "[relay abababab]"}

    def fake_app(self, stdout):
        """Stand-in for Antigravity: run an enabled relay sidecar's job once."""
        def app():
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                sidecars = json.loads(self.config.read_text(encoding="utf-8")).get("sidecars", {})
                for name, entry in sidecars.items():
                    if name.startswith(agy.SIDECAR_PREFIX) and entry.get("enabled"):
                        spec = json.loads((self.gemini / "config" / "sidecars" / name / "sidecar.json").read_text(encoding="utf-8"))
                        job = spec["args"][spec["args"].index("--job") + 1]
                        with patch.dict(os.environ, {"ANTIGRAVITY_AGENTAPI_EXE": "agentapi.exe",
                                                     "ANTIGRAVITY_PROJECT_ID": entry["projectId"]}), \
                                patch.object(agy.subprocess, "run",
                                             return_value=subprocess.CompletedProcess([], 0, stdout, "")):
                            agy.run_sidecar_job(job)
                        return
                time.sleep(0.05)
        thread = threading.Thread(target=app)
        thread.start()
        self.addCleanup(thread.join)

    def test_first_send_creates_conversation_and_cleans_up(self):
        self.fake_app(json.dumps({"response": {"newConversation": {"conversationId": CONVERSATION}}}))
        adapter = agy.AntigravityAdapter(delivery_timeout=10, gemini=self.gemini)
        delivery = adapter.deliver("pending-x", {"body": "[relay abababab] hi"}, state=self.state,
                                   request=self.request, title="My task")
        self.assertEqual(delivery, {"accepted": True, "returncode": 0, "thread": CONVERSATION})
        job = json.loads((self.state.path / "antigravity" / self.request["id"] / "job.json").read_text(encoding="utf-8"))
        self.assertEqual(job["args"][:3], ["new-conversation", "--title=My task", "--model=flash"])
        value = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(value, {"sidecars": {"old": {"enabled": False, "projectId": "p"}}, "userSettings": {"k": "v"}})
        self.assertFalse((self.gemini / "config" / "sidecars" / (agy.SIDECAR_PREFIX + self.request["id"][:12])).exists())
        self.assertTrue((agy.observation_dir(self.state.home) / (CONVERSATION + ".jsonl")).exists())
        self.assertTrue(any((self.state.home / "antigravity" / "backups").iterdir()))

    def test_unrun_sidecar_is_unknown_and_disabled(self):
        adapter = agy.AntigravityAdapter(delivery_timeout=0.3, gemini=self.gemini)
        delivery = adapter.deliver(CONVERSATION, {"body": "x"}, state=self.state, request=self.request)
        self.assertTrue(delivery["unknown"])
        entry = json.loads(self.config.read_text(encoding="utf-8"))["sidecars"][agy.SIDECAR_PREFIX + self.request["id"][:12]]
        self.assertFalse(entry["enabled"])

    def test_read_only_sandbox_is_refused(self):
        with self.assertRaises(AdapterUnavailable):
            agy.AntigravityAdapter(gemini=self.gemini).new_thread(self.root, "n", "read-only")


class CliRebindTests(Base):
    class Fake:
        needs_context = True
        name = "antigravity"

        def __init__(self):
            self.calls = []

        def new_thread(self, root, name, sandbox, open_app=True):
            return "pending-1"

        def deliver(self, thread, mail, state=None, request=None, title=None):
            self.calls.append((thread, title))
            return {"accepted": True, "returncode": 0, **({"thread": CONVERSATION} if thread == "pending-1" else {})}

    def invoke(self, *args, root=True):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main([*args, *(["--root", str(self.root)] if root else [])])
        return code, out.getvalue(), err.getvalue()

    def test_pending_handle_binds_to_conversation(self):
        (self.relay).mkdir(parents=True, exist_ok=True)
        (self.relay / "config.json").write_text('{"agents":{"antigravity":{"enabled":true}}}', encoding="utf-8")
        fake = self.Fake()
        with patch.object(cli, "get_adapter", return_value=fake), \
                patch.object(cli.collector, "spawn", return_value={"pid": 0}):
            code, out, err = self.invoke("new", "--worker", "antigravity", "--cwd", str(self.root), "--name", "작업", root=False)
            self.assertEqual((code, out.strip()), (0, "antigravity:pending-1"), err)
            code, out, err = self.invoke("send", "--to", "antigravity:pending-1", "--text", "go", "--no-wait")
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out)["to"], "antigravity:" + CONVERSATION)
            # The provisional handle now routes to the bound conversation and its open request.
            code, out, err = self.invoke("send", "--to", "antigravity:pending-1", "--text", "again", "--no-wait")
        self.assertEqual(code, 4)
        self.assertIn("already has an open request", err)
        self.assertEqual(fake.calls, [("pending-1", "작업")])
        # Completion after rebinding acknowledges the envelope at its original address
        # (regression: 3.3 send exited 1 with "message belongs to a different recipient").
        state = ProjectState(self.root)
        request = state.requests()[0]
        with patch.object(cli, "get_adapter", return_value=type("A", (), {
                "fallback_collect": lambda self, request, timeout=0: {"outcome": "completed", "text": "답"}})()):
            code, out, err = self.invoke("wait", "--request", request["id"])
        self.assertEqual((code, out.strip()), (0, "답"), err)
        self.assertEqual(state.load_request(request["id"])["status"], "completed")
        from agent_relay import inbox
        self.assertEqual(inbox.pending(state.path / "inbox", "antigravity:pending-1"), [])
        self.assertEqual([m["body"] for m in inbox.pending(state.path / "inbox", "claude:lead")], ["답"])


class CleanupTests(Base):
    def add_sidecar(self, request_id, status=None, enabled=False):
        state = ProjectState(self.root)
        folder = state.path / 'antigravity' / request_id
        agy._write_json(folder / 'job.json', {'request_id': request_id})
        if status:
            state.save_request({'id': request_id, 'status': status})
        name = agy.SIDECAR_PREFIX + request_id[:12]
        agy._write_json(self.gemini / 'config' / 'sidecars' / name / 'sidecar.json', {
            'description': 'agent-relay: one agentapi call',
            'args': [str(agy.ENTRY_SCRIPT), 'antigravity-sidecar', '--job', str(folder / 'job.json')]})
        path = self.gemini / 'config' / 'config.json'
        value = agy._read_json(path, {'other': 7, 'sidecars': {}})
        value['sidecars'][name] = {'enabled': enabled}
        agy._write_json(path, value)
        return name

    def test_module_sidecar_is_owned_and_cleanup_preserves_open_requests(self):
        closed = self.add_sidecar('1' * 32, 'completed')
        opened = self.add_sidecar('2' * 32, 'accepted')
        for name in (closed, opened):
            manifest = self.gemini / 'config' / 'sidecars' / name / 'sidecar.json'
            value = agy._read_json(manifest)
            value['args'] = ['-m', 'agent_relay', *value['args'][1:]]
            agy._write_json(manifest, value)
        result = agy.cleanup_sidecars(self.relay, True, self.gemini)
        self.assertEqual(result['candidates'], [closed])

    def test_preserves_active_open_foreign_and_backs_up_removed(self):
        removed = self.add_sidecar('a' * 32, 'completed')
        missing = self.add_sidecar('b' * 32)
        active = self.add_sidecar('c' * 32, 'completed', True)
        opened = self.add_sidecar('d' * 32, 'delivery_unknown')
        foreign = self.add_sidecar('e' * 32, 'completed')
        manifest = self.gemini / 'config' / 'sidecars' / foreign / 'sidecar.json'
        agy._write_json(manifest, {'description': 'unrelated', 'args': []})
        path = self.gemini / 'config' / 'config.json'
        before = path.read_bytes()
        dry = agy.cleanup_sidecars(self.relay, True, self.gemini)
        self.assertEqual(set(dry['candidates']), {removed, missing})
        self.assertEqual(path.read_bytes(), before)
        result = agy.cleanup_sidecars(self.relay, home=self.gemini)
        self.assertEqual(Path(result['backup']).read_bytes(), before)
        self.assertTrue((Path(result['folder_backup']) / removed / 'sidecar.json').exists())
        value = agy._read_json(path)
        self.assertEqual(set(value['sidecars']), {active, opened, foreign})
        self.assertEqual(value['other'], 7)
        self.assertFalse((self.gemini / 'config' / 'sidecars' / removed).exists())

    def test_concurrent_config_refusal_keeps_everything(self):
        name = self.add_sidecar('f' * 32, 'completed')
        path = self.gemini / 'config' / 'config.json'
        self.assertEqual(agy.cleanup_sidecars(self.relay, True, self.gemini)['count'], 1)
        original = agy.update_user_json
        def race(path, change, backup):
            agy._write_json(path, {'sidecars': {name: {'enabled': True}}, 'concurrent': True})
            return original(path, change, backup)
        with patch.object(agy, 'update_user_json', side_effect=race):
            with self.assertRaisesRegex(AdapterError, 'concurrently'):
                agy.cleanup_sidecars(self.relay, home=self.gemini)
        self.assertTrue(agy._read_json(path)['concurrent'])
        self.assertTrue((self.gemini / 'config' / 'sidecars' / name).exists())

    def test_unrecognized_relay_named_entry_is_kept(self):
        # Only sidecars whose manifest runs this relay's antigravity-sidecar job qualify.
        name = agy.SIDECAR_PREFIX + 'experiment'
        agy._write_json(self.gemini / 'config' / 'sidecars' / name / 'sidecar.json',
                        {'description': 'agent-relay experiment', 'args': ['probe.py', 'serve']})
        agy._write_json(self.gemini / 'config' / 'config.json', {'sidecars': {name: {'enabled': False}}})
        self.assertEqual(agy.cleanup_sidecars(self.relay, True, self.gemini)['count'], 0)

if __name__ == "__main__":
    unittest.main()
