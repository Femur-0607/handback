"""Documented Lead replacement, with temporary state and no application I/O."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_relay import cli, hooks
from agent_relay.state import ProjectState
from tests.test_cli import FakeAdapter


class LeadHandoffTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name).resolve()
        self.root = directory / "project"
        self.root.mkdir()
        self.home = directory / "state"
        env = patch.dict(os.environ, AGENT_RELAY_HOME=str(self.home),
                         GIT_CEILING_DIRECTORIES=str(directory))
        env.start()
        self.addCleanup(env.stop)
        for name in ("AGENT_RELAY_LEAD", "AGENT_RELAY_WORKERS"):
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
