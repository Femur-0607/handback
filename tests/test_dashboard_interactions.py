"""Click/drag intent, overflow attention, and retained tooltip behavior."""
import copy
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from handback import dashboard
from tests import test_dashboard_compact as compact


class DashboardInteractionTests(compact.CompactGuiTests):
    def status_fixture(self, kind="running"):
        dashboard.save_prefs({**dashboard.DEFAULT_PREFS, "compact_x": 200,
                              "compact_y": 300}, self.home)
        rows = self.sample_rows()
        rows[0]["running"][0]["handle"] = "codex:11111111-1111-1111-1111-111111111111"
        rows[0]["unread"][0]["sender"] = "codex:22222222-2222-2222-2222-222222222222"
        self.strip.render(rows)
        self.root.deiconify()
        self.root.update()
        column = 1 if kind == "running" else 2
        status = self.strip.frame.winfo_children()[0].winfo_children()[column]
        self.cursor[:] = [status.winfo_rootx()+2, status.winfo_rooty()+2]
        return rows, status, tuple(self.cursor)

    def test_status_drag_moves_widget_without_opening_conversation_even_without_motion_event(self):
        for kind in ("running", "unread"):
            for send_motion in (True, False):
                with self.subTest(kind=kind, send_motion=send_motion):
                    _, status, start = self.status_fixture(kind)
                    with patch.object(self.strip, "opener") as opener:
                        status.event_generate("<Button-1>", x=2, y=2)
                        opener.assert_not_called()
                        self.cursor[:] = [start[0]+80, start[1]+40]
                        if send_motion:
                            status.event_generate("<B1-Motion>", x=2, y=2)
                            self.root.update_idletasks()
                            opener.assert_not_called()
                        status.event_generate("<ButtonRelease-1>", x=2, y=2)
                        self.root.update_idletasks()
                        opener.assert_not_called()
                    self.assertEqual((self.root.winfo_x(), self.root.winfo_y()), (280, 340))
                    self.assertIsNone(self.strip.panel)
                    self.assertFalse(dashboard.load_prefs(self.home)["docked"])

    def test_small_status_pointer_jitter_waits_for_release_without_capturing(self):
        _, status, start = self.status_fixture()
        with patch.object(self.strip, "opener") as opener, \
                patch.object(self.root, "grab_set", wraps=self.root.grab_set) as capture:
            status.event_generate("<Button-1>", x=2, y=2)
            self.cursor[0] = start[0] + self.strip._drag_threshold-1
            status.event_generate("<B1-Motion>", x=2, y=2)
            opener.assert_not_called()
            capture.assert_not_called()
            status.event_generate("<ButtonRelease-1>", x=2, y=2)
            opener.assert_called_once_with("codex://threads/11111111-1111-1111-1111-111111111111")
        self.assertEqual((self.root.winfo_x(), self.root.winfo_y()), (200, 300))
        self.assertIsNone(self.strip.panel)

    def test_cancelled_status_press_and_lost_drag_capture_never_open_conversation(self):
        for interruption in ("unmap_before_drag", "lost_capture"):
            with self.subTest(interruption=interruption):
                _, status, start = self.status_fixture()
                with patch.object(self.strip, "opener") as opener:
                    status.event_generate("<Button-1>", x=2, y=2)
                    if interruption == "unmap_before_drag":
                        self.strip._drag_unmapped(SimpleNamespace(widget=self.root))
                    else:
                        self.cursor[:] = [start[0]+80, start[1]+40]
                        status.event_generate("<B1-Motion>", x=2, y=2)
                        self.root.update_idletasks()
                        self.root.grab_release()
                        self.root.after_cancel(self.strip._drag_capture_timer)
                        self.strip._watch_drag_capture()
                    opener.assert_not_called()
                    status.event_generate("<ButtonRelease-1>", x=2, y=2)
                    opener.assert_not_called()
                self.assertIsNone(self.strip.panel)
                self.assertIsNone(self.strip._start)

    def test_status_target_removed_before_release_does_not_open_or_toggle_panel(self):
        rows, status, _ = self.status_fixture()
        with patch.object(self.strip, "opener") as opener:
            status.event_generate("<Button-1>", x=2, y=2)
            current = copy.deepcopy(rows)
            current[0]["running"] = []
            self.strip.render(current)
            self.root.event_generate("<ButtonRelease-1>")
            opener.assert_not_called()
        self.assertIsNone(self.strip.panel)

    def test_overflow_unread_changes_attention_without_visible_text_changes_and_ignores_hidden_projects(self):
        dashboard.save_prefs(dashboard.DEFAULT_PREFS, self.home)
        rows = self.sample_rows()[:4]
        for row in rows:
            row["running"], row["unread"] = [], []
        self.strip.render(rows)

        def labels():
            return [widget for widget in self.widgets(self.strip.frame)
                    if isinstance(widget, self.tk.Label)]

        before = [label.cget("text") for label in labels()]
        self.assertEqual(labels()[-1].cget("text"), "+2")
        self.assertEqual(labels()[-1].cget("fg"), self.strip.DIM)
        current = copy.deepcopy(rows)
        current[2]["unread"] = [{}, {}]
        self.strip.render(current)
        self.assertEqual([label.cget("text") for label in labels()], before)
        self.assertEqual(labels()[-1].cget("fg"), self.strip.YELLOW)
        dashboard.save_prefs({**dashboard.DEFAULT_PREFS, "hidden": [current[2]["root"]]}, self.home)
        self.strip.render(current)
        self.assertEqual(labels()[-1].cget("text"), "+1")
        self.assertEqual(labels()[-1].cget("fg"), self.strip.DIM)
        current[3]["unread"] = [{}]
        self.strip.render(current)
        self.assertEqual(labels()[-1].cget("text"), "+1")
        self.assertEqual(labels()[-1].cget("fg"), self.strip.YELLOW)

    def error_tooltip_fixture(self, mode):
        self.strip.tooltip.hide()
        dashboard.save_prefs({**dashboard.DEFAULT_PREFS, "mode": mode,
                              "compact_x": 200, "compact_y": 300}, self.home)
        rows = self.sample_rows()
        rows[0].update(unreadable=1, first_unreadable="project/requests/broken.json",
                       read_error="invalid JSON")
        self.strip.render(rows)
        self.root.deiconify()
        self.root.update()
        target = next(widget for widget in self.widgets(self.strip.frame)
                      if isinstance(widget, self.tk.Label)
                      and widget.cget("text").startswith("읽기 오류"))
        return rows, target

    def test_unchanged_refresh_preserves_pending_and_visible_tooltip_until_its_content_changes(self):
        for mode in ("taskbar", "panel"):
            with self.subTest(mode=mode):
                rows, target = self.error_tooltip_fixture(mode)
                self.strip.tooltip.schedule(target, dashboard.read_error_text(rows[0]))
                pending = self.strip.tooltip.pending
                with patch.object(dashboard, "snapshot", return_value=rows):
                    self.strip.refresh(reschedule=False)
                self.assertEqual(self.strip.tooltip.pending, pending)
                self.assertTrue(target.winfo_exists())
                self.strip.tooltip.show(target, dashboard.read_error_text(rows[0]))
                tooltip = self.strip.tooltip.window
                with patch.object(dashboard, "snapshot", return_value=rows):
                    self.strip.refresh(reschedule=False)
                self.assertIs(self.strip.tooltip.window, tooltip)
                self.assertTrue(target.winfo_exists())
                current = copy.deepcopy(rows)
                current[0]["read_error"] = "permission denied"
                with patch.object(dashboard, "snapshot", return_value=current):
                    self.strip.refresh(reschedule=False)
                self.assertIsNone(self.strip.tooltip.window)
                self.assertIsNone(self.strip.tooltip.pending)

    def test_destroyed_tooltip_target_cancels_pending_or_visible_tooltip(self):
        for mode, visible in (("taskbar", False), ("panel", True)):
            with self.subTest(mode=mode, visible=visible):
                rows, target = self.error_tooltip_fixture(mode)
                if visible:
                    self.strip.tooltip.show(target, dashboard.read_error_text(rows[0]))
                else:
                    self.strip.tooltip.schedule(target, dashboard.read_error_text(rows[0]))
                target.destroy()
                self.assertIsNone(self.strip.tooltip.pending)
                self.assertIsNone(self.strip.tooltip.window)

    def test_rebuilt_panel_retains_visible_tooltip_when_other_activity_or_age_changes(self):
        for change in ("other_activity", "displayed_age"):
            with self.subTest(change=change), patch.object(dashboard, "ago", return_value="방금") as age:
                rows, target = self.error_tooltip_fixture("panel")
                error = dashboard.read_error_text(rows[0])
                self.strip.tooltip.show(target, error)
                window = self.strip.tooltip.window
                rect = dashboard.DarkTooltip._rect(target)
                current = copy.deepcopy(rows)
                if change == "other_activity":
                    current[2]["last_activity"] -= 120
                else:
                    age.return_value = "1분 전"
                with patch.object(dashboard, "snapshot", return_value=current):
                    self.strip.refresh(reschedule=False)
                self.root.update()
                self.assertFalse(target.winfo_exists(), "Exercise a row rebuild, not just the render cache")
                self.assertIs(self.strip.tooltip.window, window)
                self.assertEqual(self.strip.tooltip.text, error)
                self.assertEqual(dashboard.DarkTooltip._rect(self.strip.tooltip.target), rect)

    def test_rebuilt_panel_preserves_original_tooltip_deadline_and_uses_replacement_target(self):
        with patch.object(dashboard, "ago", return_value="방금"):
            rows, target = self.error_tooltip_fixture("panel")
            self.strip.tooltip.schedule(target, dashboard.read_error_text(rows[0]))
            pending = self.strip.tooltip.pending
            original_deadline = self.tk.BooleanVar(self.root)
            self.root.after(550, lambda: original_deadline.set(True))
            midway = self.tk.BooleanVar(self.root)
            self.root.after(250, lambda: midway.set(True))
            self.root.wait_variable(midway)
            current = copy.deepcopy(rows)
            current[2]["last_activity"] -= 120
            with patch.object(dashboard, "snapshot", return_value=current):
                self.strip.refresh(reschedule=False)
            self.assertEqual(self.strip.tooltip.pending, pending)
            self.root.update()
            replacement = self.strip.tooltip.target
            self.assertIsNot(replacement, target)
            self.assertFalse(target.winfo_exists())
            # Rendering itself may cross the deadline on a busy Windows host.
            if not original_deadline.get():
                self.root.wait_variable(original_deadline)
            self.assertIsNotNone(self.strip.tooltip.window)
            self.assertIsNone(self.strip.tooltip.pending)
            self.assertIs(self.strip.tooltip.target, replacement)

    def test_panel_rebuild_hides_tooltip_when_target_disappears_or_moves(self):
        for change in ("removed", "moved"):
            for visible in (False, True):
                with self.subTest(change=change, visible=visible), \
                        patch.object(dashboard, "ago", return_value="방금"):
                    rows, target = self.error_tooltip_fixture("panel")
                    if visible:
                        self.strip.tooltip.show(target, dashboard.read_error_text(rows[0]))
                    else:
                        self.strip.tooltip.schedule(target, dashboard.read_error_text(rows[0]))
                    current = copy.deepcopy(rows)
                    if change == "removed":
                        current = current[1:]
                    else:
                        prefs = dashboard.load_prefs(self.home)
                        prefs["order"] = [rows[1]["root"], rows[0]["root"]]
                        dashboard.save_prefs(prefs, self.home)
                    with patch.object(dashboard, "snapshot", return_value=current):
                        self.strip.refresh(reschedule=False)
                    self.root.update()
                    self.assertIsNone(self.strip.tooltip.target)
                    self.assertIsNone(self.strip.tooltip.pending)
                    self.assertIsNone(self.strip.tooltip.window)


def load_tests(loader, tests, pattern):
    names = sorted(name for name, method in DashboardInteractionTests.__dict__.items()
                   if name.startswith("test_") and callable(method))
    return unittest.TestSuite(DashboardInteractionTests(name) for name in names)


if __name__ == "__main__":
    unittest.main()
