import base64
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from agent_relay import hook_install


class HookInstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / "relay-state"
        self.paths = {"codex": self.root / "codex" / "hooks.json",
                      "claude": self.root / "claude" / "settings.json"}
        self.script = self.root / "relay entry.py"
        self.script.write_text("pass\n", encoding="utf-8")

    def write(self, agent, value):
        self.paths[agent].parent.mkdir(parents=True, exist_ok=True)
        self.paths[agent].write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def read(self, agent):
        return json.loads(self.paths[agent].read_text(encoding="utf-8"))

    def install(self, agents=("codex", "claude"), **kwargs):
        return hook_install.install(agents, self.script, home=self.home, config_paths=self.paths, **kwargs)

    def uninstall(self, agents=("codex", "claude"), **kwargs):
        return hook_install.uninstall(agents, home=self.home, config_paths=self.paths, **kwargs)

    def manifest(self):
        return json.loads((self.home / "hook-install" / "manifest.json").read_text(encoding="utf-8"))

    def test_install_preserves_settings_and_existing_multiple_handlers(self):
        existing = {"matcher": "startup", "hooks": [{"type": "command", "command": "first"},
                                                       {"type": "command", "command": "second"}]}
        before = {"env": {"PRIVATE_VALUE": "never-expose-this"}, "model": "custom",
                  "hooks": {"SessionStart": [existing], "UnknownEvent": [{"custom": True}]}}
        self.write("claude", before)
        original_bytes = self.paths["claude"].read_bytes()
        result = self.install()
        after = self.read("claude")
        self.assertEqual(after["env"], before["env"])
        self.assertEqual(after["model"], before["model"])
        self.assertEqual(after["hooks"]["SessionStart"][0], existing)
        self.assertEqual(after["hooks"]["UnknownEvent"], before["hooks"]["UnknownEvent"])
        self.assertEqual(set(self.read("codex")["hooks"]), {"UserPromptSubmit", "Stop", "Interrupt"})
        self.assertNotIn("never-expose-this", json.dumps(result))
        backup = next(change["backup"] for change in result["changes"] if change["agent"] == "claude")
        self.assertEqual(Path(backup).read_bytes(), original_bytes)
        self.assertNotIn("never-expose-this", json.dumps(self.manifest()))
        self.assertNotIn("Stop", after["hooks"])
        self.assertTrue(any("/hooks" in warning for warning in result["warnings"]))

    def test_install_and_uninstall_are_idempotent(self):
        self.write("codex", {"description": "user config"})
        self.install()
        snapshots = {agent: path.read_bytes() for agent, path in self.paths.items()}
        manifest_bytes = (self.home / "hook-install" / "manifest.json").read_bytes()
        backups = sorted((self.home / "hook-install" / "backups").iterdir())
        result = self.install()
        self.assertFalse(any(change["changed"] for change in result["changes"]))
        self.assertEqual(manifest_bytes, (self.home / "hook-install" / "manifest.json").read_bytes())
        self.assertEqual(backups, sorted((self.home / "hook-install" / "backups").iterdir()))
        self.assertEqual(snapshots, {agent: path.read_bytes() for agent, path in self.paths.items()})
        self.uninstall()
        self.assertEqual(self.read("codex"), {"description": "user config"})
        self.assertEqual(self.read("claude"), {})
        again = self.uninstall()
        self.assertFalse(any(change["changed"] for change in again["changes"]))

    def test_uninstall_preserves_newer_unrelated_edits(self):
        self.write("codex", {"hooks": {"Stop": []}, "description": "original"})
        self.install()
        config = self.read("codex")
        unknown = {"hooks": [{"type": "command", "command": "added after install"}]}
        config["hooks"]["Stop"].append(unknown)
        config["hooks"]["FutureEvent"] = [{"future": True}]
        config["description"] = "newer"
        self.write("codex", config)
        self.uninstall()
        self.assertEqual(self.read("codex"), {"description": "newer",
                         "hooks": {"Stop": [unknown], "FutureEvent": [{"future": True}]}})

    def test_uninstall_preserves_edited_managed_group(self):
        self.install(("codex",))
        config = self.read("codex")
        config["hooks"]["Stop"][0]["hooks"].append({"type": "command", "command": "user addition"})
        self.write("codex", config)
        result = self.uninstall(("codex",))
        self.assertEqual(self.read("codex")["hooks"], {"Stop": config["hooks"]["Stop"]})
        self.assertTrue(any("edited relay" in warning for warning in result["warnings"]))
        self.assertEqual(len(next(iter(self.manifest()["installations"].values()))["groups"]), 1)

    def test_reinstall_edited_group_refuses_without_overwrite(self):
        self.install(("claude",))
        config = self.read("claude")
        config["hooks"]["SessionStart"][0]["matcher"] = "resume"
        self.write("claude", config)
        original = self.paths["claude"].read_bytes()
        with self.assertRaisesRegex(ValueError, "was edited"):
            self.install(("claude",))
        self.assertEqual(self.paths["claude"].read_bytes(), original)

    def test_identical_preexisting_hooks_are_never_adopted(self):
        self.install()
        # Use the exact same generated commands (same state-home) but remove the
        # ownership manifest to represent pre-existing unmanaged configuration.
        manifest_path = self.home / "hook-install" / "manifest.json"
        manifest_path.unlink()
        result = self.install()
        self.assertFalse(any(change["changed"] for change in result["changes"]))
        self.assertFalse(manifest_path.exists())
        before = {agent: path.read_bytes() for agent, path in self.paths.items()}
        self.uninstall()
        self.assertEqual(before, {agent: path.read_bytes() for agent, path in self.paths.items()})

    def test_new_script_path_replaces_only_exact_owned_groups(self):
        self.install(("claude",))
        old = deepcopy(self.read("claude"))
        second = self.root / "new relay.py"
        second.write_text("pass\n", encoding="utf-8")
        result = hook_install.install(["claude"], second, home=self.home, config_paths=self.paths)
        change = result["changes"][0]
        self.assertEqual(len(change["added"]), 2)
        self.assertEqual(len(change["removed"]), 2)
        self.assertNotEqual(self.read("claude"), old)
        self.assertEqual(len(self.read("claude")["hooks"]["SessionStart"]), 1)
        self.assertIn(str(second.resolve()), self.read("claude")["hooks"]["SessionStart"][0]["hooks"][0]["args"])

    def test_dry_run_creates_no_files_and_omits_unrelated_data(self):
        before = sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*"))
        result = self.install(dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertTrue(all(change["changed"] for change in result["changes"]))
        self.assertEqual(before, sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*")))
        result = self.uninstall(dry_run=True)
        self.assertFalse(any(change["changed"] for change in result["changes"]))
        self.assertEqual(before, sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*")))

    def test_bad_json_is_detected_before_any_config_write(self):
        self.write("claude", {})
        self.paths["claude"].write_text("{", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Invalid JSON"):
            self.install()
        self.assertFalse(self.paths["codex"].exists())
        self.assertEqual(self.paths["claude"].read_text(), "{")
        self.assertFalse((self.home / "hook-install" / "manifest.json").exists())

    def test_invalid_hook_shape_preserved(self):
        for bad in ({"hooks": []}, {"hooks": {"Stop": "invalid"}}):
            self.write("codex", bad)
            with self.assertRaises(ValueError):
                self.install(("codex",))
            self.assertEqual(self.read("codex"), bad)

    def test_failed_second_config_write_rolls_back_first_and_manifest(self):
        self.write("codex", {"description": "original"})
        original = self.paths["codex"].read_bytes()
        real = hook_install._atomic_bytes
        def fail(path, data, expected=hook_install._UNCHECKED):
            if Path(path) == self.paths["claude"]:
                raise OSError("simulated write failure")
            return real(path, data, expected)
        with mock.patch.object(hook_install, "_atomic_bytes", side_effect=fail):
            with self.assertRaisesRegex(OSError, "simulated"):
                self.install()
        self.assertEqual(self.paths["codex"].read_bytes(), original)
        self.assertFalse(self.paths["claude"].exists())
        self.assertFalse((self.home / "hook-install" / "manifest.json").exists())
        self.assertTrue(list((self.home / "hook-install" / "backups").glob("*.bak")))

    def test_failed_manifest_write_does_not_modify_configs(self):
        self.write("codex", {"description": "original"})
        real = hook_install._atomic_bytes
        def fail(path, data, expected=hook_install._UNCHECKED):
            if Path(path).name == "manifest.json":
                raise OSError("manifest failure")
            return real(path, data, expected)
        with mock.patch.object(hook_install, "_atomic_bytes", side_effect=fail):
            with self.assertRaisesRegex(OSError, "manifest failure"):
                self.install()
        self.assertEqual(self.read("codex"), {"description": "original"})
        self.assertFalse(self.paths["claude"].exists())

    def test_concurrent_edit_detected_without_overwrite(self):
        self.write("codex", {"description": "original"})
        real = hook_install._atomic_bytes
        def edit(path, data, expected=hook_install._UNCHECKED):
            if Path(path) == self.paths["codex"]:
                self.write("codex", {"description": "concurrent edit"})
            return real(path, data, expected)
        with mock.patch.object(hook_install, "_atomic_bytes", side_effect=edit):
            with self.assertRaisesRegex(ValueError, "changed concurrently"):
                self.install(("codex",))
        self.assertEqual(self.read("codex"), {"description": "concurrent edit"})

    def test_crashed_uninstall_keeps_ownership_for_recovery(self):
        self.install()
        real = hook_install._atomic_bytes
        def crash(path, data, expected=hook_install._UNCHECKED):
            if Path(path) == self.paths["codex"]:
                raise KeyboardInterrupt("simulated process interruption")
            return real(path, data, expected)
        with mock.patch.object(hook_install, "_atomic_bytes", side_effect=crash):
            with self.assertRaises(KeyboardInterrupt):
                self.uninstall()
        self.assertTrue(self.manifest()["transaction_pending"])
        self.assertEqual(len(self.manifest()["installations"]), 2)
        result = self.uninstall()
        self.assertEqual(self.read("codex"), {})
        self.assertEqual(self.read("claude"), {})
        self.assertEqual(self.manifest()["installations"], {})
        self.assertNotIn("transaction_pending", self.manifest())
        self.assertTrue(any("interrupted" in warning for warning in result["warnings"]))

    def test_crashed_upgrade_retains_old_and_new_groups_for_recovery(self):
        self.install(("claude",))
        second = self.root / "replacement.py"
        second.write_text("pass\n", encoding="utf-8")
        real = hook_install._atomic_bytes
        def crash(path, data, expected=hook_install._UNCHECKED):
            if Path(path) == self.paths["claude"]:
                raise KeyboardInterrupt()
            return real(path, data, expected)
        with mock.patch.object(hook_install, "_atomic_bytes", side_effect=crash):
            with self.assertRaises(KeyboardInterrupt):
                hook_install.install(["claude"], second, home=self.home, config_paths=self.paths)
        record = next(iter(self.manifest()["installations"].values()))
        self.assertEqual(len(record["groups"]), 4)
        self.uninstall(("claude",))
        self.assertEqual(self.read("claude"), {})

    def test_failed_finalize_rolls_back_configs(self):
        self.write("claude", {"model": "original"})
        real = hook_install._atomic_bytes
        calls = 0
        def fail(path, data, expected=hook_install._UNCHECKED):
            nonlocal calls
            if Path(path).name == "manifest.json":
                calls += 1
                if calls == 2:
                    raise OSError("finalize failure")
            return real(path, data, expected)
        with mock.patch.object(hook_install, "_atomic_bytes", side_effect=fail):
            with self.assertRaisesRegex(OSError, "finalize failure"):
                self.install()
        self.assertFalse(self.paths["codex"].exists())
        self.assertEqual(self.read("claude"), {"model": "original"})
        self.assertFalse((self.home / "hook-install" / "manifest.json").exists())

    def test_rollback_preserves_newer_edits_and_retains_removable_ownership(self):
        self.write("codex", {"description": "original"})
        real = hook_install._atomic_bytes
        def fail_after_edit(path, data, expected=hook_install._UNCHECKED):
            if Path(path) == self.paths["claude"]:
                current = self.read("codex")
                current["user_setting"] = "newer edit"
                self.write("codex", current)
                raise OSError("second file failure")
            return real(path, data, expected)
        with mock.patch.object(hook_install, "_atomic_bytes", side_effect=fail_after_edit):
            with self.assertRaisesRegex(ValueError, "newer edits were preserved"):
                self.install()
        self.assertTrue(self.manifest()["transaction_pending"])
        self.assertEqual(self.read("codex")["user_setting"], "newer edit")
        self.uninstall()
        self.assertEqual(self.read("codex"), {"description": "original", "user_setting": "newer edit"})
        self.assertFalse(self.paths["claude"].exists())

    def test_empty_existing_hook_keys_preserved_on_uninstall(self):
        original = {"hooks": {"Stop": []}}
        self.write("codex", original)
        self.install(("codex",))
        self.uninstall(("codex",))
        self.assertEqual(self.read("codex"), original)

    def test_claude_exec_form_and_codex_windows_encoded_command(self):
        result = self.install(dry_run=True)
        claude = next(change for change in result["changes"] if change["agent"] == "claude")
        handler = claude["added"][0]["group"]["hooks"][0]
        self.assertEqual(handler["command"], os.path.abspath(sys.executable))
        self.assertIn("--state-home", handler["args"])
        with mock.patch.object(hook_install.sys, "platform", "win32"):
            group, argv = hook_install._spec("codex", "Interrupt", self.script, self.home)
        handler = group["hooks"][0]
        self.assertEqual(handler["timeout"], 3)
        decoded = base64.b64decode(handler["commandWindows"].split()[-1]).decode("utf-16le")
        self.assertIn("exit $LASTEXITCODE", decoded)
        self.assertIn(str(self.script), decoded)
        self.assertIn("-WindowStyle Hidden", handler["commandWindows"])
        self.assertEqual(argv[-1], str(self.home))

    @unittest.skipUnless(os.name == "nt" and shutil.which("powershell.exe"), "Windows PowerShell unavailable")
    def test_windows_command_roundtrips_special_paths_arguments_and_stdin(self):
        special = self.root / "한글 space ' $ ` &"
        special.mkdir()
        script = special / "relay script.py"
        script.write_text("import json, sys\nprint(json.dumps({'args': sys.argv[1:], 'stdin': sys.stdin.read()}, ensure_ascii=False))\n",
                          encoding="utf-8")
        group, argv = hook_install._spec("codex", "Stop", script, special / "state")
        command = group["hooks"][0]["commandWindows"]
        result = subprocess.run(command.split(), input='{"sample":"한글"}', capture_output=True,
                                text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["args"], argv[4:])
        self.assertEqual(payload["stdin"], '{"sample":"한글"}')

    def test_legacy_owned_hooks_upgrade_to_module_and_uninstall(self):
        self.install()
        from agent_relay import invocation
        with mock.patch.object(invocation.Path, "is_file", return_value=False):
            result = hook_install.install(["codex", "claude"], home=self.home, config_paths=self.paths)
        for change in result["changes"]:
            self.assertEqual(len(change["added"]), len(change["removed"]))
            for spec in change["invocations"]:
                self.assertEqual(spec["argv"][1:6], ["-X", "utf8", "-m", "agent_relay", "hook"])
        self.uninstall()
        self.assertEqual(self.read("codex"), {})
        self.assertEqual(self.read("claude"), {})

    def test_hook_preserves_virtual_environment_interpreter(self):
        with mock.patch.object(hook_install.Path, "resolve", side_effect=AssertionError("venv symlink resolved")):
            group, argv = hook_install._spec("claude", "SessionStart", self.script, self.home)
        self.assertEqual(argv[0], os.path.abspath(sys.executable))
        self.assertEqual(group["hooks"][0]["command"], argv[0])


if __name__ == "__main__":
    unittest.main()
