import contextlib
import io
import sys
import unittest
from unittest.mock import MagicMock, patch

from handback import cli, setup_wizard

ALL = {"claude": True, "codex": False, "antigravity": False}


def run(answers=(), found=ALL, **kwargs):
    lines, queue, prompts = [], list(answers), []

    def fake_input(prompt=""):
        prompts.append(prompt)
        return queue.pop(0) if queue else ""
    install = MagicMock(return_value=["installed: C:/Users/a/.claude/skills/handback/SKILL.md"])
    auto, launch = MagicMock(), MagicMock()
    options = dict(input_fn=fake_input, interactive=True, detect=lambda: found, install=install,
                   set_autostart=auto, launch=launch, out=lines.append)
    options.update(kwargs)
    code = setup_wizard.run(**options)
    return code, lines, install, auto, launch, prompts


class WizardTests(unittest.TestCase):
    def test_happy_path(self):
        with patch("sys.platform", "win32"):
            code, lines, install, auto, launch, prompts = run(["", "y", ""])
        self.assertEqual(code, 0)
        install.assert_called_once_with(None, False)
        auto.assert_called_once_with(True)
        launch.assert_called_once()
        print("\n".join(lines))
        self.assertIn("✓ Claude", lines)
        self.assertIn("– Codex 없음 (선택)", lines)
        self.assertTrue(any("/handback" in line for line in lines))

    def test_declined(self):
        with patch("sys.platform", "win32"):
            code, lines, install, auto, launch, _ = run(["n", "n", "n"])
        install.assert_not_called(); auto.assert_not_called(); launch.assert_not_called()

    def test_claude_missing_exits(self):
        code, lines, install, auto, launch, prompts = run(found={"claude": False})
        self.assertEqual(code, 1)
        install.assert_not_called()
        self.assertTrue(any("Claude가 필요" in line for line in lines))

    def test_dry_run_changes_nothing(self):
        code, lines, install, auto, launch, prompts = run(dry_run=True)
        self.assertEqual(code, 0)
        install.assert_called_once_with(None, True)
        auto.assert_not_called(); launch.assert_not_called()
        self.assertEqual(prompts, [])

    def test_non_tty_without_yes_is_plan_only(self):
        code, lines, install, auto, launch, prompts = run(interactive=False)
        self.assertEqual(code, 2)
        install.assert_called_once_with(None, True)
        auto.assert_not_called(); launch.assert_not_called()
        self.assertEqual(prompts, [])

    def test_non_tty_yes_installs_skills_only(self):
        code, lines, install, auto, launch, prompts = run(interactive=False, yes=True)
        install.assert_called_once_with(None, False)
        auto.assert_not_called(); launch.assert_not_called()
        self.assertEqual(prompts, [])

    def test_step_failure_is_one_line_not_traceback(self):
        install = MagicMock(side_effect=OSError("권한 없음\n자세히"))
        code, lines, *_ = run(["y", "n", "n"], install=install)
        self.assertEqual(code, 1)
        self.assertIn("실패: 스킬 설치 (권한 없음 자세히)", lines)


class LocationTests(unittest.TestCase):
    def test_risky_paths(self):
        for path in ("C:/Users/a/Downloads/handback/handback.exe", "C:/Users/a/Desktop/h/handback.exe",
                     "C:/Users/a/AppData/Local/Temp/h/handback.exe", "C:/my tools/handback.exe"):
            self.assertTrue(setup_wizard.risky_location(path), path)
        self.assertFalse(setup_wizard.risky_location("C:/handback/handback.exe"))

    def test_frozen_risky_location_warns_and_declines(self):
        with patch.object(setup_wizard, "frozen", return_value=True),                 patch.object(setup_wizard, "risky_location", return_value=True):
            code, lines, install, *_ = run(["n"])
        self.assertEqual(code, 1)
        self.assertTrue(any(line.startswith("권장:") for line in lines))
        install.assert_not_called()

    def test_frozen_risky_location_non_tty_only_prints(self):
        with patch.object(setup_wizard, "frozen", return_value=True),                 patch.object(setup_wizard, "risky_location", return_value=True):
            code, lines, install, _, _, prompts = run(interactive=False, yes=True)
        self.assertEqual(code, 0)
        self.assertEqual(prompts, [])
        self.assertTrue(any(line.startswith("권장:") for line in lines))


class InteractiveTests(unittest.TestCase):
    def test_win32_nul_like_stdin_is_not_interactive(self):
        with patch("sys.platform", "win32"), patch.object(setup_wizard, "_console", return_value=False):
            self.assertFalse(setup_wizard._interactive())

    def test_win32_console_both_streams_is_interactive(self):
        with patch("sys.platform", "win32"), patch.object(setup_wizard, "_console", return_value=True):
            self.assertTrue(setup_wizard._interactive())

    def test_error_means_not_interactive(self):
        with patch("sys.platform", "win32"), patch.object(setup_wizard, "_console", side_effect=OSError):
            self.assertFalse(setup_wizard._interactive())

    def test_win32_getconsolemode_failure(self):
        if sys.platform != "win32":
            self.skipTest("Windows only")
        import ctypes
        fake = MagicMock(return_value=0)
        with patch.object(ctypes.windll.kernel32, "GetConsoleMode", fake, create=True),                 patch("msvcrt.get_osfhandle", return_value=1):
            self.assertFalse(setup_wizard._console(MagicMock(fileno=lambda: 0)))

    def test_run_defaults_to_helper(self):
        with patch.object(setup_wizard, "_interactive", return_value=False) as helper:
            code, _, install, _, _, prompts = run(interactive=None)
        helper.assert_called_once()
        self.assertEqual(code, 2)
        self.assertEqual(prompts, [])


class EntryTests(unittest.TestCase):
    def test_frozen_no_args_runs_wizard(self):
        with patch("handback.invocation.frozen", return_value=True), \
                patch("handback.setup_wizard.run", return_value=0) as wizard:
            self.assertEqual(cli.main([]), 0)
        wizard.assert_called_once_with(yes=False, dry_run=False)

    def test_source_no_args_prints_help(self):
        out = io.StringIO()
        with patch("handback.invocation.frozen", return_value=False), contextlib.redirect_stdout(out):
            self.assertEqual(cli.main([]), 0)
        self.assertIn("usage: handback", out.getvalue())

    def test_setup_subcommand_flags(self):
        with patch("handback.setup_wizard.run", return_value=0) as wizard:
            cli.main(["setup", "--dry-run"])
        wizard.assert_called_once_with(yes=False, dry_run=True)


if __name__ == "__main__":
    unittest.main()
