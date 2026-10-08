from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from handback import dashboard


class DashboardMenuTests(unittest.TestCase):
    def setUp(self):
        try:
            import tkinter as tk
        except ImportError as error:
            self.skipTest(f"Tk is unavailable: {error}")
        self.tk = tk
        # Set transparency before any window can map, including menu/dialog children.
        original_attributes = tk.Wm.attributes
        def invisible_attributes(window, *args):
            if args and args[0] == "-alpha" and len(args) > 1:
                args = ("-alpha", 0)
            return original_attributes(window, *args)
        self.addCleanup(patch.stopall)
        patch.object(tk.Wm, "attributes", invisible_attributes).start()
        for cls in (tk.Tk, tk.Toplevel):
            original_init = cls.__init__
            def hidden_init(window, *args, _init=original_init, **kwargs):
                _init(window, *args, **kwargs)
                original_attributes(window, "-alpha", 0)
            patch.object(cls, "__init__", hidden_init).start()
        temporary = tempfile.TemporaryDirectory(prefix="dashboard-gui-test-", dir=Path(__file__).parent)
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name) / "state"
        dashboard.save_prefs({**dashboard.DEFAULT_PREFS, "mode": "panel"}, self.home)
        try:
            self.strip = dashboard.Strip(home=self.home)
        except tk.TclError as error:
            self.skipTest(f"Tk cannot create a window: {error}")
        self.root = self.strip.root
        self.addCleanup(self.destroy_root)
        self.root.withdraw()
        self.root.update_idletasks()
        self.event = SimpleNamespace(x_root=0, y_root=0)

    def destroy_root(self):
        try:
            self.root.destroy()
        except self.tk.TclError:
            pass  # The menu's close command may already have destroyed it.

    def widgets(self, parent):
        result = [parent]
        for child in parent.winfo_children():
            result.extend(self.widgets(child))
        return result

    def commands(self):
        return set(self.root.tk.splitlist(self.root.tk.call("info", "commands")))

    def menu_callbacks(self, menu):
        return {command for widget in self.widgets(menu.window)
                for command in (widget._tclCommands or [])}

    def context_menu(self, project=None):
        self.assertEqual(self.strip._menu(self.event, project), "break")
        menus = [child for child in self.root.winfo_children()
                 if isinstance(child, self.tk.Toplevel)]
        self.assertEqual(len(menus), 1, "Only the current context menu should remain alive")
        return self.strip._context_menu

    def entry(self, menu, label):
        for index, item in enumerate(menu.items):
            if item and item["label"].strip("✓ ") == label:
                return index
        self.fail(f"Menu entry not found: {label}")

    def submenu(self, menu, label="보기 설정 ▶"):
        menu.select(self.entry(menu, label))
        menu.open_child()
        return menu.child

    def resource_counts(self, menu):
        # FocusOut schedules an idle Tcl callback when popups hand focus to children.
        # Drain window events AND their newly scheduled idle work before counting.
        self.root.update()
        self.root.update_idletasks()
        widgets = self.widgets(self.root)
        return (len(widgets), sum(isinstance(widget, self.tk.Toplevel) for widget in widgets),
                len(self.commands()), len(self.menu_callbacks(menu)))

    def handles(self):
        import sys
        if sys.platform != "win32":
            return (0, 0)
        import ctypes
        from ctypes import wintypes
        kernel, user = ctypes.windll.kernel32, ctypes.windll.user32
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        user.GetGuiResources.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        return tuple(user.GetGuiResources(kernel.GetCurrentProcess(), kind) for kind in (0, 1))

    def sample_rows(self):
        return [{"root": str(i), "name": "긴 프로젝트 이름 " * 8 if i == 0 else f"project-{i}",
                 "lead": "claude:test", "workers": ["codex"], "last_activity": time.time() - i,
                 "running": [{"name": "긴 작업 " * 10}] if i == 0 else [],
                 "unread": [{"created_utc": "2026-10-08T00:00:00+00:00", "from": "worker",
                             "body": "full result " * 10, "kind": "result"}] if i == 0 else []}
                for i in range(5)]

    def test_500_refreshes_hover_tooltip_resources_and_width(self):
        rows = self.sample_rows()
        self.root.deiconify()
        self.strip.expanded.add("0")
        baseline = handles = None
        for iteration in range(500):
            self.strip.render(rows)
            self.root.update_idletasks()
            row = self.strip.frame.winfo_children()[0]
            name = row.winfo_children()[0].winfo_children()[0]
            name.event_generate("<Enter>")
            self.assertEqual(row.cget("bg"), dashboard.DarkMenu.BG)
            self.assertIsNotNone(self.strip.tooltip.pending)
            self.strip.tooltip.show(name, rows[0]["name"])
            self.root.update_idletasks()
            self.assertEqual(sum(isinstance(w, self.tk.Toplevel) for w in self.widgets(self.root)), 1)
            name.event_generate("<Leave>")
            self.assertIsNone(self.strip.tooltip.window)
            self.assertIsNone(self.strip.tooltip.pending)
            self.assertEqual(row.cget("bg"), self.strip.BG)
            current = (len(self.widgets(self.root)), len(self.commands()),
                       len(self.root.tk.splitlist(self.root.tk.call("after", "info"))),
                       self.root.winfo_reqwidth())
            if baseline is None:
                baseline = current
            self.assertEqual(current, baseline)
            if iteration == 99:
                handles = self.handles()
            name.event_generate("<Enter>")  # Next refresh cancels this pending timer.
        self.strip.tooltip.hide()
        for value, initial in zip(self.handles(), handles):
            self.assertLessEqual(value, initial)
        self.strip.expanded.clear()
        for row in rows:
            row["running"] = [{}] * 99
            row["unread"] = [{}] * 99
            row["last_activity"] = 9999999999
        self.strip.render(rows)
        self.assertEqual(self.root.winfo_reqwidth(), baseline[-1])

    def test_tooltip_real_delay_refresh_and_icon_lifetime(self):
        self.root.deiconify()
        rows = self.sample_rows()
        self.strip.render(rows)
        name = self.strip.frame.winfo_children()[0].winfo_children()[0].winfo_children()[0]
        icons = tuple(map(str, self.strip.icons))
        name.event_generate("<Enter>")
        self.assertIsNone(self.strip.tooltip.window)
        ready = self.tk.BooleanVar(self.root)
        self.root.after(550, lambda: ready.set(True))
        self.root.wait_variable(ready)
        self.assertIsNotNone(self.strip.tooltip.window)
        self.assertIsNone(self.strip.tooltip.pending)
        self.strip.render(rows)
        self.assertIsNone(self.strip.tooltip.window)
        self.assertEqual(tuple(map(str, self.strip.icons)), icons)
        self.assertEqual(len(icons), 5)
        self.assertTrue(set(icons).issubset(self.root.tk.call("image", "names")))

    def test_overflow_temporary_and_same_menu_setting_resets_it(self):
        rows = self.sample_rows()
        with patch.object(dashboard, "snapshot", return_value=rows):
            self.strip.refresh(reschedule=False)
            before = dashboard.load_prefs(self.home)
            self.strip._toggle_overflow()
            self.assertTrue(self.strip.show_all)
            self.strip.refresh(reschedule=False)
            self.assertTrue(self.strip.show_all)
            self.assertEqual(dashboard.load_prefs(self.home), before)
            self.assertEqual(self.strip.frame.winfo_children()[-1].cget("text"), "접기")
            self.strip._set_max_rows(before, before["max_rows"])
            self.assertFalse(self.strip.show_all)
            self.assertEqual(self.strip.frame.winfo_children()[-1].cget("text"), "+2개 더 보기")

    def conversation_fixture(self):
        return [{"handle": "codex:11111111-2222-4333-8444-555555555555", "name": "UI review", "created_utc": ""},
                {"handle": "codex:11111111-2222-4333-8444-666666666666", "name": "Tests", "created_utc": ""},
                {"handle": "antigravity:worker", "name": "Unsupported worker", "created_utc": ""}]

    def test_status_single_multi_unsupported_and_viewer_routes(self):
        targets = self.conversation_fixture()
        with patch.object(self.strip, "opener") as opener:
            self.strip._conversations(self.event, targets[:1])
            opener.assert_called_once_with(dashboard.conversation_link(targets[0]["handle"]))
            self.assertIsNone(self.strip._context_menu)
            self.strip._conversations(self.event, targets)
            menu = self.strip._context_menu
            self.assertFalse(menu.items[2]["enabled"])
            self.assertEqual(menu.items[2]["tooltip"], dashboard.UNSUPPORTED_CONVERSATION)
            menu.invoke(2)
            self.assertEqual(opener.call_count, 1)
            menu.invoke(1)
            self.assertEqual(opener.call_args.args[0], dashboard.conversation_link(targets[1]["handle"]))
            rows = self.sample_rows()
            rows[0]["running"][0]["handle"] = targets[0]["handle"]
            rows[0]["unread"][0]["sender"] = targets[1]["handle"]
            self.strip.expanded.add("0")
            self.root.deiconify()
            self.strip.render(rows)
            row = self.strip.frame.winfo_children()[0]
            for column, target in ((2, targets[0]), (3, targets[1])):
                cell = row.winfo_children()[column].winfo_children()[0]
                self.assertEqual(cell.cget("cursor"), "hand2")
                cell.event_generate("<Button-1>")
                self.assertEqual(opener.call_args.args[0], dashboard.conversation_link(target["handle"]))
            with patch.object(self.strip, "_open") as viewer:
                detail = self.strip.frame.winfo_children()[1].winfo_children()[1].winfo_children()[1]
                detail.event_generate("<Button-1>")
                viewer.assert_called_once_with(rows[0], 0)

    def test_500_conversation_menus_release_callbacks_tooltips_and_handles(self):
        targets = self.conversation_fixture()
        baseline = handles = None
        # Real Tk objects and callbacks; suppress display/focus churn in this stress test.
        with patch.object(dashboard.DarkMenu, "show"), patch.object(self.strip, "opener") as opener:
            for iteration in range(500):
                self.strip._conversations(self.event, targets)
                menu = self.strip._context_menu
                self.strip.tooltip.schedule(menu.labels[2], dashboard.UNSUPPORTED_CONVERSATION)
                self.strip.tooltip.show(menu.labels[2], dashboard.UNSUPPORTED_CONVERSATION)
                self.strip.tooltip.hide()
                self.root.update_idletasks()
                current = self.resource_counts(menu) + (len(self.root.tk.call("after", "info")),)
                if baseline is None:
                    baseline = current
                self.assertEqual(current, baseline)
                if iteration == 99:
                    handles = self.handles()
                self.strip.tooltip.schedule(menu.labels[2], dashboard.UNSUPPORTED_CONVERSATION)
            self.strip._close_menu()
            for value, initial in zip(self.handles(), handles):
                self.assertLessEqual(value, initial)
            opener.assert_not_called()
            self.assertIsNone(self.strip.tooltip.pending)
            self.assertIsNone(self.strip.tooltip.window)

    def test_missing_logo_silently_falls_back(self):
        with patch.object(dashboard.resources, "files", return_value=self.home / "missing"):
            other = dashboard.Strip(self.home)
            try:
                self.assertEqual(other.icons, [])
            finally:
                other.root.destroy()

    @patch.object(dashboard.DarkMenu, "show")
    def test_repeated_context_menus_keep_widget_and_tcl_command_counts_constant(self, _show):
        # Keep lifecycle stress independent of OS focus delivery. Focus handoff and
        # its temporary idle callback are exercised explicitly in the next test.
        dashboard.save_prefs({"max_rows": 3, "order": [], "hidden": ["hidden-a", "hidden-b"]},
                             self.home)
        self.strip.unread_counts["project"] = 2
        for project in (None, "project"):
            with self.subTest(project=project):
                menu = self.context_menu(project)
                self.submenu(menu)
                baseline = self.resource_counts(menu)
                self.assertEqual(baseline[1], 2)  # Context popup and settings popup.
                self.assertGreater(baseline[3], 0)
                for _ in range(99):
                    menu = self.context_menu(project)
                    self.submenu(menu)
                    self.assertEqual(self.resource_counts(menu), baseline)
                handles = self.handles()
                for _ in range(400):
                    menu = self.context_menu(project)
                    self.submenu(menu)
                    self.assertEqual(self.resource_counts(menu), baseline)
                # Windows may finish deleting earlier windows/fonts asynchronously.
                # A decrease is healthy; either resource growing is a leak.
                for current, baseline_handles in zip(self.handles(), handles):
                    self.assertLessEqual(current, baseline_handles)

    def test_focus_handoff_temporarily_registers_an_idle_tcl_command(self):
        menu = self.context_menu()
        self.root.update()
        before = self.commands()
        with patch.object(menu.window, "focus_displayof", return_value=menu.window):
            menu.focus_out(None)
            self.assertIsNotNone(menu.pending)
            added = self.commands() - before
            self.assertEqual(len(added), 1)
            self.assertTrue(next(iter(added)).endswith("check_focus"))
            self.root.update_idletasks()
        self.assertIsNone(menu.pending)
        self.assertEqual(self.commands(), before)

    def test_replacement_releases_old_callbacks_and_uses_current_project_and_preferences(self):
        dashboard.save_prefs({"max_rows": 2, "order": ["old"], "hidden": ["old-hidden"]},
                             self.home)
        old_menu = self.context_menu("old")
        self.submenu(old_menu)
        old_path = str(old_menu.window)
        old_callbacks = self.menu_callbacks(old_menu)
        deleted_callbacks = set()
        trace = self.root.register(lambda old, new, operation: deleted_callbacks.add(old.lstrip(":")))
        for command in old_callbacks:
            self.root.tk.call("trace", "add", "command", command, "delete", trace)

        latest = {**dashboard.DEFAULT_PREFS, "max_rows": 3, "order": ["new", "other"],
                  "hidden": ["new-hidden", "keep-hidden"]}
        dashboard.save_prefs(latest, self.home)
        menu = self.context_menu("new")
        self.assertFalse(self.root.tk.call("winfo", "exists", old_path))
        # Tk names callbacks using object IDs, which can be reused immediately.
        # Observe deletion itself rather than treating a reused name as a leak.
        self.assertEqual(deleted_callbacks, old_callbacks)
        self.root.deletecommand(trace)

        menu.invoke(self.entry(menu, "이 프로젝트 숨기기"))
        expected = {**latest, "hidden": latest["hidden"] + ["new"]}
        self.assertEqual(dashboard.load_prefs(self.home), expected)

        menu = self.context_menu("new")
        rows = self.submenu(menu)
        rows.invoke(self.entry(rows, "2줄"))
        expected = {**expected, "max_rows": 2}
        self.assertEqual(dashboard.load_prefs(self.home), expected)

        menu = self.context_menu("new")
        hidden = self.submenu(menu)
        hidden.invoke(self.entry(hidden, "new-hidden 다시 표시"))
        expected = {**expected, "hidden": ["keep-hidden", "new"]}
        self.assertEqual(dashboard.load_prefs(self.home), expected)

    def test_close_command_destroys_the_current_menu_and_its_callbacks(self):
        menu = self.context_menu("project")
        callbacks = self.menu_callbacks(menu)
        paths = {str(widget) for widget in self.widgets(menu.window)}
        menu.invoke(self.entry(menu, "닫기"))
        remaining = self.commands()
        self.assertTrue(callbacks.isdisjoint(remaining))
        self.assertTrue(paths.isdisjoint(remaining))

    def test_release_menu_moves_the_selected_project_state_after_confirmation(self):
        checkout = self.home.parent / "project"
        checkout.mkdir()
        project = str(checkout)
        folder = self.home / "projects" / ("a" * 64)
        topology = {"root": project, "lead": "claude:test", "workers": ["codex"]}
        dashboard.atomic_json(folder / "topology.json", topology)
        dashboard.save_prefs({"max_rows": 3, "order": [project], "hidden": [project]}, self.home)
        menu = self.context_menu(project)

        with patch.object(self.strip, "_dialog", return_value=True) as dialog, \
                patch.object(dashboard, "release", wraps=dashboard.release) as release:
            menu.invoke(self.entry(menu, "handback 등록 해제…"))
            self.assertEqual(dialog.call_count, 2)
            self.assertTrue(dialog.call_args_list[0].kwargs["confirm"])
            release.assert_called_once_with(project, self.home)

        self.assertFalse(folder.exists())
        [archived] = (self.home / "released").iterdir()
        self.assertEqual(dashboard._read(archived / "topology.json"), topology)
        self.assertEqual(dashboard.load_prefs(self.home), dashboard.DEFAULT_PREFS)

    def test_item_context_disabled_checked_and_hidden(self):
        menu = self.context_menu()
        self.assertEqual([item["label"] for item in menu.items if item],
                         ["보기 설정 ▶", "새로고침", "닫기"])
        settings = self.submenu(menu)
        self.assertEqual([item["label"] for item in settings.items if item and item["label"].startswith("✓")],
                         ["✓ 3줄", "✓ 펼친 패널 고정"])
        index = self.entry(settings, "숨긴 프로젝트 없음")
        self.assertFalse(settings.items[index]["enabled"])
        menu = self.context_menu("project")
        index = self.entry(menu, "미확인 결과 모두 확인 처리 (0개)")
        self.assertFalse(menu.items[index]["enabled"])
        menu.invoke(index)
        self.assertIs(self.strip._context_menu, menu)
        self.assertEqual(len([item for item in menu.items if item]), 8)

    def test_keyboard_skips_disabled_and_submenu_returns_to_parent(self):
        menu = self.context_menu("project")
        menu.select(self.entry(menu, "이 프로젝트 숨기기"))
        menu.step(1)
        self.assertEqual(menu.items[menu.active]["label"], "handback 등록 해제…")
        menu.step(1)
        menu.open_child(keyboard=True)
        child = menu.child
        self.assertEqual(child.items[child.active]["label"].strip("✓ "), "2줄")
        child.step(-1)
        self.assertEqual(child.items[child.active]["label"], "순서 초기화 (최근 활동순)")
        child.back()
        self.assertIsNone(menu.child)
        self.assertIs(self.strip._context_menu, menu)

    def test_keyboard_bindings_invoke_and_mouse_hover_opens_submenu(self):
        menu = self.context_menu()
        menu.window.update()
        menu.window.event_generate("<Down>")
        self.assertEqual(menu.active, self.entry(menu, "보기 설정 ▶"))
        menu.window.event_generate("<Right>")
        child = menu.child
        child.window.update()
        self.assertEqual(child.items[child.active]["label"].strip("✓ "), "2줄")
        child.window.event_generate("<Down>")
        child.window.event_generate("<Return>")
        self.assertIsNone(self.strip._context_menu)
        self.assertEqual(dashboard.load_prefs(self.home)["max_rows"], 3)
        menu = self.context_menu()
        menu.window.update()
        menu.labels[0].event_generate("<Enter>")
        self.assertIsNotNone(menu.child)
        menu.open_child(keyboard=True)
        menu.child.window.update()
        menu.child.window.event_generate("<Left>")
        self.assertIsNone(menu.child)

    def test_acknowledge_uses_dark_confirm_and_preserves_mail(self):
        checkout = self.home.parent / "project"
        checkout.mkdir()
        project = str(checkout)
        folder = self.home / "projects" / ("a" * 64)
        dashboard.atomic_json(folder / "topology.json",
                             {"root": project, "lead": "claude:test", "workers": ["codex"]})
        message = dashboard.inbox.send(folder / "inbox", "test", "codex:test", "result", "result")
        self.strip.unread_counts[project] = 1
        with patch.object(self.strip, "_dialog", return_value=False):
            self.assertFalse(self.strip._acknowledge(project))
        self.assertEqual(len(dashboard.inbox.pending(folder / "inbox")), 1)
        with patch.object(self.strip, "_dialog", return_value=True) as dialog:
            self.assertTrue(self.strip._acknowledge(project))
            self.assertTrue(dialog.call_args_list[0].kwargs["confirm"])
            self.assertEqual(dialog.call_args_list[1].args, ("정리 완료", "1개를 확인 처리했습니다."))
        self.assertEqual(dashboard.inbox.pending(folder / "inbox"), [])
        self.assertTrue((folder / "inbox" / (message["id"] + ".json")).exists())

    def test_escape_outside_focus_and_refresh_close(self):
        menu = self.context_menu()
        menu.window.update()
        menu.window.event_generate("<Escape>")
        self.assertIsNone(self.strip._context_menu)
        menu = self.context_menu()
        menu.outside(SimpleNamespace(x_root=-10000, y_root=-10000))
        self.assertIsNone(self.strip._context_menu)
        menu = self.context_menu()
        with patch.object(menu.window, "focus_displayof", return_value=None):
            menu.check_focus()
        self.assertIsNone(self.strip._context_menu)
        self.context_menu()
        self.strip.refresh(reschedule=False)
        self.assertIsNone(self.strip._context_menu)

    def test_dark_dialog_returns_safe_default_yes_no_escape_and_close(self):
        self.root.deiconify()
        for action, expected in (("예", True), ("아니요", False), ("escape", False), ("close", False)):
            observed = []
            def respond():
                window = next(w for w in self.root.winfo_children() if isinstance(w, self.tk.Toplevel))
                observed.append(window.focus_get().cget("text"))
                self.assertEqual(window.cget("bg"), self.strip.BG)
                if action == "escape":
                    window.event_generate("<Escape>")
                elif action == "close":
                    self.root.tk.call(window.protocol("WM_DELETE_WINDOW"))
                else:
                    next(w for w in self.widgets(window) if isinstance(w, self.tk.Button)
                         and w.cget("text") == action).invoke()
            self.root.after(100, respond)
            self.assertEqual(self.strip._dialog("확인", "테스트 메시지", confirm=True), expected)
            self.assertEqual(observed, ["아니요"])
            self.assertIsNone(self.root.grab_current())

    def test_button_release_binding_updates_anchor_without_releasing_a_project(self):
        # Invoke Tk's registered binding while the test window stays withdrawn.
        binding = self.root.bind("<ButtonRelease-1>")
        callback = binding.split("[", 1)[1].split()[0]
        values = {key: "0" for key in self.root._subst_format}
        values.update({"%W": str(self.root), "%T": "5"})
        self.strip.anchor = None
        expected = (self.root.winfo_x() + self.root.winfo_width(),
                    self.root.winfo_y() + self.root.winfo_height())

        with patch.object(dashboard, "release") as release:
            self.root.tk.call(callback, *(values[key] for key in self.root._subst_format))
            self.assertEqual(self.strip.anchor, expected)
            release.assert_not_called()


class StripLayoutTests(unittest.TestCase):
    def test_pixel_budgets_and_truncation(self):
        measure = lambda text: len(text) * 10
        widths = dashboard.column_widths(measure)
        self.assertEqual(len(widths), 5)
        self.assertGreaterEqual(widths[2], measure("● 진행 99"))
        self.assertGreaterEqual(widths[3], measure("✉ 99"))
        self.assertGreaterEqual(widths[4], measure("59분 전"))
        for text, width, expected in (("abc", 30, "abc"), ("abcd", 30, "ab…"),
                                      ("가나다라", 20, "가…"), ("ab", 5, ""),
                                      ("a\nb", 30, "a b"), ("", 0, "")):
            self.assertEqual(dashboard.truncate_text(text, width, measure), expected)

    def test_display_rows_never_mutates_saved_preferences(self):
        rows = [{"root": str(i), "last_activity": i} for i in range(5)]
        prefs = {"max_rows": 2, "order": [], "hidden": ["0"]}
        self.assertEqual(tuple(map(len, dashboard.display_rows(rows, prefs))), (2, 2))
        self.assertEqual(tuple(map(len, dashboard.display_rows(rows, prefs, True))), (4, 0))
        self.assertEqual(prefs, {"max_rows": 2, "order": [], "hidden": ["0"]})


class PopupPositionTests(unittest.TestCase):
    def test_edges_negative_monitor_and_submenu(self):
        place = dashboard.popup_position
        self.assertEqual(place(790, 590, 260, 300, (0, 0, 800, 600)), (530, 290, 260, 300))
        self.assertEqual(place(-10, 590, 260, 300, (-800, 0, 0, 600)), (-270, 290, 260, 300))
        self.assertEqual(place(0, 0, 900, 700, (0, 0, 800, 600)), (0, 0, 800, 600))
        self.assertEqual(place(540, 500, 260, 300, (0, 0, 800, 600), (540, 500, 800, 520)),
                         (280, 300, 260, 300))
        self.assertEqual(place(0, 10, 260, 300, (0, 0, 800, 600), (0, 10, 260, 30)),
                         (260, 10, 260, 300))


if __name__ == "__main__":
    unittest.main()
