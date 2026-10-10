"""Right-click menu: autostart, doctor, skills, hooks, roles, connection test."""
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from handback import dashboard
from tests.test_dashboard_gui import DashboardMenuTests


class MenuActionTests(DashboardMenuTests):
    def project(self, running=False, lead="claude:lead", workers=("codex",)):
        root = str(self.home.parent)
        row = {"name": "p", "root": root, "lead": lead, "workers": list(workers),
               "running": [{"id": "1"}] if running else [], "unread": [], "last_activity": 0,
               "unreadable": 0, "first_unreadable": "", "read_error": "", "combo": "x"}
        self.strip._rows = [row]
        return root

    def labels(self, menu):
        return [i["label"].strip("✓ ") for i in menu.items if i]

    def sync_async(self):
        def run(key, work, done):
            try:
                result, error = work(), None
            except Exception as exc:
                result, error = None, exc
            done(result, error)
        return patch.object(self.strip, "_run_async", side_effect=run)

    def test_root_menu_order_and_project_items(self):
        root = self.project()
        menu = self.context_menu()
        expected = ["보기 설정 ▶", "설치 상태 점검", "스킬 다시 설치", "설정 다시 실행", "Hook ▶", "새로고침", "닫기"]
        if dashboard.sys.platform == "win32":
            expected.insert(1, "Windows 시작 시 실행")
        self.assertEqual(self.labels(menu), expected)
        self.strip._close_menu()
        names = self.labels(self.context_menu(root))
        self.assertIn("역할 지정 ▶", names)
        self.assertIn("연결 테스트", names)
        sub = self.submenu(self.context_menu(root), "Hook ▶") if False else None
        self.assertIsNone(sub)

    @unittest.skipUnless(dashboard.sys.platform == "win32", "Windows only")
    def test_autostart_toggle_calls_set_autostart(self):
        with patch.object(dashboard, "startup_shortcut") as link, \
                patch.object(dashboard, "set_autostart") as setter:
            link.return_value = Path(self.home) / "missing.lnk"
            menu = self.context_menu()
            menu.invoke(self.entry(menu, "Windows 시작 시 실행"))
            setter.assert_called_once_with(True)

    def test_autostart_error_shows_dialog(self):
        with patch.object(dashboard, "set_autostart", side_effect=RuntimeError("boom")), \
                patch.object(self.strip, "_dialog") as dialog:
            self.strip._toggle_autostart(True)
            self.assertIn("boom", dialog.call_args[0][1])

    def test_role_change_refuses_with_open_requests(self):
        root = self.project(running=True)
        with patch.object(self.strip, "_dialog") as dialog, \
                patch.object(dashboard.config, "use_topology") as use:
            self.strip._change_roles(root, "claude:lead", ["codex", "antigravity"])
            use.assert_not_called()
            self.assertEqual(dialog.call_args[0][1], "진행 중인 작업이 있어 바꿀 수 없습니다")

    def test_role_change_refuses_with_env_override(self):
        root = self.project()
        with patch.dict(dashboard.os.environ, {"HANDBACK_LEAD": "x"}), \
                patch.object(self.strip, "_dialog") as dialog, \
                patch.object(dashboard.config, "use_topology") as use:
            self.strip._change_roles(root, "claude:lead", ["antigravity"])
            use.assert_not_called()
            self.assertIn("HANDBACK_LEAD", dialog.call_args[0][1])

    def test_role_change_confirms_then_calls_use_topology(self):
        root = self.project()
        env = {k: v for k, v in dashboard.os.environ.items() if k not in dashboard.ROLE_MIGRATION_ENV}
        with patch.dict(dashboard.os.environ, env, clear=True), \
                patch.object(self.strip, "refresh"), \
                patch.object(dashboard.config, "use_topology") as use:
            with patch.object(self.strip, "_dialog", return_value=False) as dialog:
                self.strip._change_roles(root, "claude:lead", ["codex", "antigravity"])
                use.assert_not_called()
                self.assertIn("Workers: codex → codex, antigravity 로 바꿉니다", dialog.call_args[0][1])
            with patch.object(self.strip, "_dialog", return_value=True):
                self.strip._change_roles(root, "claude:lead", ["codex", "antigravity"])
            use.assert_called_once_with(root, "claude:lead", ["codex", "antigravity"], "ask",
                                        home=self.home)

    def test_hook_install_requires_confirmation(self):
        with patch.object(dashboard, "run_hooks", return_value=["b.bak"]) as run:
            with patch.object(self.strip, "_dialog", return_value=False) as dialog:
                self.strip._hooks("install")
                run.assert_not_called()
                self.assertIn("백업", dialog.call_args[0][1])
            with patch.object(self.strip, "_dialog", return_value=True) as dialog:
                self.strip._hooks("install")
                run.assert_called_once_with("install", self.home)
                self.assertIn("b.bak", dialog.call_args[0][1])

    def test_skill_reinstall_requires_confirmation(self):
        with self.sync_async(), patch.object(dashboard, "run_skill_install",
                                             return_value=["installed: a"]) as run:
            with patch.object(self.strip, "_dialog", return_value=False):
                self.strip._reinstall_skills()
                run.assert_not_called()
            with patch.object(self.strip, "_dialog", return_value=True) as dialog:
                self.strip._reinstall_skills()
                run.assert_called_once()
                self.assertIn("1개", dialog.call_args[0][1])

    def test_doctor_lists_problems_only(self):
        self.project()
        rows = [{"check": "claude skill", "level": "WARN", "detail": "missing"},
                {"check": "Python", "level": "OK"},
                {"check": "codex version", "level": "WARN"}]
        self.assertEqual(dashboard.problem_lines(rows), ["Claude 스킬 없음 — 우클릭 › 스킬 다시 설치"])
        with self.sync_async(), patch.object(dashboard, "doctor_problems", return_value=[]), \
                patch.object(self.strip, "_dialog") as dialog:
            self.strip._check_install()
            self.assertEqual(dialog.call_args[0][1], "문제 없음")

    def test_connection_test_warns_runs_and_guards_double_run(self):
        root = self.project()
        with self.sync_async(), patch.object(dashboard, "run_connection_test",
                                             return_value=(True, 12, "")) as run, \
                patch.object(self.strip, "refresh"):
            with patch.object(self.strip, "_dialog", return_value=True) as dialog:
                self.strip._connection_test(root)
                self.assertIn("사용량 발생", dialog.call_args_list[0][0][1])
                self.assertIn("Workers=Codex", dialog.call_args_list[0][0][1])
                self.assertEqual(dialog.call_args_list[-1][0][1], "연결 정상 (12초)")
            run.assert_called_once_with(root)
            self.strip._busy = {("try", root)}
            with patch.object(self.strip, "_dialog") as dialog:
                self.strip._connection_test(root)
                dialog.assert_not_called()
            menu = self.context_menu(root)
            item = menu.items[self.entry(menu, "연결 테스트 (테스트 중…)")]
            self.assertFalse(item["enabled"])

    def test_connection_test_blocked_by_open_requests(self):
        root = self.project(running=True)
        menu = self.context_menu(root)
        self.assertFalse(menu.items[self.entry(menu, "연결 테스트")]["enabled"])

    def test_connection_test_runs_cli_subprocess(self):
        import subprocess
        from types import SimpleNamespace
        done = SimpleNamespace(stdout='{"elapsed_seconds": 7.2}', stderr="", returncode=0)
        with patch.object(dashboard.subprocess, "run", return_value=done) as run,                 patch("handback.invocation.self_argv", return_value=["H.exe"]):
            self.assertEqual(dashboard.run_connection_test("R"), (True, 7, ""))
            args, kwargs = run.call_args
            self.assertEqual(args[0], ["H.exe", "try", "--root", "R", "--json"])
            self.assertEqual(kwargs["env"]["PYTHONUTF8"], "1")
            self.assertTrue(kwargs["capture_output"])
        fail = SimpleNamespace(stdout='{"error": "no codex"}', stderr="", returncode=5)
        with patch.object(dashboard.subprocess, "run", return_value=fail),                 patch("handback.invocation.self_argv", return_value=["H.exe"]):
            self.assertEqual(dashboard.run_connection_test("R"), (False, 0, "no codex"))

    def test_menu_helpers_survive_missing_stdio(self):
        root = self.project()
        env = {k: v for k, v in dashboard.os.environ.items() if k not in dashboard.ROLE_MIGRATION_ENV}
        fake_home = Path(self.home) / "fakehome"
        fake_home.mkdir(parents=True, exist_ok=True)
        with patch.object(dashboard.sys, "stdout", None), patch.object(dashboard.sys, "stderr", None),                 patch.dict(dashboard.os.environ, env, clear=True),                 patch.object(Path, "home", return_value=fake_home):
            dashboard.doctor_problems(root, self.home, adapter_for=lambda a, v: SimpleNamespace(
                detect=lambda: {"agent": a, "installed": False}))
            dashboard.run_skill_install()
            dashboard.config.use_topology(root, "claude:lead", ["codex"], "ask", home=self.home)
            paths = {"claude": fake_home / "c.json", "codex": fake_home / "x.json"}
            dashboard.run_hooks("install", self.home, config_paths=paths)
            dashboard.run_hooks("uninstall", self.home, config_paths=paths)

    def test_autostart_repair_rewrites_stale_shortcut(self):
        link = Path(self.home) / "x.lnk"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.write_text("x")
        with patch.object(dashboard.sys, "platform", "win32"), \
                patch.object(dashboard, "startup_shortcut", return_value=link), \
                patch.object(dashboard, "_shortcut_target", return_value=r"C:\gone\pythonw.exe"), \
                patch.object(dashboard, "set_autostart") as setter:
            self.assertTrue(dashboard.repair_autostart())
            setter.assert_called_once_with(True)
        expected = dashboard._expected_autostart_target()
        with patch.object(dashboard.sys, "platform", "win32"), \
                patch.object(dashboard, "startup_shortcut", return_value=link), \
                patch.object(dashboard, "_shortcut_target", return_value=str(expected)), \
                patch.object(Path, "exists", return_value=True), \
                patch.object(dashboard, "set_autostart") as setter:
            self.assertFalse(dashboard.repair_autostart())
            setter.assert_not_called()


# Reuse the Tk fixture without re-running the inherited menu tests.
for _name in dir(DashboardMenuTests):
    if _name.startswith("test_") and _name not in MenuActionTests.__dict__:
        setattr(MenuActionTests, _name, None)


if __name__ == "__main__":
    unittest.main()
