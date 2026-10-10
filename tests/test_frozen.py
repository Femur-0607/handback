"""Behaviour when running as a PyInstaller onedir build (sys.frozen, handback.exe)."""
import base64
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from handback import cli, collector, dashboard, diagnostics, hook_install, invocation, router, skill_install
from handback.adapters import antigravity as agy
from handback.state import ProjectState

EXE = "C:\\x\\handback.exe"


class FrozenCase(unittest.TestCase):
    def setUp(self):
        for p in (patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", EXE)):
            p.start()
            self.addCleanup(p.stop)
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def assertPlain(self, argv, *subcommand):
        self.assertEqual(os.path.abspath(argv[0]).lower(), os.path.abspath(EXE).lower())
        self.assertEqual(argv[1:1 + len(subcommand)], list(subcommand))
        for bad in ("-X", "-m", "utf8"):
            self.assertNotIn(bad, argv)
        self.assertFalse(any(str(a).endswith((".py", ".pyw")) for a in argv))


class InvocationTests(FrozenCase):
    def test_frozen_helpers(self):
        self.assertTrue(invocation.frozen())
        self.assertEqual(invocation.entry_args(), [])
        self.assertEqual(invocation.self_argv(Path("whatever.py")), [EXE])

    def test_command_text(self):
        text = invocation.command_text("inbox", "ack")
        self.assertIn("handback.exe", text)
        self.assertNotIn("-X", text)
        self.assertNotIn(".py", text)
        self.assertIn("inbox", text)
        self.assertTrue(text.rstrip("'").endswith("ack"))

    def test_not_frozen_unchanged(self):
        with patch.object(sys, "frozen", False, create=True):
            argv = invocation.self_argv()
        self.assertEqual(argv[:3], [EXE, "-X", "utf8"])


class BuilderTests(FrozenCase):
    def setUp(self):
        super().setUp()
        self.home = self.base / "state"

    def test_hook_specs(self):
        claude, argv = hook_install._spec("claude", "SessionStart", None, self.home)
        handler = claude["hooks"][0]
        self.assertEqual(os.path.abspath(handler["command"]), os.path.abspath(EXE))
        self.assertEqual(handler["args"][:1], ["hook"])
        self.assertPlain(argv, "hook")
        codex, argv = hook_install._spec("codex", "Stop", None, self.home)
        self.assertPlain(argv, "hook")
        handler = codex["hooks"][0]
        self.assertNotIn("-X", handler["command"])
        if sys.platform == "win32":
            decoded = base64.b64decode(handler["commandWindows"].rsplit(" ", 1)[1]).decode("utf-16le")
            self.assertIn("handback.exe", decoded)
            self.assertNotIn("-X", decoded)
            self.assertNotIn(".py", decoded)

    def test_collector_and_router_spawn(self):
        root = self.base / "project"
        root.mkdir()
        with patch.dict(os.environ, {"HANDBACK_HOME": str(self.base / "h")}):
            state = ProjectState(root)
            seen = []

            def popen(command, **kwargs):
                seen.append((command, kwargs))
                return SimpleNamespace(pid=os.getpid())
            request = {"schema": 1, "id": os.urandom(16).hex(), "status": "accepted", "root": str(root)}
            state.save_request(request)
            with patch.object(collector.subprocess, "Popen", side_effect=popen), \
                    patch.object(router.subprocess, "Popen", side_effect=popen):
                collector.spawn(state, request, python="C:\\other\\python.exe")
                router.spawn_external(state, "conv")
        self.assertPlain(seen[0][0], "collect")
        self.assertPlain(seen[1][0], "route")
        for _, kwargs in seen:
            self.assertEqual(kwargs["env"]["PYTHONUTF8"], "1")

    def test_antigravity_hook_and_sidecar(self):
        parts = agy.hook_command("Stop", str(self.base)).split(" ")
        self.assertTrue(parts[0].lower().endswith("handback.exe"))
        self.assertEqual(parts[1], "hook")
        self.assertNotIn("-X", parts)
        self.assertNotIn("-m", parts)
        self.assertEqual([*agy.entry_args(agy.ENTRY_SCRIPT), "antigravity-sidecar"], ["antigravity-sidecar"])

    def test_skill_and_diagnostics(self):
        for agent in ("claude", "codex"):
            content = skill_install.render(agent)
            self.assertIn("handback.exe", content)
            self.assertNotIn("-X", content)
            self.assertNotIn("handback.py", content)
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"CODEX_HOME": home}):
            target = Path(home) / "skills/handback/SKILL.md"
            target.parent.mkdir(parents=True)
            target.write_text(skill_install.render("codex"), encoding="utf-8")
            self.assertEqual(diagnostics.skill_status("codex"), "points at this installation")
            target.write_text("nothing", encoding="utf-8")
            self.assertNotEqual(diagnostics.skill_status("codex"), "points at this installation")

    @unittest.skipUnless(sys.platform == "win32", "Windows only")
    def test_autostart_target(self):
        exe_dir = self.base / "app"
        exe_dir.mkdir()
        (exe_dir / "handback.exe").write_bytes(b"")
        with patch.object(sys, "executable", str(exe_dir / "handback.exe")), \
                patch.dict(os.environ, {"APPDATA": str(self.base / "appdata")}), \
                patch.object(dashboard.subprocess, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "handback-dashboard.exe"):
                dashboard.set_autostart(True)
            (exe_dir / "handback-dashboard.exe").write_bytes(b"")
            result = dashboard.set_autostart(True)
        script = run.call_args[0][0][-1]
        self.assertEqual(Path(result["target"]).name, "handback-dashboard.exe")
        self.assertIn("$s.Arguments = ''", script)
        self.assertNotIn("pythonw", script)
        self.assertNotIn(".pyw", script)

    def test_version(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as raised:
            cli.main(["--version"])
        self.assertEqual(raised.exception.code, 0)
        self.assertRegex(out.getvalue(), r"^handback \d+\.\d+\.\d+")


class OwnershipTests(FrozenCase):
    def test_exe_forms_recognized(self):
        home = str(self.base / "state")
        for agent, event in (("claude", "SessionStart"), ("codex", "Stop")):
            group, _ = hook_install._spec(agent, event, None, self.base / "state")
            self.assertTrue(hook_install.legacy_handler(group["hooks"][0], agent, event))
        shouted = {"type": "command", "command": "C:\\Other Dir\\HANDBACK.EXE",
                   "args": ["hook", "--agent", "claude", "--event", "Stop", "--state-home", home]}
        self.assertTrue(hook_install.legacy_handler(shouted, "claude", "Stop"))
        other = dict(shouted, command="C:\\x\\other.exe")
        self.assertFalse(hook_install.legacy_handler(other, "claude", "Stop"))

    def test_pip_to_exe_reinstall_replaces(self):
        home = self.base / "state"
        paths = {"codex": self.base / "codex" / "hooks.json", "claude": self.base / "claude" / "settings.json"}
        script = self.base / "entry.py"
        script.write_text("pass\n", encoding="utf-8")
        with patch.object(sys, "frozen", False, create=True), patch.object(sys, "executable", "C:\\py\\python.exe"):
            hook_install.install(("codex", "claude"), script, home=home, config_paths=paths)
        before = json.loads(paths["claude"].read_text(encoding="utf-8"))
        self.assertIn("-X", before["hooks"]["SessionStart"][0]["hooks"][0]["args"])
        hook_install.install(("codex", "claude"), script, home=home, config_paths=paths)
        for agent, event in (("claude", "SessionStart"), ("codex", "Stop")):
            groups = json.loads(paths[agent].read_text(encoding="utf-8"))["hooks"][event]
            self.assertEqual(len(groups), 1)
        handler = json.loads(paths["claude"].read_text(encoding="utf-8"))["hooks"]["SessionStart"][0]["hooks"][0]
        self.assertEqual(os.path.abspath(handler["command"]), os.path.abspath(EXE))
        self.assertNotIn("-X", handler["args"])

    def test_exe_hooks_without_receipt_are_adopted_not_duplicated(self):
        paths = {"claude": self.base / "claude" / "settings.json"}
        hook_install.install(("claude",), None, home=self.base / "state", config_paths=paths)
        hook_install.install(("claude",), None, home=self.base / "state2", config_paths=paths)
        groups = json.loads(paths["claude"].read_text(encoding="utf-8"))["hooks"]["SessionStart"]
        self.assertEqual(len(groups), 1)

    def test_sidecar_cleanup_recognizes_exe(self):
        self.assertIn('"handback.exe"', Path(agy.__file__).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
