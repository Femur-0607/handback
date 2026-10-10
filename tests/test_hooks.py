"""Fail-open observers cannot claim completion or leak into another session."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from handback import envelope, hooks, inbox
from handback.state import ProjectState, atomic_json


class HookTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        # Fixtures are non-Git projects even when the tests live inside a checkout.
        ceiling = patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": str(self.directory)})
        ceiling.start()
        self.addCleanup(ceiling.stop)
        self.root = self.directory / "project"
        self.root.mkdir()
        self.home = self.directory / "state"
        self.state = ProjectState(self.root, home=self.home)
        self.request = {"id": "a" * 32, "handle": "codex:worker-session", "thread": "worker-session",
                        "agent": "codex", "root": str(self.root), "status": "accepted",
                        "marker": "[relay abcd1234]", "return_to": "claude:lead-session"}

    def seed_request(self, **changes):
        self.request.update(changes)
        self.state.save_request(self.request)
        return self.state.path / "requests" / (self.request["id"] + ".json")

    def submit(self, **changes):
        payload = {"session_id": "worker-session", "turn_id": "target-turn",
                   "prompt": self.request["marker"] + " Read this task"}
        payload.update(changes)
        return hooks.process("codex", "UserPromptSubmit", payload, home=self.home)

    def stop(self, **changes):
        payload = {"session_id": "worker-session", "turn_id": "target-turn",
                   "last_assistant_message": "한글 완료"}
        payload.update(changes)
        return hooks.process("codex", "Stop", payload, home=self.home)

    def seed_lead(self, lead="claude:lead-session"):
        self.state.write_json("topology.json", {"lead": lead, "workers": ["codex"], "root": str(self.root)})
        return inbox.put(self.state.path / "inbox", envelope.make("request", "codex:worker-session",
                                                                lead, "한글 반환 🐱", message_id="b" * 32))

    def recover(self, session="lead-session", event="SessionStart"):
        return hooks.process("claude", event, {"session_id": session}, home=self.home)

    def test_unmanaged_session_creates_no_state(self):
        self.assertIsNone(self.submit())
        self.assertIsNone(self.stop())
        self.assertIsNone(self.recover())
        self.assertFalse(self.home.exists())

    def test_unregistered_session_does_not_touch_registered_request(self):
        path = self.seed_request()
        before = path.read_bytes()
        self.submit(session_id="another-session")
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse((self.state.path / "log").exists())

    def test_no_marker_or_turn_does_not_bind(self):
        path = self.seed_request()
        before = path.read_bytes()
        for fields in ({"prompt": "direct user prompt"}, {"turn_id": None}, {"session_id": None}, {"prompt": None}):
            with self.subTest(fields=fields):
                self.assertIsNone(self.submit(**fields))
                self.assertEqual(path.read_bytes(), before)

    def test_valid_submit_binds_once_and_refuses_rebinding(self):
        path = self.seed_request()
        self.submit()
        bound = self.state.load_request(self.request["id"])
        self.assertEqual(bound["hook_turn_id"], "target-turn")
        self.assertEqual(bound["status"], "accepted")
        before = path.read_bytes()
        self.submit()
        self.submit(turn_id="replacement-turn")
        self.assertEqual(path.read_bytes(), before)

    def test_stop_is_diagnostic_only_and_exact_turn(self):
        path = self.seed_request(hook_turn_id="target-turn")
        before = path.read_bytes()
        self.stop(turn_id="user-turn")
        self.assertEqual(path.read_bytes(), before)
        self.stop()
        stored = self.state.load_request(self.request["id"])
        self.assertEqual(stored["hook_candidate"], {"text": "한글 완료", "outcome": "completed",
                                                    "turn": "target-turn", "event": "Stop"})
        self.assertEqual(stored["status"], "accepted")
        self.assertNotIn("result", stored)
        self.assertFalse((self.state.path / "inbox").exists())
        before = path.read_bytes()
        self.stop()
        self.assertEqual(path.read_bytes(), before)

    def test_stop_without_submit_never_becomes_candidate(self):
        path = self.seed_request()
        before = path.read_bytes()
        self.stop()
        self.assertEqual(path.read_bytes(), before)

    def test_plain_user_turn_is_not_rebound_or_returned(self):
        path = self.seed_request(hook_turn_id="target-turn")
        before = path.read_bytes()
        self.submit(prompt="Plain app input", turn_id="plain-turn")
        self.stop(turn_id="plain-turn", last_assistant_message="Do not return me")
        self.assertEqual(path.read_bytes(), before)

    def test_interrupt_records_failure_without_changing_request_status(self):
        self.seed_request(hook_turn_id="target-turn")
        hooks.process("codex", "interrupt", {"session_id": "worker-session", "turn_id": "target-turn"}, home=self.home)
        request = self.state.load_request(self.request["id"])
        self.assertEqual(request["status"], "accepted")
        self.assertEqual(request["hook_candidate"]["outcome"], "failed")
        self.assertEqual(request["hook_candidate"]["event"], "Interrupt")

    def test_completed_requests_are_not_modified(self):
        path = self.seed_request(status="completed", hook_turn_id="target-turn")
        before = path.read_bytes()
        self.submit()
        self.stop()
        self.assertEqual(path.read_bytes(), before)

    def test_saved_root_must_match_project_state_key(self):
        other = self.directory / "other"
        other.mkdir()
        path = self.seed_request(root=str(other))
        before = path.read_bytes()
        self.submit()
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse((self.state.path / "log").exists())

    def test_unicode_recovery_is_exact_session_and_never_acknowledges(self):
        mail = self.seed_lead()
        self.assertIsNone(self.recover("other-session"))
        for event in ("SessionStart", "UserPromptSubmit"):
            with self.subTest(event=event):
                output = self.recover(event=event)
                self.assertEqual(output["hookSpecificOutput"]["hookEventName"], event)
                self.assertIn("한글 반환 🐱", output["hookSpecificOutput"]["additionalContext"])
                self.assertIn("untrusted data", output["hookSpecificOutput"]["additionalContext"])
        self.assertEqual([item["id"] for item in inbox.pending(self.state.path / "inbox", "claude:lead-session")], [mail["id"]])
        self.assertFalse((self.state.path / "inbox/acks").exists())

    def watch_hint(self, session="lead-session", event="UserPromptSubmit"):
        output = self.recover(session, event)
        if output is None:
            return None
        lines = output["hookSpecificOutput"]["additionalContext"].splitlines()
        return next((line for line in lines if "no inbox watcher is running" in line), None)

    def test_open_request_without_live_watcher_asks_lead_to_arm_it(self):
        self.state.write_json("topology.json", {"lead": "claude:lead-session", "workers": ["codex"],
                                                "root": str(self.root)})
        self.assertIsNone(self.recover())  # no open work: stay silent
        self.seed_request(status="completed")
        self.assertIsNone(self.recover())
        self.seed_request(status="accepted")
        for event in ("SessionStart", "UserPromptSubmit"):
            with self.subTest(event=event):
                hint = self.watch_hint(event=event)
                self.assertIn("1 open request(s)", hint)
                self.assertIn("--for claude:lead-session", hint.replace("'", ""))
                self.assertIn("--idle-exit 1200", hint.replace("'", ""))
        self.assertIsNone(self.recover("other-session"))
        # Codex Leads never use Monitor.
        self.state.write_json("topology.json", {"lead": "codex:lead-session", "workers": ["antigravity"],
                                                "root": str(self.root)})
        self.seed_request(return_to="codex:lead-session")
        self.assertIsNone(hooks.process("codex", "UserPromptSubmit", {"session_id": "lead-session"},
                                        home=self.home))

    def test_fresh_watcher_lease_suppresses_the_hint_and_stale_one_does_not(self):
        from handback import watcher
        self.state.write_json("topology.json", {"lead": "claude:lead-session", "workers": ["codex"],
                                                "root": str(self.root)})
        self.seed_request(status="accepted")
        lease = watcher.lease_path(self.state.path, "claude:lead-session")
        atomic_json(lease, {"recipient": "claude:lead-session", "pid": 1, "started": time.time(),
                            "heartbeat": time.time()})
        self.assertIsNone(self.recover())
        atomic_json(lease, {"recipient": "claude:lead-session", "pid": 1, "started": 0,
                            "heartbeat": time.time() - watcher.STALE_SECONDS - 1})
        self.assertIsNotNone(self.watch_hint())
        lease.write_text("{broken", encoding="utf-8")
        self.assertIsNotNone(self.watch_hint())

    def test_hint_and_unread_mail_are_combined(self):
        self.seed_lead()
        self.seed_request(status="accepted")
        context = self.recover()["hookSpecificOutput"]["additionalContext"]
        self.assertIn("no inbox watcher is running", context)
        self.assertIn("한글 반환 🐱", context)

    def test_generic_lead_does_not_recover_into_arbitrary_session(self):
        self.seed_lead("claude:lead")
        self.assertIsNone(self.recover("lead-session"))

    def test_acknowledged_or_other_recipient_mail_is_not_recovered(self):
        mail = self.seed_lead()
        inbox.acknowledge(self.state.path / "inbox", mail["id"], mail["recipient"])
        inbox.put(self.state.path / "inbox", envelope.make("other", "codex:worker", "claude:someone-else", "private"))
        self.assertIsNone(self.recover())

    def test_disabled_user_or_project_settings_prevent_observation_and_recovery(self):
        self.seed_request()
        self.seed_lead()
        path = self.state.path / "requests" / (self.request["id"] + ".json")
        before = path.read_bytes()
        settings = ({"hooks": {"enabled": False}}, {"hooks": {"codex": {"enabled": False}, "claude": {"enabled": False}}},
                    {"agents": {"codex": {"enabled": False}, "claude": {"enabled": False}}})
        for configuration in settings:
            for config_path in (self.home / "config.json", self.root / ".handback.json"):
                with self.subTest(configuration=configuration, location=config_path.name):
                    atomic_json(config_path, configuration)
                    self.submit()
                    self.assertEqual(path.read_bytes(), before)
                    self.assertIsNone(self.recover())
                    config_path.unlink()

    def test_invalid_input_is_silent_and_never_raises(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            for payload in (None, [], "bad json", {"session_id": []}, {"session_id": "bad\nname"}):
                self.assertIsNone(hooks.process("codex", "stop", payload, home=self.home))
                self.assertIsNone(hooks.process("claude", "session-start", payload, home=self.home))
            # Antigravity reads stdout JSON; an unrelated payload gets a no-op object.
            self.assertEqual(hooks.process("antigravity", "Stop", {}, home=self.home), {})
            self.assertIsNone(hooks.process("codex", "unknown", {}, home=self.home))
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")
        self.assertFalse(self.home.exists())

    def test_event_argument_and_payload_event_must_agree(self):
        path = self.seed_request()
        before = path.read_bytes()
        self.submit(hook_event_name="Stop")
        self.assertEqual(path.read_bytes(), before)

    def test_malformed_configuration_cannot_enable_hooks(self):
        path = self.seed_request()
        before = path.read_bytes()
        atomic_json(self.home / "config.json", {"hooks": {"enabled": "false"}})
        self.assertIsNone(self.submit())
        self.assertEqual(path.read_bytes(), before)

    def test_corrupt_requests_in_unrelated_project_do_not_block_managed_submit(self):
        bad_project = self.home / "projects" / ("0" * 64)
        bad_requests = bad_project / "requests"
        bad_requests.mkdir(parents=True)
        (bad_requests / ("0" * 32 + ".json")).write_bytes(b"{corrupt SECRET")
        (bad_requests / ("1" * 32 + ".json")).write_bytes(b"x" * (hooks.MAX_STATE_BYTES + 1))
        self.seed_request()
        with patch.object(hooks, "_projects", return_value=iter([bad_project, self.state.path])):
            self.submit()
        self.assertEqual(self.state.load_request(self.request["id"])["hook_turn_id"], "target-turn")
        self.assertFalse((bad_project / "log").exists())

    def test_malformed_request_fields_do_not_stop_request_iteration(self):
        self.seed_request()
        invalid = self.state.path / "requests" / ("0" * 32 + ".json")
        atomic_json(invalid, {"id": "0" * 32, "handle": "codex:other", "status": []})
        context = hooks._Context(self.home, "codex", "UserPromptSubmit")
        records = list(hooks._requests(self.state.path, context))
        self.assertEqual([record["id"] for record in records], [self.request["id"]])
        self.assertFalse((self.state.path / "log").exists())
        self.submit()
        self.assertEqual(self.state.load_request(self.request["id"])["hook_turn_id"], "target-turn")

    def test_corrupt_unrelated_topology_does_not_block_claude_recovery(self):
        bad_project = self.home / "projects" / ("0" * 64)
        bad_project.mkdir(parents=True)
        self.seed_lead()
        for contents in (b"{invalid SECRET", b"x" * (hooks.MAX_STATE_BYTES + 1)):
            with self.subTest(size=len(contents)):
                (bad_project / "topology.json").write_bytes(contents)
                with patch.object(hooks, "_projects", return_value=iter([bad_project, self.state.path])):
                    result = self.recover()
                self.assertIn("한글 반환", result["hookSpecificOutput"]["additionalContext"])
                self.assertFalse((bad_project / "log").exists())

    def test_corrupt_envelopes_and_ack_do_not_block_correct_recipient(self):
        mail = self.seed_lead()
        directory = self.state.path / "inbox"
        (directory / ("0" * 32 + ".json")).write_bytes(b'{"recipient":"claude:other","body":"SECRET"}')
        (directory / ("1" * 32 + ".json")).write_bytes(b"x" * (envelope.MAX_BYTES + 1))
        ack = directory / "acks" / (mail["id"] + ".json")
        ack.parent.mkdir()
        ack.write_bytes(b"{corrupt SECRET receipt")
        before = ack.read_bytes()
        result = self.recover()
        context = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn("한글 반환 🐱", context)
        self.assertNotIn("SECRET", context)
        self.assertEqual(ack.read_bytes(), before)
        log = self.state.path / "log/hook-errors.log"
        self.assertTrue(log.is_file())
        self.assertNotIn("SECRET", log.read_text(encoding="utf-8"))

    def test_valid_topology_root_does_not_depend_on_corrupt_thread_index(self):
        self.seed_lead()
        (self.state.path / "threads.json").write_bytes(b"{broken")
        result = self.recover()
        self.assertIn("한글 반환", result["hookSpecificOutput"]["additionalContext"])

    def test_corrupt_matched_project_policy_denies_only_that_project(self):
        other_root = self.directory / "other"
        other_root.mkdir()
        other = ProjectState(other_root, home=self.home)
        other.write_json("topology.json", {"lead": "claude:lead-session", "root": str(other_root)})
        inbox.put(other.path / "inbox", envelope.make("other", "codex:worker", "claude:lead-session", "DENIED MAIL"))
        (other_root / ".handback.json").write_bytes(b"{corrupt policy")
        self.seed_lead()
        with patch.object(hooks, "_projects", return_value=iter([other.path, self.state.path])):
            result = self.recover()
        context = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn("한글 반환", context)
        self.assertNotIn("DENIED MAIL", context)

    def test_failure_logs_only_exception_class_and_is_bounded(self):
        self.seed_request(hook_turn_id="target-turn")
        with patch.object(hooks, "_enabled", side_effect=ValueError("SECRET prompt credential")):
            self.assertIsNone(self.stop())
            log = self.state.path / "log/hook-errors.log"
            self.assertIn("ValueError", log.read_text(encoding="utf-8"))
            self.assertNotIn("SECRET", log.read_text(encoding="utf-8"))
            log.write_bytes(b"x" * hooks.MAX_LOG_BYTES)
            self.assertIsNone(self.stop())
            self.assertLessEqual(log.stat().st_size, hooks.MAX_LOG_BYTES)

    def test_zero_scan_budget_fails_open_without_state(self):
        with patch.object(hooks, "SCAN_SECONDS", 0):
            start = time.monotonic()
            self.assertIsNone(self.submit())
            self.assertLess(time.monotonic() - start, 0.5)
        self.assertFalse(self.home.exists())

    @contextlib.contextmanager
    def scan_fixture(self, kind, records, receipts=None):
        """Fixed enumeration order; only source reads are mocked, checkpoints are real."""
        directory = self.state.path / kind
        values = {directory / (record["id"] + ".json"): record for record in records}
        names = [path.name for path in values]
        values.update({directory / "acks" / (key + ".json"): value
                       for key, value in (receipts or {}).items()})
        original_read, original_entries = hooks._read, hooks._entries

        def read(path, context, default=None, **kwargs):
            path = Path(path)
            if path in values:
                context.remaining()
                return values[path]
            return original_read(path, context, default, **kwargs)

        def entries(path):
            return iter(names) if path == directory else original_entries(path)

        with patch.object(hooks, "_entries", side_effect=entries), patch.object(hooks, "_read", side_effect=read):
            yield

    def test_large_ack_history_resumes_and_isolates_recipient(self):
        self.state.write_json("topology.json", {"lead": "claude:lead-session", "root": str(self.root)})
        template = envelope.make("request", "codex:worker", "claude:lead-session", "ACK HISTORY")
        history = [{**template, "id": f"{index:032x}"} for index in range(4096)]
        receipts = {m["id"]: {"id": m["id"], "recipient": m["recipient"]} for m in history}
        target = {**template, "id": "e" * 32, "body": "LAST UNREAD"}
        other = {**template, "id": "f" * 32, "recipient": "claude:other", "body": "OTHER PRIVATE"}
        with self.scan_fixture("inbox", history + [target, other], receipts), patch.object(hooks, "SCAN_SECONDS", 60):
            first = self.recover()["hookSpecificOutput"]["additionalContext"]
            self.assertIn("recovery scan incomplete", first)
            self.assertNotIn("LAST UNREAD", first)
            second = self.recover()["hookSpecificOutput"]["additionalContext"]
            self.assertIn("LAST UNREAD", second)
            self.assertNotIn("OTHER PRIVATE", second)
            self.assertNotIn("ACK HISTORY", second)
            # This recipient starts at zero, regardless of the first one's cursor.
            contexts = [hooks._Context(self.home), hooks._Context(self.home)]
            self.assertEqual(list(hooks._unread(self.state.path, "claude:other", contexts[0])), [])
            self.assertEqual([m["id"] for m in hooks._unread(self.state.path, "claude:other", contexts[1])],
                             [other["id"]])
            # Completing a pass wraps. Without an explicit ACK, mail is replayed.
            self.recover()
            self.assertIn("LAST UNREAD", self.recover()["hookSpecificOutput"]["additionalContext"])
        self.assertFalse((self.state.path / "inbox/acks").exists())
        log = (self.home / "hook-scans/limits.log").read_text(encoding="utf-8")
        self.assertIn("kind=inbox reason=limit", log)
        self.assertNotIn("PRIVATE", log)

    def test_large_closed_request_history_resumes_watcher_lookup(self):
        self.state.write_json("topology.json", {"lead": "claude:lead-session", "root": str(self.root)})
        history = [{**self.request, "id": f"{index:032x}", "status": "completed"} for index in range(4096)]
        other = {**self.request, "id": "e" * 32, "return_to": "claude:other"}
        target = {**self.request, "id": "f" * 32}
        with self.scan_fixture("requests", history + [other, target]), patch.object(hooks, "SCAN_SECONDS", 60):
            first = self.recover()["hookSpecificOutput"]["additionalContext"]
            self.assertIn("recovery scan incomplete", first)
            self.assertNotIn("no inbox watcher", first)
            second = self.recover()["hookSpecificOutput"]["additionalContext"]
            self.assertIn("1 open request(s)", second)
            self.assertIn("--for claude:lead-session", second.replace("'", ""))
            self.assertNotIn("--for claude:other", second)

    def test_request_cursors_are_scoped_to_session_event_and_purpose(self):
        records = [{**self.request, "id": f"{index:032x}"} for index in range(3)]
        def scan(session, event="Stop", purpose="observation"):
            context = hooks._Context(self.home, "codex", event)
            context.session = session
            return [r["id"] for r in hooks._requests(self.state.path, context, purpose)]
        with self.scan_fixture("requests", records), patch.object(hooks, "MAX_REQUESTS", 1):
            self.assertEqual(scan("one"), [records[0]["id"]])
            self.assertEqual(scan("two"), [records[0]["id"]])
            self.assertEqual(scan("one", "UserPromptSubmit"), [records[0]["id"]])
            self.assertEqual(scan("one", purpose="project-root"), [records[0]["id"]])
            self.assertEqual(scan("one"), [records[1]["id"]])

    def test_scan_budget_resumes_interrupted_read_without_losing_recovered_mail(self):
        first = self.seed_lead()
        last = inbox.put(self.state.path / "inbox", envelope.make(
            "last", "codex:worker", first["recipient"], "LAST AFTER BUDGET", message_id="c" * 32))
        original_read = hooks._read
        directory = self.state.path / "inbox"
        names = [first["id"] + ".json", last["id"] + ".json"]
        original_entries = hooks._entries
        def read(path, context, *args, **kwargs):
            if path == directory / names[-1]:
                raise hooks._BudgetExpired()
            return original_read(path, context, *args, **kwargs)
        with patch.object(hooks, "_entries", side_effect=lambda path:
                          iter(names) if path == directory else original_entries(path)):
            with patch.object(hooks, "_read", side_effect=read):
                output = self.recover()["hookSpecificOutput"]["additionalContext"]
            self.assertIn(first["body"], output)
            self.assertIn("recovery scan incomplete", output)
            self.assertNotIn(last["body"], output)
            self.assertIn(last["body"], self.recover()["hookSpecificOutput"]["additionalContext"])
        self.assertIn("reason=budget", (self.home / "hook-scans/limits.log").read_text(encoding="utf-8"))
        self.assertEqual(len(inbox.pending(directory, first["recipient"])), 2)

    def test_time_budget_advances_requests_and_leaves_time_for_next_lookup(self):
        records = [{**self.request, "id": f"{index:032x}"} for index in range(5)]
        clock = [0.0]
        with self.scan_fixture("requests", records):
            fixture_read = hooks._read
            def read(path, context, *args, **kwargs):
                if Path(path).parent == self.state.path / "requests":
                    clock[0] += 0.3
                return fixture_read(path, context, *args, **kwargs)
            with patch.object(hooks.time, "monotonic", side_effect=lambda: clock[0]), \
                    patch.object(hooks, "_read", side_effect=read):
                seen = []
                for _ in range(4):
                    context = hooks._Context(self.home, "claude", "SessionStart")
                    seen.extend(r["id"] for r in hooks._requests(self.state.path, context))
                    self.assertGreater(context.remaining(), 0)
                self.assertIn(records[-1]["id"], seen)

    def test_context_overflow_does_not_skip_the_unreported_message(self):
        self.seed_lead()
        directory = self.state.path / "inbox"
        for index in range(hooks.MAX_CONTEXT_MESSAGES + 2):
            inbox.put(directory, envelope.make(str(index), "codex:worker", "claude:lead-session", "body"))
        reported = set()
        for _ in range(3):
            text = self.recover()["hookSpecificOutput"]["additionalContext"]
            reported.update(json.loads(line)["id"] for line in text.splitlines() if line.startswith("{"))
        self.assertEqual(reported, {m["id"] for m in inbox.pending(directory, "claude:lead-session")})

    def test_checkpoint_failure_is_visible_and_does_not_discard_mail(self):
        self.seed_lead()
        with patch.object(hooks, "MAX_CONTEXT_MESSAGES", 0), \
                patch.object(hooks, "atomic_json", side_effect=OSError("SECRET")):
            output = self.recover()["hookSpecificOutput"]["additionalContext"]
        self.assertIn("checkpoint failure", output)
        self.assertNotIn("SECRET", output)
        self.assertTrue(inbox.pending(self.state.path / "inbox", "claude:lead-session"))

    def test_damaged_or_stale_checkpoint_restarts_from_original_state(self):
        records = [{**self.request, "id": f"{index:032x}"} for index in range(2)]
        with self.scan_fixture("requests", records), patch.object(hooks, "MAX_REQUESTS", 1):
            def scan():
                return list(hooks._requests(self.state.path, hooks._Context(self.home)))
            scan()
            checkpoint = next((self.home / "hook-scans").glob("*.json"))
            checkpoint.write_text("{broken", encoding="utf-8")
            self.assertEqual(scan()[0]["id"], records[0]["id"])
            atomic_json(checkpoint, {"offset": 999})
            self.assertEqual(scan(), [])  # EOF resets a cursor beyond a shrunken directory.
            self.assertEqual(scan()[0]["id"], records[0]["id"])

    def test_completed_scan_removes_checkpoint_and_keeps_limits_log(self):
        records = [{**self.request, "id": f"{index:032x}"} for index in range(2)]
        with self.scan_fixture("requests", records), patch.object(hooks, "MAX_REQUESTS", 1):
            def scan():
                return list(hooks._requests(self.state.path, hooks._Context(self.home)))
            scan()
            scans = self.home / "hook-scans"
            self.assertEqual(len(list(scans.glob("*.json"))), 1)
            scan()  # reaches EOF
            self.assertEqual(list(scans.glob("*.json")), [])
            self.assertTrue((scans / "limits.log").exists())

    def test_stale_checkpoints_are_pruned_when_another_is_written(self):
        records = [{**self.request, "id": f"{index:032x}"} for index in range(2)]
        scans = self.home / "hook-scans"
        scans.mkdir(parents=True, exist_ok=True)
        stale, fresh = scans / "a.json", scans / "b.json"
        for path in (stale, fresh):
            atomic_json(path, {"offset": 0})
        old = time.time() - 8 * 24 * 3600
        os.utime(stale, (old, old))
        with self.scan_fixture("requests", records), patch.object(hooks, "MAX_REQUESTS", 1):
            list(hooks._requests(self.state.path, hooks._Context(self.home)))
        self.assertFalse(stale.exists())
        self.assertTrue(fresh.exists())
        self.assertTrue((scans / "limits.log").exists())

    def test_later_recovery_budget_expiry_keeps_earlier_mail(self):
        mail = self.seed_lead()
        with patch.object(hooks, "_watch_hint", side_effect=hooks._BudgetExpired):
            output = self.recover()["hookSpecificOutput"]["additionalContext"]
        self.assertIn(mail["body"], output)
        self.assertIn("recovery scan incomplete", output)


if __name__ == "__main__":
    unittest.main()
