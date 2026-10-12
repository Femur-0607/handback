"""Claude is detected from its CLI or, without one, from the desktop app."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from handback import discovery
from handback.adapters import claude
from handback.adapters.claude import ClaudeAdapter


class ClaudeDetectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = {k: v for k, v in os.environ.items() if k not in ("HANDBACK_CLAUDE", "USERPROFILE")}
        env["LOCALAPPDATA"] = self.tmp.name
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        which = mock.patch.object(shutil, "which", return_value=None)
        which.start()
        self.addCleanup(which.stop)

    def test_nothing_installed(self):
        info = ClaudeAdapter().detect()
        self.assertFalse(info["installed"])
        self.assertIsNone(info["desktop_app"])

    def test_msix_desktop_app_without_cli(self):
        package = Path(self.tmp.name, "Packages", "Claude_pzs8sxrjxfjjc")
        package.mkdir(parents=True)
        info = ClaudeAdapter().detect()
        self.assertTrue(info["installed"])
        self.assertEqual(info["desktop_app"], str(package))
        self.assertIsNone(info["version"])

    def test_squirrel_desktop_app_without_cli(self):
        exe = Path(self.tmp.name, "AnthropicClaude", "claude.exe")
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"")
        self.assertEqual(discovery.claude_desktop_app(), str(exe))

    def test_discovery_never_runs_the_app(self):
        Path(self.tmp.name, "Packages", "Claude_x").mkdir(parents=True)
        with mock.patch.object(subprocess, "run", side_effect=AssertionError("ran an app")):
            self.assertTrue(ClaudeAdapter().detect()["installed"])

    def test_working_cli_skips_desktop_lookup(self):
        done = subprocess.CompletedProcess([], 0, stdout="2.1.292 (Claude Code)\n")
        with mock.patch.object(claude.subprocess, "run", return_value=done), \
                mock.patch.object(claude, "claude_desktop_app", side_effect=AssertionError("looked up app")):
            info = ClaudeAdapter("claude").detect()
        self.assertTrue(info["installed"])
        self.assertIsNone(info["desktop_app"])


if __name__ == "__main__":
    unittest.main()
