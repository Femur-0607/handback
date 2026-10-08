from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_relay import dashboard


ID = "01a11b01-5bb3-7201-a473-44b01aeeb755"


class ConversationLinkTests(unittest.TestCase):
    def test_only_valid_codex_identifiers(self):
        self.assertEqual(dashboard.conversation_link("codex:" + ID), "codex://threads/" + ID)
        self.assertEqual(dashboard.conversation_link("codex:" + ID.upper()), "codex://threads/" + ID)
        for value in (None, 42, "", "codex:", "codex:pending-123", "codex:" + ID + "?x=1",
                      "codex:" + ID + "/../other", "codex:" + ID + "\n", "codex:$(calc)",
                      "codex:" + ID + " & calc", "claude:" + ID, "antigravity:" + ID):
            self.assertIsNone(dashboard.conversation_link(value), repr(value))

    def test_deduplicate_using_worker_handle_not_display_name(self):
        rows = {"running": [{"handle": "codex:" + ID, "name": "old", "created_utc": "2026-01-01"},
                            {"handle": "codex:" + ID, "name": "new", "created_utc": "2026-02-01"},
                            {"handle": "antigravity:" + ID, "name": "new"}],
                "unread": [{"sender": "codex:" + ID, "from": "worker display name"},
                           {"sender": "codex:" + ID, "from": "same worker"}]}
        targets = dashboard.conversation_targets(rows, "running")
        self.assertEqual(len(targets), 2)
        self.assertEqual(targets[0]["name"], "new")
        self.assertEqual(dashboard.conversation_targets(rows, "unread")[0]["handle"], "codex:" + ID)
        self.assertEqual(len(dashboard.conversation_targets(rows, "unread")), 1)

    def test_os_opener_only_receives_validated_url(self):
        url = "codex://threads/" + ID
        with patch.object(dashboard.sys, "platform", "win32"), \
                patch.object(dashboard.os, "startfile", create=True) as opener:
            dashboard.open_conversation_url(url)
            opener.assert_called_once_with(url)
            for invalid in ("cmd /c start calc", "https://example.com", url + "?q=1", None):
                with self.assertRaises(ValueError):
                    dashboard.open_conversation_url(invalid)
            self.assertEqual(opener.call_count, 1)
        with patch.object(dashboard.sys, "platform", "linux"), \
                patch.object(dashboard.webbrowser, "open", return_value=True) as opener:
            dashboard.open_conversation_url(url)
            opener.assert_called_once_with(url)

    def test_snapshot_keeps_raw_worker_identity(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp:
            root = Path(temp) / "project"
            root.mkdir()
            folder = Path(temp) / "state"
            handle = "codex:" + ID
            dashboard.atomic_json(folder / "topology.json", {"root": str(root), "lead": "claude:lead", "workers": ["codex"]})
            dashboard.atomic_json(folder / "threads.json", {handle: {"name": "display"}})
            dashboard.atomic_json(folder / "requests" / "one.json", {"handle": handle, "status": "accepted"})
            dashboard.inbox.send(folder / "inbox", "test", handle, "result", "result")
            row = dashboard.project_row(folder)
            self.assertEqual(row["running"][0]["handle"], handle)
            self.assertEqual(row["unread"][0]["sender"], handle)
            self.assertEqual(row["unread"][0]["from"], "display")
