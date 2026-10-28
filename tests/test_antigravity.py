"""Antigravity adapter: settings safety, completion contract, sidecar delivery."""
import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from handback import cli, collector, config, hooks, inbox, router
from handback.adapters import antigravity as agy
from handback.adapters.base import AdapterError, AdapterUnavailable
from handback.state import ProjectState

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
        env = patch.dict(os.environ, {"GEMINI_HOME": str(self.gemini), "HANDBACK_HOME": str(self.relay)})
        env.start()
        self.addCleanup(env.stop)
        for key in ("HANDBACK_LEAD", "HANDBACK_WORKERS", "ANTIGRAVITY_PROJECT_ID", "ANTIGRAVITY_AGENTAPI_EXE",
                    "CLAUDE_CODE_SESSION_ID"):
            os.environ.pop(key, None)
        self.now = 1_000_000.0

    def register_project(self, pid="proj-1", root=None, name="p"):
        path = str(root or self.root).replace("\\", "/").replace(":", "%3A", 1)
        uri = "file://" + ("" if path.startswith("/") else "/") + path
        (self.gemini / "config" / "projects" / (pid + ".json")).write_text(json.dumps(
            {"name": name, "projectResources": {"resources": [{"gitFolder": {"folderUri": uri}}]}}), encoding="utf-8")

    def conversations(self, *project_ids):
        """Antigravity's conversation list, one conversation in each given project."""
        path = self.gemini / "antigravity" / "conversation_summaries.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("CREATE TABLE IF NOT EXISTS conversation_summaries "
                               "(conversation_id TEXT PRIMARY KEY, project_id TEXT)")
            connection.executemany("INSERT INTO conversation_summaries VALUES (?, ?)",
                                   [(f"c-{pid}", pid) for pid in project_ids])

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

    def test_folder_uri_keeps_a_posix_root(self):
        self.assertEqual(agy._uri_path("file:///home/me/x"), "/home/me/x")
        with patch.object(agy.os, "name", "nt"):
            self.assertEqual(agy._uri_path("file:///C%3A/Users/me/x"), "C:/Users/me/x")
        with patch.object(agy.os, "name", "posix"):
            self.assertEqual(agy._uri_path("file:///a%3Awork/x"), "/a:work/x")

    def test_sidecar_managed_project_is_not_a_registration(self):
        # Antigravity 2.23 copies the target project for each sidecar (issue #4).
        self.register_project()
        self.register_project("managed", name=agy.SIDECAR_PREFIX + "83089e491ed3")
        self.register_project("named", name=agy.sidecar_name("2650df801dea" * 2, "Study"))
        self.assertEqual(agy.resolve_project(self.root, self.gemini), "proj-1")

    def test_sidecar_name_carries_the_registered_project(self):
        # The app names the project copy after the sidecar, so the copy should say whose it is (issue #5).
        request_id = "2650df801dea" + "0" * 20
        cases = {"Study": "handback-study-2650df801dea",
                 "My App (v2)": "handback-my-app-v2-2650df801dea",
                 "공부": "handback-2650df801dea",
                 None: "handback-2650df801dea",
                 "x" * 40 + "-y": "handback-" + "x" * agy.PROJECT_LABEL_LIMIT + "-2650df801dea"}
        for registered, expected in cases.items():
            name = agy.sidecar_name(request_id, registered)
            self.assertEqual(name, expected)
            self.assertTrue(agy._sidecar_managed({"name": name}), name)
        self.assertFalse(agy._sidecar_managed({"name": "handback-notes"}))

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
    def pending_request(self, **changes):
        state = ProjectState(self.root)
        request = {"id": "ab" * 16, "root": str(self.root), "marker": "[relay abababab]",
                   "agent": "antigravity", "thread": "pending-x", "handle": "antigravity:pending-x",
                   "status": "dispatching", **changes}
        state.save_request(request)
        agy._write_json(state.path / "antigravity" / request["id"] / "job.json", {
            "request_id": request["id"],
            "args": ["new-conversation", "--title=test", "--model=flash", request["marker"] + " go"]})
        return state, request

    def test_early_preinvocation_and_stop_keep_other_hook_confirmation_delay(self):
        state, request = self.pending_request()
        self.transcript(step(0, "USER_INPUT", request["marker"]), step(1, "PLANNER_RESPONSE", "done"))
        agy._write_json(self.gemini / "config/hooks.json", {
            "other": {"Stop": [{"command": "other", "timeout": 20}]}})
        stop_at = CompletionContractTests.t0 + 5
        with patch.object(router, "spawn_external") as spawn, patch.object(agy.time, "time", return_value=stop_at):
            hooks.process("antigravity", "PreInvocation", {"conversationId": CONVERSATION}, home=self.relay)
            hooks.process("antigravity", "Stop", {"conversationId": CONVERSATION, "fullyIdle": True}, home=self.relay)
        self.assertEqual(spawn.call_count, 1)
        self.assertEqual([r["event"] for r in agy._observations(self.relay, CONVERSATION)], ["PreInvocation", "Stop"])
        self.assertEqual(state.load_request(request["id"])["thread"], CONVERSATION)
        follower = self.collector(request["marker"])
        self.now = stop_at + 21
        self.assertIsNone(follower.poll())
        self.now = stop_at + 23
        self.assertEqual(follower.poll()["text"], "done")

    def test_unowned_early_observations_do_not_bind_or_create_spool(self):
        for case in ("unmarked", "assistant-marker", "no-job", "closed", "disabled"):
            with self.subTest(case=case):
                state, request = self.pending_request(status="completed" if case == "closed" else "dispatching")
                conversation = "unowned-" + case
                kind = "PLANNER_RESPONSE" if case == "assistant-marker" else "USER_INPUT"
                content = "direct user input" if case == "unmarked" else request["marker"]
                self.transcript(step(0, kind, content), conversation=conversation)
                if case == "no-job":
                    (state.path / "antigravity" / request["id"] / "job.json").unlink()
                if case == "disabled":
                    agy._write_json(self.relay / "config.json", {"hooks": {"antigravity": {"enabled": False}}})
                with patch.object(router, "spawn_external") as spawn:
                    hooks.process("antigravity", "Stop", {"conversationId": conversation, "fullyIdle": True}, home=self.relay)
                spawn.assert_not_called()
                self.assertFalse((agy.observation_dir(self.relay) / (conversation + ".jsonl")).exists())
                self.assertEqual(state.load_request(request["id"])["thread"], "pending-x")

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
        agy._write_json(folder / "job.json", {"request_id": "r", "project_id": project, "args": args,
                                              "sidecar": agy.SIDECAR_PREFIX + "0123456789ab",
                                              "root": str(self.root), "gemini": str(self.gemini)})
        return folder

    def test_runs_once_with_executable_not_bat(self):
        folder = self.job(["send-message", "c", "line1\nline2 \"q\" %x% & y"])
        calls, projects = [], []

        def fake_run(command, env=None, **kwargs):
            calls.append(command)
            projects.append(env["ANTIGRAVITY_PROJECT_ID"])
            return subprocess.CompletedProcess(command, 0, '{"ok":1}', "")

        with patch.dict(os.environ, {"ANTIGRAVITY_AGENTAPI_EXE": "C:/x/language_server.exe",
                                     "ANTIGRAVITY_PROJECT_ID": "proj-1"}), \
                patch.object(agy.subprocess, "run", side_effect=fake_run):
            agy.run_sidecar_job(folder / "job.json")
            agy.run_sidecar_job(folder / "job.json")  # a restart must not send again
        self.assertEqual(calls, [["C:/x/language_server.exe", "agentapi", "send-message", "c",
                                  "line1\nline2 \"q\" %x% & y"]])
        self.assertEqual(json.loads((folder / "result.json").read_text(encoding="utf-8"))["exit"], 0)
        self.assertEqual(projects, ["proj-1"])

    def test_agentapi_is_pointed_at_the_registered_project_from_the_copy(self):
        # Antigravity 2.23+ runs the sidecar in its copy; agentapi still gets the registered project (issue #5).
        self.register_project("managed", name=agy.SIDECAR_PREFIX + "0123456789ab")
        folder = self.job(["new-conversation", "x"])
        with patch.dict(os.environ, {"ANTIGRAVITY_AGENTAPI_EXE": "a", "ANTIGRAVITY_PROJECT_ID": "managed"}), \
                patch.object(agy.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "{}", "")) as run:
            agy.run_sidecar_job(folder / "job.json")
        self.assertEqual(run.call_args.kwargs["env"]["ANTIGRAVITY_PROJECT_ID"], "proj-1")

    def test_project_mismatch_is_recorded_not_sent(self):
        folder = self.job(["send-message", "c", "x"])
        with patch.dict(os.environ, {"ANTIGRAVITY_AGENTAPI_EXE": "a", "ANTIGRAVITY_PROJECT_ID": "other"}), \
                patch.object(agy.subprocess, "run") as run:
            agy.run_sidecar_job(folder / "job.json")
        run.assert_not_called()
        result = json.loads((folder / "result.json").read_text(encoding="utf-8"))
        self.assertIn("different Antigravity project", result["error"])
        self.assertIs(result["invoked"], False)

    def run_in(self, project_id):
        folder = self.job(["send-message", "c", "x"])
        (folder / "attempted").unlink(missing_ok=True)
        with patch.dict(os.environ, {"ANTIGRAVITY_AGENTAPI_EXE": "a", "ANTIGRAVITY_PROJECT_ID": project_id}), \
                patch.object(agy.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "{}", "")) as run:
            agy.run_sidecar_job(folder / "job.json")
        return run.called

    def test_project_made_for_this_sidecar_is_accepted(self):
        self.register_project("managed", name=agy.SIDECAR_PREFIX + "0123456789ab")
        self.assertTrue(self.run_in("managed"))

    def test_project_made_for_another_sidecar_or_folder_is_refused(self):
        self.register_project("other-sidecar", name=agy.SIDECAR_PREFIX + "ffffffffffff")
        self.assertFalse(self.run_in("other-sidecar"))
        self.register_project("other-folder", root=Path(self.temp.name) / "elsewhere",
                              name=agy.SIDECAR_PREFIX + "0123456789ab")
        self.assertFalse(self.run_in("other-folder"))


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

    def fake_app(self, stdout, before_result=None, managed=False, agentapi=True, place=None):
        """Stand-in for Antigravity: run an enabled relay sidecar's job once.

        managed=True mimics 2.23+, which runs the sidecar in a copy of the project named after it.
        place lists the new conversation like the app does: "env" under the project agentapi was
        given, "sidecar" under the project the sidecar runs in; None leaves the list unreadable."""
        def app():
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                sidecars = json.loads(self.config.read_text(encoding="utf-8")).get("sidecars", {})
                for name, entry in sidecars.items():
                    if name.startswith(agy.SIDECAR_PREFIX) and entry.get("enabled"):
                        spec = json.loads((self.gemini / "config" / "sidecars" / name / "sidecar.json").read_text(encoding="utf-8"))
                        job = spec["args"][spec["args"].index("--job") + 1]
                        if before_result:
                            before_result()
                        project = entry["projectId"]
                        if managed:
                            project = "managed-" + name
                            self.register_project(project, name=name)
                        exe = {"ANTIGRAVITY_AGENTAPI_EXE": "agentapi.exe"} if agentapi else {}

                        def agentapi_call(command, env=None, sidecar_project=project, **kwargs):
                            if place:
                                self.conversations()
                                owner = env["ANTIGRAVITY_PROJECT_ID"] if place == "env" else sidecar_project
                                database = self.gemini / "antigravity" / "conversation_summaries.db"
                                with contextlib.closing(sqlite3.connect(database)) as connection, connection:
                                    connection.execute("INSERT INTO conversation_summaries VALUES (?, ?)",
                                                       (CONVERSATION, owner))
                            return subprocess.CompletedProcess([], 0, stdout, "")

                        with patch.dict(os.environ, {"ANTIGRAVITY_PROJECT_ID": project, **exe}), \
                                patch.object(agy.shutil, "which", return_value=None), \
                                patch.object(agy.subprocess, "run", side_effect=agentapi_call):
                            agy.run_sidecar_job(job)
                        return
                time.sleep(0.05)
        thread = threading.Thread(target=app)
        thread.start()
        self.addCleanup(thread.join)

    def test_first_idle_stop_before_acceptance_is_collected_once(self):
        agy._write_json(self.relay / "config.json", {"agents": {"antigravity": {"enabled": True}}})
        config.use_topology(self.root, "codex:lead-thread", ["antigravity"], home=self.relay)
        observed = []

        def early_reply():
            request = self.state.requests()[0]
            self.transcript(step(0, "USER_INPUT", request["marker"] + " go"),
                            step(1, "PLANNER_RESPONSE", "RELAY_OK"))
            with patch.object(router, "spawn_external") as spawn:
                hooks.process("antigravity", "Stop", {
                    "conversationId": CONVERSATION, "fullyIdle": True,
                    "executionNum": 0, "terminationReason": "NO_TOOL_CALL"}, home=self.relay)
                observed.append((self.state.load_request(request["id"]), spawn.call_count))

        self.fake_app(json.dumps({"response": {"newConversation": {"conversationId": CONVERSATION}}}), early_reply)
        adapter = agy.AntigravityAdapter(delivery_timeout=10, gemini=self.gemini)
        with patch.object(cli, "get_adapter", return_value=adapter):
            request, code = cli.submit(self.state, config.resolve(self.root),
                                       "antigravity:pending-x", "go", "Lead")
        self.assertEqual(code, 0)
        result = adapter.incremental_collector(request, self.relay).poll()
        self.assertIsNotNone(result, "the first fully idle Stop was lost before agentapi acceptance")
        self.assertEqual((result["outcome"], result["text"]), ("completed", "RELAY_OK"))
        self.assertEqual(len(agy._observations(self.relay, CONVERSATION)), 1)
        early, spawns = observed[0]
        self.assertEqual((early["thread"], spawns), (CONVERSATION, 1))
        self.assertEqual(request["provisional_handle"], "antigravity:pending-x")
        self.assertEqual(self.state.threads()["antigravity:pending-x"]["bound_to"], "antigravity:" + CONVERSATION)
        collector._finish_request(self.state, request, result)
        collector._finish_request(self.state, request, result)
        self.assertEqual(len(inbox.pending(self.state.path / "inbox", "codex:lead-thread")), 1)
        self.assertEqual(inbox.pending(self.state.path / "inbox", "antigravity:pending-x"), [])

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
        self.assertFalse((self.gemini / "config" / "sidecars" / "handback-p-abababababab").exists())
        self.assertTrue((agy.observation_dir(self.state.home) / (CONVERSATION + ".jsonl")).exists())
        self.assertTrue(any((self.state.home / "antigravity" / "backups").iterdir()))

    def test_known_conversation_is_observed_before_sidecar_acceptance(self):
        def early_reply():
            self.transcript(message(0, self.request["marker"]), step(1, "PLANNER_RESPONSE", "RELAY_OK"))
            agy.record_observation(self.relay, {"conversationId": CONVERSATION, "fullyIdle": True}, "Stop")

        self.fake_app('{"ok": true}', early_reply)
        adapter = agy.AntigravityAdapter(delivery_timeout=10, gemini=self.gemini)
        self.assertTrue(adapter.deliver(CONVERSATION, {"body": self.request["marker"]},
                                       state=self.state, request=self.request)["accepted"])
        self.assertEqual(self.collector(self.request["marker"]).poll()["text"], "RELAY_OK")
        self.assertEqual(len(agy._observations(self.relay, CONVERSATION)), 1)

    def deliver_new(self, **app):
        self.fake_app(json.dumps({"response": {"newConversation": {"conversationId": CONVERSATION}}}), **app)
        adapter = agy.AntigravityAdapter(delivery_timeout=10, gemini=self.gemini)
        delivery = adapter.deliver("pending-x", {"body": "[relay abababab] hi"}, state=self.state,
                                   request=self.request)
        projects = {p.stem: json.loads(p.read_text(encoding="utf-8"))["name"]
                    for p in (self.gemini / "config" / "projects").glob("*.json")}
        return delivery, projects

    def test_new_conversation_lands_in_the_registered_project_and_the_copy_goes(self):
        # Like Codex, the conversation is listed under the registered project, not the app's copy (issue #5).
        delivery, projects = self.deliver_new(managed=True, place="env")
        self.assertEqual(delivery, {"accepted": True, "returncode": 0, "thread": CONVERSATION})
        self.assertEqual(agy._conversation_project(self.gemini, CONVERSATION), "proj-1")
        self.assertEqual(projects, {"proj-1": "p"})
        self.assertTrue(any(path.name.startswith("managed-") for path in
                            (self.state.home / "antigravity" / "backups").iterdir()))

    def test_conversation_kept_in_the_copy_keeps_that_project_and_names_it(self):
        # Deleting the project a conversation belongs to hides that conversation in the app.
        delivery, projects = self.deliver_new(managed=True, place="sidecar")
        self.assertEqual(delivery, {"accepted": True, "returncode": 0, "thread": CONVERSATION,
                                    "project": {"name": "handback-p-abababababab", "copy_of": "p"}})
        self.assertEqual(sorted(projects.values()), ["handback-p-abababababab", "p"])

    def test_copy_stays_when_the_conversation_list_is_unreadable(self):
        delivery, projects = self.deliver_new(managed=True)
        self.assertNotIn("project", delivery)
        self.assertEqual(sorted(projects.values()), ["handback-p-abababababab", "p"])

    def test_known_conversation_delivery_names_no_project(self):
        self.fake_app('{"ok": true}')
        adapter = agy.AntigravityAdapter(delivery_timeout=10, gemini=self.gemini)
        delivery = adapter.deliver(CONVERSATION, {"body": "x"}, state=self.state, request=self.request)
        self.assertEqual(delivery, {"accepted": True, "returncode": 0})

    def test_project_name_is_none_when_unreadable(self):
        self.assertEqual(agy.project_name("proj-1", self.gemini), "p")
        self.assertIsNone(agy.project_name("absent", self.gemini))

    def test_failure_before_agentapi_is_send_failed_not_unknown(self):
        self.fake_app("{}", agentapi=False)
        adapter = agy.AntigravityAdapter(delivery_timeout=10, gemini=self.gemini)
        delivery = adapter.deliver(CONVERSATION, {"body": "x"}, state=self.state, request=self.request)
        self.assertFalse(delivery["accepted"])
        self.assertNotIn("unknown", delivery)
        self.assertIn("agentapi is not available", delivery["stderr"])

    def test_unrun_sidecar_is_unknown_and_disabled(self):
        adapter = agy.AntigravityAdapter(delivery_timeout=0.3, gemini=self.gemini)
        delivery = adapter.deliver(CONVERSATION, {"body": "x"}, state=self.state, request=self.request)
        self.assertTrue(delivery["unknown"])
        entry = json.loads(self.config.read_text(encoding="utf-8"))["sidecars"]["handback-p-abababababab"]
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
        from handback import inbox
        self.assertEqual(inbox.pending(state.path / "inbox", "antigravity:pending-1"), [])
        self.assertEqual([m["body"] for m in inbox.pending(state.path / "inbox", "claude:lead")], ["답"])


class CleanupTests(Base):
    def add_sidecar(self, request_id, status=None, enabled=False, registered=None):
        state = ProjectState(self.root)
        folder = state.path / 'antigravity' / request_id
        name = agy.sidecar_name(request_id, registered)
        agy._write_json(folder / 'job.json', {'request_id': request_id, 'sidecar': name})
        if status:
            state.save_request({'id': request_id, 'status': status})
        agy._write_json(self.gemini / 'config' / 'sidecars' / name / 'sidecar.json', {
            'description': 'handback: one agentapi call',
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
            value['args'] = ['-m', 'handback', *value['args'][1:]]
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

    def test_named_sidecar_is_owned_only_under_its_recorded_name(self):
        named = self.add_sidecar('4' * 32, 'completed', registered='Study')
        self.assertEqual(named, 'handback-study-444444444444')
        self.add_sidecar('5' * 32, 'completed', registered='Study')
        # A key that is not the one recorded for its request is not provably ours.
        job = ProjectState(self.root).path / 'antigravity' / ('5' * 32) / 'job.json'
        agy._write_json(job, {'request_id': '5' * 32, 'sidecar': 'handback-other-555555555555'})
        self.assertEqual(agy.cleanup_sidecars(self.relay, True, self.gemini)['candidates'], [named])

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

    def test_orphaned_managed_projects_are_removed_with_backup(self):
        live = self.add_sidecar('3' * 32, 'accepted', True)
        self.register_project("orphan", name=agy.SIDECAR_PREFIX + "83089e491ed3")
        self.register_project("talked", name=agy.SIDECAR_PREFIX + "0123456789ab")
        self.register_project("live", name=live)
        self.register_project("user", name="handback-notes")
        self.conversations("talked", "proj-elsewhere")
        dry = agy.cleanup_sidecars(self.relay, True, self.gemini)
        self.assertEqual((dry["count"], dry["projects"]), (0, ["orphan"]))
        self.assertTrue((self.gemini / "config" / "projects" / "orphan.json").exists())
        result = agy.cleanup_sidecars(self.relay, home=self.gemini)
        self.assertEqual(result["removed_projects"], ["orphan"])
        left = sorted(p.stem for p in (self.gemini / "config" / "projects").glob("*.json"))
        self.assertEqual(left, ["live", "talked", "user"])
        backups = self.relay / "antigravity" / "backups"
        self.assertTrue(any(p.name.startswith("orphan.json.") for p in backups.iterdir()))

    def test_managed_projects_stay_when_conversations_cannot_be_read(self):
        self.register_project("orphan", name=agy.SIDECAR_PREFIX + "83089e491ed3")
        self.assertEqual(agy.cleanup_sidecars(self.relay, True, self.gemini)["projects"], [])
        database = self.gemini / "antigravity" / "conversation_summaries.db"
        database.parent.mkdir(parents=True)
        database.write_bytes(b"not a database")
        self.assertEqual(agy.cleanup_sidecars(self.relay, home=self.gemini)["projects"], [])
        self.assertTrue((self.gemini / "config" / "projects" / "orphan.json").exists())

    def test_unrecognized_relay_named_entry_is_kept(self):
        # Only sidecars whose manifest runs this relay's antigravity-sidecar job qualify.
        name = agy.SIDECAR_PREFIX + 'experiment'
        agy._write_json(self.gemini / 'config' / 'sidecars' / name / 'sidecar.json',
                        {'description': 'handback experiment', 'args': ['probe.py', 'serve']})
        agy._write_json(self.gemini / 'config' / 'config.json', {'sidecars': {name: {'enabled': False}}})
        self.assertEqual(agy.cleanup_sidecars(self.relay, True, self.gemini)['count'], 0)

if __name__ == "__main__":
    unittest.main()
