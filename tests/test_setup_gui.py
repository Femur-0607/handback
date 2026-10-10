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
        calls = []
        lines = setup_gui.run_install(
            True, True, install=lambda *a: ["installed: C:/u/.claude/skills/handback"],
            set_autostart=lambda on: calls.append(on))
        self.assertEqual(lines, ["✓ Claude 스킬 설치됨", "✓ 시작 시 자동 실행"])
        self.assertEqual(calls, [True])
        self.assertEqual(setup_gui.run_install(False, False, install=lambda *a: [],
                                               set_autostart=lambda on: None), [])


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

    def test_claude_missing_disables_install(self):
        with self.sync():
            window = setup_gui.open_setup(self.strip, detect=lambda: {"claude": False})
        texts = self.texts(window)
        self.assertIn("handback 설정", texts)
        self.assertIn(setup_gui.NEED_CLAUDE, texts)
        self.assertEqual(str(self.button(window, "설치").cget("state")), "disabled")

    def test_install_flow_shows_result_and_done_button(self):
        with self.sync():
            window = setup_gui.open_setup(
                self.strip, detect=lambda: {"claude": True},
                install=lambda *a: ["installed: C:/u/.claude/skills/handback"],
                set_autostart=lambda on: None)
            self.assertIn("✓ Claude", self.texts(window))
            self.button(window, "설치").invoke()
        texts = self.texts(window)
        self.assertIn("✓ Claude 스킬 설치됨", texts)
        self.assertIn(setup_gui.NEXT_STEP, texts)
        self.assertEqual(self.button(window, "완료").cget("text"), "완료")
        window.destroy()
        self.assertTrue(self.root.winfo_exists())  # closing keeps the dashboard

    def test_install_error_is_one_line(self):
        def boom(*args):
            raise OSError("디스크 가득")
        with self.sync():
            window = setup_gui.open_setup(self.strip, detect=lambda: {"claude": True}, install=boom,
                                          set_autostart=lambda on: None)
            self.button(window, "설치").invoke()
        self.assertIn("실패: 디스크 가득", self.texts(window))
        self.assertEqual(str(self.button(window, "설치").cget("state")), "normal")

    def test_reopen_reuses_window(self):
        with self.sync():
            first = setup_gui.open_setup(self.strip, detect=lambda: {})
            self.assertIs(setup_gui.open_setup(self.strip, detect=lambda: {}), first)

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
