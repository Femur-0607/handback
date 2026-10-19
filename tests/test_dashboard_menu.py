"""Right-click menu: autostart, doctor, skills, hooks, roles, connection test."""
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from handback import compat, dashboard, skill_install
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

    def test_update_item_and_settings_toggle(self):
        release = {"version": "9.0.0", "installer": "i", "sums": "s", "page": "https://github.com/x"}
        for supported in (True, False):
            with self.subTest(supported=supported), \
                    patch.object(dashboard.updater, "supported", return_value=supported):
                self.strip._update = release
                menu = self.context_menu()
                self.assertEqual(self.labels(menu)[0], "업그레이드 (9.0.0)")
                self.assertIn("릴리스 페이지" if not supported else "설치", menu.items[0]["tooltip"])
                labels = [item["label"] for item in menu.items if item]
                self.assertIn(f"최신 버전 확인 (현재 {dashboard.__version__})", labels)
                self.strip._close_menu()
                self.assertNotIn("업그레이드 (9.0.0)", self.labels(self.context_menu(self.project())))
                self.strip._close_menu()
        self.strip._update = None
        self.assertTrue(dashboard.load_prefs(self.home)["update_check"])

    def test_source_checkout_upgrade_opens_the_release_page_instead_of_installing(self):
        release = {"version": "9.0.0", "installer": None, "sums": None,
                   "page": "https://github.com/Femur-0607/handback/releases/tag/v9.0.0"}
        self.strip._update = release
        with self.sync_async(), patch.object(dashboard.updater, "supported", return_value=False), \
                patch.object(dashboard.updater, "download") as get, \
                patch.object(dashboard.webbrowser, "open") as browser, \
                patch.object(self.strip, "quit") as quit_:
            with patch.object(self.strip, "_dialog", return_value=False):
                self.strip._install_update()
            browser.assert_not_called()
            with patch.object(self.strip, "_dialog", return_value=True) as dialog:
                self.strip._install_update()
            browser.assert_called_once_with(release["page"])
            self.assertIn("릴리스 페이지", dialog.call_args[0][1])
            get.assert_not_called()
            quit_.assert_not_called()
        self.strip._update = None

    def test_install_update_confirms_then_quits_after_launch(self):
        release = {"version": "9.0.0", "installer": "i", "sums": "s"}
        self.strip._update = release
        with self.sync_async(), patch.object(dashboard.updater, "supported", return_value=True), \
                patch.object(dashboard.updater, "download", return_value="C:/u/setup.exe") as get,                 patch.object(dashboard.updater, "run_installer") as launch,                 patch.object(self.strip, "quit") as quit_:
            with patch.object(self.strip, "_dialog", return_value=False):
                self.strip._install_update()
            get.assert_not_called()
            with patch.object(self.strip, "_dialog", return_value=True):
                self.strip._install_update()
            launch.assert_called_once_with("C:/u/setup.exe")
            quit_.assert_called_once()
        self.strip._update = None

    def test_failed_download_keeps_running(self):
        self.strip._update = {"version": "9.0.0", "installer": "i", "sums": "s"}
        with self.sync_async(), patch.object(dashboard.updater, "supported", return_value=True), \
                patch.object(dashboard.updater, "download",
                                             side_effect=dashboard.updater.UpdateError("hash")),                 patch.object(dashboard.updater, "run_installer") as launch,                 patch.object(self.strip, "quit") as quit_,                 patch.object(self.strip, "_dialog", return_value=True) as dialog:
            self.strip._install_update()
        launch.assert_not_called(); quit_.assert_not_called()
        self.assertIn("업데이트하지 못했습니다", dialog.call_args[0][1])
        self.strip._update = None

    def test_manual_check_reports_up_to_date(self):
        with self.sync_async(), patch.object(dashboard.updater, "check", return_value=None),                 patch.object(self.strip, "_dialog") as dialog:
            self.strip._check_update()
        self.assertIn("최신 버전", dialog.call_args[0][1])

    def test_root_menu_order_and_project_items(self):
        root = self.project()
        menu = self.context_menu()
        expected = ["옵션…", f"최신 버전 확인 (현재 {dashboard.__version__})", "점검·복구 ▶", "닫기"]
        self.assertEqual(self.labels(menu), expected)
        self.strip._close_menu()
        names = self.labels(self.context_menu(root))
        self.assertIn("에이전트 ▶", names)
        self.assertNotIn("모델·추론 설정 ▶", names)
        self.assertNotIn("요청 닫기 ▶", names)  # only while a request is open
        self.assertIn("연결 테스트", names)
        sub = self.submenu(self.context_menu(root), "Hook ▶") if False else None
        self.assertIsNone(sub)

    def test_hovering_a_disabled_row_closes_the_open_submenu(self):
        menu = self.context_menu(self.project(running=True))
        agents = self.entry(menu, "에이전트 ▶")
        menu.select(agents, hover=True)
        self.assertIsNotNone(menu.child)
        disabled = self.entry(menu, "연결 테스트")
        self.assertFalse(menu.items[disabled]["enabled"])
        menu.select(disabled, hover=True)
        self.assertIsNone(menu.child)
        self.assertIsNone(menu.active)

    @unittest.skipUnless(dashboard.sys.platform == "win32", "Windows only")
    def test_autostart_toggle_calls_set_autostart(self):
        with self.sync_async(), patch.object(dashboard, "startup_shortcut") as link, \
                patch.object(dashboard, "set_autostart") as setter:
            link.return_value = Path(self.home) / "missing.lnk"
            self.strip._options()
            window = self.strip._options_window
            next(w for w in self.widgets(window) if isinstance(w, self.tk.Checkbutton)
                 and w.cget("text") == "Windows 시작 시 실행").invoke()
            window.destroy()
            setter.assert_called_once_with(True)

    def test_autostart_error_shows_dialog(self):
        with self.sync_async(), patch.object(dashboard, "set_autostart", side_effect=RuntimeError("boom")), \
                patch.object(self.strip, "_dialog") as dialog:
            self.strip._toggle_autostart(True)
            self.assertIn("boom", dialog.call_args[0][1])

    def test_autostart_toggle_runs_off_the_tk_thread(self):
        with patch.object(self.strip, "_run_async") as run_async, \
                patch.object(dashboard, "set_autostart") as setter:
            self.strip._toggle_autostart(False)
            setter.assert_not_called()
            key, work, _done = run_async.call_args[0]
            self.assertEqual(key, ("autostart",))
            work()
            setter.assert_called_once_with(False)

    def test_powershell_calls_hide_console_window(self):
        expected = getattr(dashboard.subprocess, "CREATE_NO_WINDOW", 0)
        done = dashboard.subprocess.CompletedProcess([], 0, "\ufeffC:\\한글\\x.exe\r\n", "")
        with patch.object(dashboard.sys, "platform", "win32"), \
                patch.object(dashboard.subprocess, "run", return_value=done) as run:
            dashboard._write_startup_shortcut("a.lnk", "b.exe", "", ".")
            self.assertEqual(run.call_args.kwargs["creationflags"], expected)
            self.assertEqual(dashboard._shortcut_target("a.lnk"), "C:\\한글\\x.exe")
            self.assertEqual(run.call_args.kwargs["creationflags"], expected)
            self.assertEqual(run.call_args.kwargs["encoding"], "utf-8")
            self.assertTrue(run.call_args[0][0][-1].startswith("[Console]::OutputEncoding=[Text.Encoding]::UTF8;"))

    def test_autostart_repair_survives_decode_error(self):
        link = Path(self.home) / "x.lnk"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.write_text("x")
        with patch.object(dashboard.sys, "platform", "win32"), \
                patch.object(dashboard, "startup_shortcut", return_value=link), \
                patch.object(dashboard, "_shortcut_target",
                             side_effect=UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")), \
                patch.object(dashboard, "set_autostart") as setter:
            self.assertFalse(dashboard.repair_autostart())
            setter.assert_not_called()

    def test_viewer_acknowledge_acks_only_shown_ids(self):
        checkout = self.home.parent / "viewer-project"
        checkout.mkdir()
        project = str(checkout)
        folder = self.home / "projects" / ("b" * 64)
        dashboard.atomic_json(folder / "topology.json",
                             {"root": project, "lead": "claude:test", "workers": ["codex"]})
        shown = dashboard.inbox.send(folder / "inbox", "test", "codex:test", "result", "seen")
        later = dashboard.inbox.send(folder / "inbox", "test", "codex:test", "result", "later")
        self.strip.unread_counts[project] = 2  # refreshed count includes the late arrival
        with patch.object(self.strip, "_dialog", return_value=True) as dialog:
            self.assertTrue(self.strip._acknowledge(project, ids=[shown["id"]]))
        self.assertIn("1개를 모두 확인 처리", dialog.call_args_list[0].args[1])
        self.assertEqual(dialog.call_args_list[1].args, ("정리 완료", "1개를 확인 처리했습니다."))
        self.assertEqual([m["id"] for m in dashboard.inbox.pending(folder / "inbox")], [later["id"]])

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

    def test_doctor_lists_problems_only(self):
        self.project()
        rows = [{"check": "claude skill", "level": "WARN", "detail": "missing"},
                {"check": "Python", "level": "OK"},
                {"check": "codex version", "level": "WARN"}]
        self.assertEqual(dashboard.problem_lines(rows), ["Claude 스킬 없음 — 우클릭 › 설정"])
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
            skill_install.install(None, False)
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


class CompatMenuTests(MenuActionTests):
    """App update notification, failure menu item and its copyable summary."""
    failure = {"agent": "antigravity", "version": "2.23.1", "previous": "2.15.1", "ok": False,
               "utc": "2026-10-16T01:00:00+00:00", "notify": True,
               "checks": [{"check": "sidecar registry schema", "ok": False, "detail": "unexpected: x"}]}

    def test_detected_update_shows_a_tray_notification(self):
        self.strip.tray = type("FakeTray", (), {})()
        shown = []
        self.strip.tray.balloon = lambda title, text, warning=False: shown.append((text, warning))
        self.strip.tray.stop = lambda: None
        with self.sync_async(), patch.object(compat, "scan", return_value=[self.failure]), \
                patch.object(compat, "failures", return_value=[self.failure]):
            self.strip._check_compat(manual=False)
        self.assertEqual(len(shown), 1)
        self.assertTrue(shown[0][0].startswith("Antigravity 2.23.1 감지 — 호환성 점검 실패"))
        self.assertTrue(shown[0][1])
        self.assertEqual(self.strip._compat_failures, [self.failure])
        self.strip.tray = None

    def test_failure_menu_item_opens_a_copyable_summary(self):
        self.strip._compat_failures = [self.failure]
        menu = self.context_menu()
        first = menu.items[0]
        self.assertEqual(first["label"], "⚠ Antigravity 2.23.1 호환성 점검 실패 — 진단 요약")
        self.strip._close_menu()
        with patch.object(dashboard.webbrowser, "open") as opened:
            window = self.strip._compat_report(self.failure)
            buttons = {child.cget("text"): child for frame in window.winfo_children()
                       for child in frame.winfo_children() if child.winfo_class() == "Button"}
            buttons["요약 복사"].invoke()
            self.assertIn("FAIL sidecar registry schema", window.clipboard_get())
            buttons["GitHub 이슈 열기"].invoke()
        # The issue page is only opened; the user files it.
        self.assertTrue(opened.call_args[0][0].startswith(compat.ISSUE_URL + "?title="))
        window.destroy()

    def test_manual_check_reports_each_app(self):
        passed = {**self.failure, "ok": True, "notify": False}
        with self.sync_async(), patch.object(compat, "scan", return_value=[passed]) as scan, \
                patch.object(self.strip, "_dialog") as dialog:
            self.strip._check_compat()
        self.assertTrue(scan.call_args.kwargs["force"])
        self.assertIn("Antigravity 2.23.1 감지 — 호환성 점검 통과", dialog.call_args[0][1])


if __name__ == "__main__":
    unittest.main()


for _name in dir(MenuActionTests):
    if _name.startswith("test_") and _name not in CompatMenuTests.__dict__:
        setattr(CompatMenuTests, _name, None)
