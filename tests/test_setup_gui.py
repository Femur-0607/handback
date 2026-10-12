"""First-run setup window: content, install flow and the needs-setup decision."""
import unittest
from unittest.mock import patch

from handback import setup_gui
from tests import test_dashboard_gui


class PureTests(unittest.TestCase):
    def test_needs_setup_follows_skill_status(self):
        with patch("handback.diagnostics.skill_status", return_value="missing"):
            self.assertTrue(setup_gui.needs_setup())
        with patch("handback.diagnostics.skill_status", return_value="points at this installation"):
            self.assertFalse(setup_gui.needs_setup())
        with patch("handback.diagnostics.skill_status", side_effect=OSError):
            self.assertFalse(setup_gui.needs_setup())

    def test_app_lines(self):
        self.assertEqual(setup_gui.app_lines({"claude": True}),
                         ["✓ Claude", "– Codex 없음 (선택)", "– Antigravity 없음 (선택)"])
        self.assertEqual(setup_gui.app_lines({})[0], "– Claude 없음")

    def test_run_install_lines_and_autostart(self):
        calls, hooked = [], []
        lines = setup_gui.run_install(
            True, True, True, ["claude", "codex"],
            install=lambda *a: ["installed: C:/u/.claude/skills/handback"],
            set_autostart=lambda on: calls.append(on), install_hooks=hooked.append)
        self.assertEqual(lines, ["✓ Claude 스킬 설치됨", "✓ 복구 훅 설치됨 (Claude, Codex)",
                                 "✓ 시작 시 자동 실행"])
        self.assertEqual(calls, [True])
        self.assertEqual(hooked, [["claude", "codex"]])
        self.assertEqual(setup_gui.run_install(False, False, False, install=lambda *a: [],
                                               set_autostart=lambda on: None,
                                               install_hooks=hooked.append), [])
        self.assertEqual(len(hooked), 1)


class SetupWindowTests(test_dashboard_gui.DashboardMenuTests):
    def sync(self):
        def run(key, work, done):
            try:
                result, error = work(), None
            except Exception as exc:
                result, error = None, exc
            done(result, error)
        return patch.object(self.strip, "_run_async", side_effect=run)

    def texts(self, window):
        return [w.cget("text") for w in self.widgets(window) if isinstance(w, self.tk.Label)]

    def button(self, window, text):
        return next(w for w in self.widgets(window)
                    if isinstance(w, self.tk.Button) and w.cget("text") == text)

    def open(self, **kwargs):
        kwargs.setdefault("status", lambda found: {"skills": False, "hooks": True})
        kwargs.setdefault("install_hooks", lambda agents: None)
        return setup_gui.open_setup(self.strip, **kwargs)

    def test_claude_missing_disables_install(self):
        with self.sync():
            window = self.open(detect=lambda: {"claude": False})
        texts = self.texts(window)
        self.assertIn("handback 설정", texts)
        self.assertIn(setup_gui.NEED_CLAUDE, texts)
        self.assertEqual(str(self.button(window, setup_gui.INSTALL).cget("state")), "disabled")

    def test_status_marks_each_item(self):
        with self.sync():
            window = self.open(detect=lambda: {"claude": True})
        boxes = [w.cget("text") for w in self.widgets(window) if isinstance(w, self.tk.Checkbutton)]
        self.assertIn(setup_gui.SKILLS_LABEL + "  · 필요", boxes)
        self.assertIn(setup_gui.HOOKS_LABEL + "  · 설치됨", boxes)

    def test_install_flow_shows_result_and_done_button(self):
        with self.sync():
            hooked = []
            window = self.open(
                detect=lambda: {"claude": True},
                install=lambda *a: ["installed: C:/u/.claude/skills/handback"],
                set_autostart=lambda on: None, install_hooks=hooked.append)
            self.assertIn("✓ Claude", self.texts(window))
            self.button(window, setup_gui.INSTALL).invoke()
        texts = self.texts(window)
        self.assertIn("✓ Claude 스킬 설치됨", texts)
        self.assertIn("✓ 복구 훅 설치됨 (Claude)", texts)
        self.assertEqual(hooked, [["claude"]])
        self.assertIn(setup_gui.NEXT_STEP, texts)
        self.assertEqual(self.button(window, "완료").cget("text"), "완료")
        window.destroy()
        self.assertTrue(self.root.winfo_exists())  # closing keeps the dashboard

    def test_install_error_is_one_line(self):
        def boom(*args):
            raise OSError("디스크 가득")
        with self.sync():
            window = self.open(detect=lambda: {"claude": True}, install=boom,
                               set_autostart=lambda on: None)
            self.button(window, setup_gui.INSTALL).invoke()
        self.assertIn("실패: 디스크 가득", self.texts(window))
        self.assertEqual(str(self.button(window, setup_gui.INSTALL).cget("state")), "normal")

    def test_unhook_button_uses_confirmed_dashboard_action(self):
        with self.sync():
            window = self.open(detect=lambda: {"claude": True})
        with patch.object(self.strip, "_hooks") as hooks:
            self.button(window, "훅 해제").invoke()
        hooks.assert_called_once_with("uninstall")

    def test_reopen_reuses_window(self):
        with self.sync():
            first = self.open(detect=lambda: {})
            self.assertIs(self.open(detect=lambda: {}), first)

    def test_auto_setup_only_when_needed(self):
        with patch("handback.setup_gui.needs_setup", return_value=False), \
                patch("handback.setup_gui.open_setup") as opened:
            self.strip._auto_setup()
            opened.assert_not_called()
        with patch("handback.setup_gui.needs_setup", return_value=True), \
                patch("handback.setup_gui.open_setup") as opened:
            self.strip._auto_setup()
            opened.assert_called_once_with(self.strip)


# Reuse the Tk fixture without re-running the inherited dashboard tests.
for _name in dir(test_dashboard_gui.DashboardMenuTests):
    if _name.startswith("test_") and _name not in SetupWindowTests.__dict__:
        setattr(SetupWindowTests, _name, None)


if __name__ == "__main__":
    unittest.main()
