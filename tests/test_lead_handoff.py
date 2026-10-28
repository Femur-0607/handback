"""Documented Lead replacement, with temporary state and no application I/O."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from handback import cli, hooks
from handback.state import ProjectState
from tests.test_cli import FakeAdapter


class LeadHandoffTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name).resolve()
        self.root = directory / "project"
        self.root.mkdir()
        self.home = directory / "state"
        env = patch.dict(os.environ, HANDBACK_HOME=str(self.home),
                         GIT_CEILING_DIRECTORIES=str(directory))
        env.start()
        self.addCleanup(env.stop)
        for name in ("HANDBACK_LEAD", "HANDBACK_WORKERS", "CLAUDE_CODE_SESSION_ID"):
            os.environ.pop(name, None)
        self.adapter = FakeAdapter()
        self.followed = []

        def incremental(request):
            self.followed.append(request["id"])
            class Follower:
                def poll(inner):
                    return self.adapter.result
            return Follower()

        self.adapter.incremental_collector = incremental
        adapter = patch.object(cli, "get_adapter", return_value=self.adapter)
        adapter.start()
        self.addCleanup(adapter.stop)
        spawn = patch.object(cli.collector, "spawn", side_effect=AssertionError("no processes"))
        spawn.start()
        self.addCleanup(spawn.stop)
        self.state = ProjectState(self.root)

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main([*args, "--root", str(self.root)])
        self.assertEqual(code, 0, err.getvalue())
        return out.getvalue()

    def switch(self, name):
        self.run_cli("use", "--lead", "claude:" + name, "--workers", "codex")

    def send(self, worker):
        return json.loads(self.run_cli("send", "--to", "codex:" + worker,
                                      "--text", "bounded task", "--no-wait", "--no-collect"))["request_id"]

    def mail(self, lead, request):
        return json.loads(self.run_cli("inbox", "list", "--for", "claude:" + lead,
                                      "--request", request))

    def recover(self, name):
        return hooks.process("claude", "UserPromptSubmit", {"session_id": name}, home=self.home)

    def test_switch_wait_read_and_ack_old_address_then_new_request(self):
        self.switch("old")
        old = self.send("first")
        self.switch("new")
        self.run_cli("wait", "--request", old, "--timeout", "1")
        self.assertEqual(self.state.load_request(old)["return_to"], "claude:old")
        old_mail = self.mail("old", old)
        self.assertEqual(len(old_mail), 1)
        self.assertEqual(self.mail("new", old), [])
        self.assertIsNone(self.recover("new"))
        # Old recovery is also suppressed once topology points at the new Lead.
        self.assertIsNone(self.recover("old"))
        self.assertEqual(self.run_cli("inbox", "watch", "--for", "claude:new", "--once"), "")
        self.run_cli("inbox", "ack", "--for", "claude:old", "--request", old)
        self.assertEqual(self.mail("old", old), [])
        self.assertTrue((self.state.path / "inbox" / (old_mail[0]["id"] + ".json")).exists())
        new = self.send("second")
        self.run_cli("wait", "--request", new, "--timeout", "1")
        self.assertEqual(self.state.load_request(new)["return_to"], "claude:new")
        self.assertEqual(len(self.mail("new", new)), 1)
        self.assertEqual(self.mail("old", new), [])
        self.assertIn(new, self.recover("new")["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(len(self.adapter.deliveries), 2)

    def test_new_watch_does_not_collect_old_requests_but_explicit_old_watch_does(self):
        self.switch("old")
        old = self.send("first")
        self.switch("new")
        new = self.send("second")
        event = json.loads(self.run_cli("inbox", "watch", "--for", "claude:new", "--once"))
        self.assertEqual(event["request_id"], new)
        self.assertEqual(self.followed, [new])
        self.assertEqual(self.state.load_request(old)["status"], "accepted")
        event = json.loads(self.run_cli("inbox", "watch", "--for", "claude:old", "--once"))
        self.assertEqual(event["request_id"], old)
        self.assertEqual(self.followed, [new, old])
        self.assertEqual(len(self.mail("old", old)), 1)
        self.assertEqual(len(self.adapter.deliveries), 2)

    def reassign(self, *extra):
        return json.loads(self.run_cli("inbox", "reassign", "--from", "claude:old", "--to", "claude:new", *extra))

    def test_reassign_moves_an_open_request_to_the_new_lead(self):
        self.switch("old")
        old = self.send("first")
        self.switch("new")
        moved = self.reassign()
        self.assertEqual(moved["requests"], [{"id": old, "status": "accepted"}])
        request = self.state.load_request(old)
        self.assertEqual(request["return_to"], "claude:new")
        self.assertEqual(request["reassigned"][0]["from"], "claude:old")
        # The new Lead's watch now follows and receives it; the old address gets nothing.
        event = json.loads(self.run_cli("inbox", "watch", "--for", "claude:new", "--once"))
        self.assertEqual(event["request_id"], old)
        self.assertEqual(self.mail("old", old), [])
        self.assertEqual(len(self.adapter.deliveries), 1)

    def test_reassign_copies_an_unacknowledged_result_then_acks_the_original(self):
        self.switch("old")
        old = self.send("first")
        self.run_cli("wait", "--request", old, "--timeout", "1")
        original = self.mail("old", old)[0]
        self.switch("new")
        self.assertIsNone(self.recover("new"))
        moved = self.reassign()
        self.assertEqual(moved["requests"], [])
        self.assertEqual([m["id"] for m in moved["mail"]], [original["id"]])
        self.assertEqual(self.mail("old", old), [])
        copy = self.mail("new", old)[0]
        self.assertEqual(copy["id"], moved["mail"][0]["copy_id"])
        for field in ("request_id", "sender", "kind", "body", "hop", "created_utc"):
            self.assertEqual(copy[field], original[field], field)
        self.assertTrue((self.state.path / "inbox" / (original["id"] + ".json")).exists())
        self.assertIn(copy["id"], self.recover("new")["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.state.load_request(old)["reply_id"], copy["id"])
        explained = json.loads(self.run_cli("explain", "--request", old, "--json"))
        result_stage = next(s for s in explained["timeline"] if s["stage"] == "result in inbox")
        self.assertEqual(result_stage["message_id"], copy["id"])
        self.assertIn("claude:new", explained["next_action"])
        # Repeating finds nothing left; ACK works on the new address.
        self.assertEqual(self.reassign()["mail"], [])
        self.run_cli("inbox", "ack", "--for", "claude:new", "--request", old)
        self.assertEqual(self.mail("new", old), [])

    def test_reassign_retry_after_a_crash_publishes_the_same_copy_once(self):
        self.switch("old")
        old = self.send("first")
        self.run_cli("wait", "--request", old, "--timeout", "1")
        self.switch("new")
        with patch.object(cli.inbox, "acknowledge", side_effect=OSError("crash")):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertNotEqual(cli.main(["inbox", "reassign", "--from", "claude:old", "--to", "claude:new",
                                              "--root", str(self.root)]), 0)
        self.assertEqual(len(self.mail("old", old)), 1)
        copy = self.mail("new", old)[0]["id"]
        # The request already follows the copy, so the original's ACK is all a retry has left.
        self.assertEqual(self.state.load_request(old)["reply_id"], copy)
        moved = self.reassign()
        self.assertEqual(len(moved["mail"]), 1)
        self.assertEqual(self.mail("old", old), [])
        self.assertEqual([m["id"] for m in self.mail("new", old)], [copy])
        self.assertEqual(len(self.state.load_request(old)["reassigned"]), 1)

    def test_reassign_moves_mail_without_a_readable_request(self):
        self.switch("old")
        legacy = cli.inbox.send(self.state.path / "inbox", "legacy-task", "codex:w", "result", "old style",
                                 recipient="claude:old")
        self.switch("new")
        moved = self.reassign()
        self.assertEqual([m["id"] for m in moved["mail"]], [legacy["id"]])
        copy = cli.inbox.pending(self.state.path / "inbox", "claude:new")[0]
        self.assertEqual((copy["request_id"], copy["body"]), ("legacy-task", "old style"))
        self.assertEqual(cli.inbox.pending(self.state.path / "inbox", "claude:old"), [])

    def test_reassign_refuses_while_the_old_address_is_watched(self):
        from handback import watcher
        self.switch("old")
        old = self.send("first")
        self.switch("new")
        lease = watcher.Lease(self.state.path, "claude:old")
        lease.beat()
        self.addCleanup(lease.release)
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = cli.main(["inbox", "reassign", "--from", "claude:old", "--to", "claude:new",
                             "--root", str(self.root)])
        self.assertNotEqual(code, 0)
        self.assertIn("Nothing was moved", err.getvalue())
        self.assertEqual(self.state.load_request(old)["return_to"], "claude:old")
        self.assertTrue(self.reassign("--dry-run")["dry_run"])
        self.assertEqual(self.reassign("--force")["requests"][0]["id"], old)
        self.assertEqual(self.state.load_request(old)["return_to"], "claude:new")

    def test_reassign_dry_run_and_request_filter(self):
        self.switch("old")
        first, second = self.send("first"), self.send("second")
        self.switch("new")
        preview = self.reassign("--dry-run")
        self.assertTrue(preview["dry_run"])
        self.assertEqual({r["id"] for r in preview["requests"]}, {first, second})
        self.assertEqual(self.state.load_request(first)["return_to"], "claude:old")
        self.assertEqual([r["id"] for r in self.reassign("--request", first)["requests"]], [first])
        self.assertEqual(self.state.load_request(first)["return_to"], "claude:new")
        self.assertEqual(self.state.load_request(second)["return_to"], "claude:old")

    def test_reassign_refuses_placeholder_or_same_recipient(self):
        self.switch("old")
        self.send("first")
        for source, target in (("claude:old", "claude:old"), ("claude:old", "claude:lead"), ("old", "claude:new")):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                try:
                    code = cli.main(["inbox", "reassign", "--from", source, "--to", target, "--root", str(self.root)])
                except SystemExit as exit:
                    code = exit.code
            self.assertNotEqual(code, 0, (source, target))
        self.assertTrue(all(r["return_to"] == "claude:old" for r in self.state.requests()))


class CallerIdentityTests(LeadHandoffTests):
    """Issue #3: a second Claude session in the same root must not receive another session's result."""

    def call(self, *args, session=None):
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": session} if session else {}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main([*args] if args[0] == "new" else [*args, "--root", str(self.root)])
        return code, out.getvalue(), err.getvalue()

    def send_as(self, session, worker, *extra):
        return self.call("send", "--to", "codex:" + worker, "--text", "bounded task",
                         "--no-wait", "--no-collect", *extra, session=session)

    def test_non_lead_session_is_refused_before_anything_is_sent(self):
        self.switch("b")
        code, _, err = self.send_as("a", "first")
        self.assertNotEqual(code, 0)
        self.assertIn("claude:a", err)
        self.assertIn("--return-to claude:a", err)
        self.assertIn("--lead claude:a", err)
        self.assertEqual(self.state.requests(), [])
        self.assertEqual(self.adapter.deliveries, [])

    def test_lead_session_and_explicit_return_to_are_accepted(self):
        self.switch("b")
        code, out, err = self.send_as("b", "first")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.state.load_request(json.loads(out)["request_id"])["return_to"], "claude:b")
        code, out, err = self.send_as("a", "second", "--return-to", "claude:a")
        self.assertEqual(code, 0, err)
        request = json.loads(out)["request_id"]
        self.assertEqual(self.state.load_request(request)["return_to"], "claude:a")
        self.run_cli("wait", "--request", request, "--timeout", "1")
        self.assertEqual(len(self.mail("a", request)), 1)
        self.assertEqual(self.mail("b", request), [])
        # The topology Lead is unchanged.
        self.assertEqual(cli.lead_handle(cli.config.resolve(self.root)["values"]), "claude:b")

    def test_return_to_requires_a_concrete_handle(self):
        self.switch("b")
        for value in ("claude:lead", "claude:", "a"):
            code, _, err = self.send_as(None, "first", "--return-to", value)
            self.assertNotEqual(code, 0, value)
        self.assertEqual(self.state.requests(), [])

    def test_new_refuses_before_creating_a_worker_thread(self):
        self.switch("b")
        created = []
        self.adapter.new_thread = lambda *args, **kwargs: created.append(args) or "thread"
        code, _, err = self.call("new", "--cwd", str(self.root), "--name", "unit", "--worker", "codex",
                                 "--text", "bounded task", "--no-wait", session="a")
        self.assertNotEqual(code, 0)
        self.assertIn("not the configured Lead", err)
        self.assertEqual(created, [])

    def test_placeholder_lead_is_not_compared(self):
        self.run_cli("use", "--lead", "claude", "--workers", "codex")
        code, _, err = self.send_as("a", "first")
        self.assertEqual(code, 0, err)

    def test_use_warns_when_the_previous_lead_is_still_active(self):
        self.switch("a")
        self.send("first")
        code, _, err = self.call("use", "--lead", "claude:b", "--workers", "codex")
        self.assertEqual(code, 0, err)
        self.assertIn("previous Lead claude:a still has 1 open request(s)", err)
        self.assertIn("--return-to", err)
        code, _, err = self.call("use", "--lead", "claude:c", "--workers", "codex")
        self.assertNotIn("previous Lead", err)
