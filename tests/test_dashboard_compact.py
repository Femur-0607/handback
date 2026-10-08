from types import SimpleNamespace
import gc
import unittest
from unittest.mock import Mock, patch

from handback import dashboard
from tests.test_dashboard_gui import DashboardMenuTests


class CompactPureTests(unittest.TestCase):
    @unittest.skipUnless(dashboard.sys.platform == "win32", "Windows z-order")
    def test_zorder_only_repairs_taskbar_occlusion_without_activation(self):
        import ctypes
        widget = Mock()
        widget.winfo_id.return_value = 11
        user = Mock()
        user.GetAncestor.return_value = 12
        user.FindWindowW.return_value = 20
        with patch.object(ctypes.windll, "user32", user):
            widget.winfo_ismapped.return_value = False
            dashboard.keep_above_taskbar(widget)
            user.GetAncestor.assert_not_called()
            widget.winfo_ismapped.return_value = True
            user.GetWindow.side_effect = [30, 20]
            dashboard.keep_above_taskbar(widget)
            user.SetWindowPos.assert_called_once_with(12, -1, 0, 0, 0, 0, 0x13)
            user.SetWindowPos.reset_mock()
            user.GetWindow.side_effect = [30, 0]
            dashboard.keep_above_taskbar(widget)
            user.SetWindowPos.assert_not_called()
            user.FindWindowW.return_value = None
            dashboard.keep_above_taskbar(widget)
            user.SetWindowPos.assert_not_called()

    def test_palette_dominant_shade_light_contrast_and_fallback(self):
        choose = dashboard.compact_palette
        self.assertEqual(choose()["bg"], "#1c1c1c")
        self.assertEqual(choose([None, (999, 0, 0), (1, 2), (True, 0, 0)])["bg"], "#1c1c1c")
        self.assertEqual(choose([(33, 33, 33), (35, 35, 35), (34, 34, 34),
                                 (255, 0, 0), (0, 0, 255)])["bg"], "#222222")
        self.assertEqual(choose([(236, 236, 236)])["fg"], "#202020")
        for rgb in ((28, 28, 28), (118, 118, 118), (236, 236, 236), (255, 255, 255)):
            palette = choose([rgb])
            background = dashboard._luminance(rgb)
            for role in ("fg", "dim", "green", "yellow"):
                color = palette[role]
                foreground = dashboard._luminance(tuple(int(color[i:i+2], 16) for i in (1, 3, 5)))
                self.assertGreaterEqual((max(background, foreground)+.05) / (min(background, foreground)+.05), 4.5)

    def test_sample_points_exclude_widget_and_unavailable_taskbars(self):
        info = {"rect": (-1920, 1032, 0, 1080), "edge": 3, "auto_hide": False}
        excluded = (-1300, 1032, -900, 1080)
        points = dashboard.taskbar_sample_points(info, 144, excluded)
        self.assertTrue(points)
        self.assertLessEqual(len(points), 18)
        self.assertTrue(all(-1920 < x < 0 and y in (1038, 1073) for x, y in points))
        self.assertFalse(any(-1300 <= x < -900 for x, y in points))
        for unavailable in (None, {**info, "auto_hide": True}, {**info, "edge": 0}):
            self.assertEqual(dashboard.taskbar_sample_points(unavailable, 96), [])

    def test_geometry_bottom_notification_offsets_and_clamping(self):
        info = {"rect": (-1920, 1032, 0, 1080), "edge": 3, "auto_hide": False,
                "notification_left": -200}
        area = (-1920, 0, 0, 1032)
        place = dashboard.compact_geometry
        self.assertEqual(place(info, area, 250, 30), (-450, 1032, 250, 48))
        self.assertEqual(place(info, area, 250, 30, -9999), (-1920, 1032, 250, 48))
        self.assertEqual(place(info, area, 250, 30, 50), (-250, 1032, 250, 48))
        self.assertEqual(place({**info, "notification_left": None}, area, 250, 30), (-250, 1032, 250, 48))
        for other in (None, {**info, "auto_hide": True}, {**info, "edge": 0},
                      {**info, "edge": 1}, {**info, "edge": 2}):
            self.assertEqual(place(other, area, 250, 30), (-258, 998, 250, 30))

    def test_compact_lines_order_hidden_counts_and_truncation(self):
        rows = [{"root": str(i), "name": "long name", "last_activity": 1700000000+i,
                 "running": [{}] * i, "unread": [{}] * (i % 2)} for i in range(5)]
        prefs = {**dashboard.DEFAULT_PREFS, "order": ["1"], "hidden": ["4"], "max_rows": 1}
        lines = dashboard.compact_lines(rows, prefs, 4, len)
        self.assertEqual([v["row"]["root"] for v in lines], ["1", "3"])
        self.assertEqual(lines[0]["name"], "lon…")
        self.assertEqual((lines[0]["running"], lines[0]["unread"], lines[0]["more"]), ("● 1", "✉ 1", ""))
        self.assertEqual(lines[1]["more"], "+2")
        idle = dashboard.compact_lines(rows[:1], dashboard.DEFAULT_PREFS, 100, len)[0]
        self.assertEqual((idle["running"], idle["unread"]), ("대기", ""))
        self.assertEqual(dashboard.compact_lines([], prefs, 4, len), [])

    def test_ago_rejects_invalid_and_implausible_values(self):
        now = 1800000000
        for value in (None, "today", "1700000000", True, [], {}, 1, 0.1, -1,
                      946684799, float("nan"), float("inf"), now + 86401, 10**1000):
            self.assertEqual(dashboard.ago(value, now), "-", repr(value))
        self.assertEqual(dashboard.ago(now + 86400, now), "방금")
        self.assertEqual(dashboard.ago(now + 1, now), "방금")
        self.assertEqual(dashboard.ago(946684800, now=946684800), "방금")


class CompactGuiTests(DashboardMenuTests):
    @unittest.skipUnless(dashboard.sys.platform == "win32", "Windows z-order")
    def test_zorder_timer_is_singleton_across_ticks_refresh_and_mode_switches(self):
        dashboard.save_prefs(dashboard.DEFAULT_PREFS, self.home)
        with patch.object(dashboard, "keep_above_taskbar") as repair:
            self.strip.render([])
            baseline = len(self.commands())
            for _ in range(100):
                timer = self.strip._zorder_timer
                # Run the registered Tcl callback as the event loop would, then
                # remove the original scheduled event before its deadline.
                callback = self.root.tk.call("after", "info", timer)[0]
                self.root.tk.call(callback)
                self.root.after_cancel(timer)
                self.strip._sync_zorder_timer()
                self.assertEqual(len(self.commands()), baseline)
                self.assertEqual(len(self.root.tk.call("after", "info")), 1)
            self.assertEqual(repair.call_count, 101)
            dashboard.save_prefs({**dashboard.DEFAULT_PREFS, "mode": "panel"}, self.home)
            self.strip.render([])
            self.assertIsNone(self.strip._zorder_timer)
            self.assertEqual(len(self.root.tk.call("after", "info")), 0)
            dashboard.save_prefs(dashboard.DEFAULT_PREFS, self.home)
            self.strip.render([])
            self.assertIsNotNone(self.strip._zorder_timer)

    # Reuse fixture helpers, without inheriting the panel tests in discovery.
    def handles(self):
        # Earlier tests can leave Python cycles owning destroyed Tk interpreters.
        # Collect them before BOTH samples so their later cleanup cannot skew equality.
        gc.collect()
        self.root.update_idletasks()
        return super().handles()

    def test_sampled_background_and_scaled_padding_align_both_lines(self):
        dashboard.save_prefs(dashboard.DEFAULT_PREFS, self.home)
        self.root.deiconify()
        with patch.object(dashboard, "taskbar_colors", return_value=[(236, 236, 236)]):
            for dpi in (96, 120, 144):
                with patch.object(dashboard, "_window_dpi", return_value=dpi):
                    self.strip.render(self.sample_rows())
                self.root.update_idletasks()
                padding = round(7 * dpi / 96)
                self.assertEqual(int(self.strip.frame.cget("padx")), padding)
                self.assertEqual(self.root.cget("bg"), "#ececec")
                positions = []
                for row in self.strip.frame.winfo_children():
                    name = row.winfo_children()[0]
                    self.assertEqual(name.cget("fg"), "#202020")
                    self.assertEqual(name.cget("bg"), "#ececec")
                    positions.append(row.winfo_x() + name.winfo_x())
                    last = row.winfo_children()[-1]
                    self.assertGreaterEqual(self.root.winfo_width() - row.winfo_x()
                                            - last.winfo_x() - last.winfo_width(), padding)
                self.assertEqual(positions, [padding, padding])

    def test_palette_sampling_is_cached_until_interval_or_position_change(self):
        dashboard.save_prefs(dashboard.DEFAULT_PREFS, self.home)
        with patch.object(dashboard, "taskbar_colors", return_value=[(32, 32, 32)]) as sample, \
                patch.object(dashboard.time, "monotonic", return_value=100) as clock, \
                patch.object(self.root, "winfo_ismapped", return_value=False):
            self.strip.render(self.sample_rows())
            self.strip.render(self.sample_rows())
            self.assertEqual(sample.call_count, 1)
            clock.return_value = 130
            self.strip.render(self.sample_rows())
            self.assertEqual(sample.call_count, 2)
            with patch.object(self.root, "winfo_ismapped", return_value=True):
                self.strip.render(self.sample_rows())
            self.assertEqual(sample.call_count, 3)

    def test_compact_500_refreshes_and_100_panel_cycles(self):
        dashboard.save_prefs(dashboard.DEFAULT_PREFS, self.home)
        rows = self.sample_rows()
        self.root.deiconify()
        baseline = handles = None
        for i in range(500):
            self.strip.render(rows)
            self.root.update()
            current = (len(self.widgets(self.root)), len(self.commands()), len(self.root.tk.call("after", "info")))
            if baseline is None:
                baseline = current
            self.assertEqual(current, baseline)
            if i == 99:
                handles = self.handles()
        self.assertEqual(self.handles(), handles)
        for i in range(100):
            self.strip._toggle_panel()
            panel = self.strip.panel
            self.assertIsNotNone(panel)
            self.strip.render(rows)
            self.assertIs(self.strip.panel, panel)
            self.assertLessEqual(panel.winfo_y() + panel.winfo_height(), self.root.winfo_y())
            self.strip._collapse_panel()
            self.root.update()
            self.assertEqual((len(self.widgets(self.root)), len(self.commands()),
                              len(self.root.tk.call("after", "info"))), baseline)
            if i == 9:
                handles = self.handles()
        self.assertEqual(self.handles(), handles)

    def test_click_drag_persistence_status_and_mode_switch(self):
        dashboard.save_prefs(dashboard.DEFAULT_PREFS, self.home)
        rows = self.sample_rows()
        rows[0]["running"][0]["handle"] = self.conversation_fixture()[0]["handle"]
        self.strip.render(rows)
        self.root.deiconify()
        self.root.update()
        event = SimpleNamespace(x_root=self.root.winfo_x()+5, y_root=self.root.winfo_y()+5)
        self.strip._press(event)
        self.strip._release(event)
        self.assertIsNotNone(self.strip.panel)
        self.strip.panel.event_generate("<Escape>")
        self.root.update()
        self.assertIsNone(self.strip.panel)
        self.strip._press(event)
        moved = SimpleNamespace(x_root=event.x_root-50, y_root=event.y_root-100)
        old_y = self.root.winfo_y()
        self.strip._drag(moved)
        self.root.update_idletasks()
        self.strip._release(moved)
        self.assertEqual(self.root.winfo_y(), old_y)
        self.assertEqual(dashboard.load_prefs(self.home)["compact_x"], self.root.winfo_x())
        self.assertIsNone(self.strip.panel)
        with patch.object(self.strip, "opener") as opener:
            status = self.strip.frame.winfo_children()[0].winfo_children()[1]
            status.event_generate("<Button-1>")
            status.event_generate("<ButtonRelease-1>")
            opener.assert_called_once()
            self.assertIsNone(self.strip.panel)
        self.strip._toggle_panel()
        with patch.object(dashboard, "snapshot", return_value=rows):
            self.strip._save({**dashboard.load_prefs(self.home), "mode": "panel"})
        self.assertIsNone(self.strip.panel)
        self.assertEqual(self.strip.mode, "panel")

    def test_compact_fonts_fit_two_lines_at_common_scales(self):
        dashboard.save_prefs(dashboard.DEFAULT_PREFS, self.home)
        original = self.root.tk.call("tk", "scaling")
        try:
            for scale in (1, 1.25, 1.5):
                self.root.tk.call("tk", "scaling", 96 * scale / 72)
                height = int(40 * scale)
                info = {"rect": (0, 1080-height, 1920, 1080), "edge": 3,
                        "auto_hide": False, "notification_left": 1700}
                with patch.object(dashboard, "taskbar_info", return_value=info), \
                        patch.object(dashboard, "_window_dpi", return_value=96 * scale):
                    self.strip.render(self.sample_rows())
                self.assertLessEqual(self.strip.compact_metrics.metrics("linespace") * 2, height)
                self.assertEqual(self.strip._compact_geometry[-1], height)
        finally:
            self.root.tk.call("tk", "scaling", original)

    def test_destroy_cancels_refresh_and_outside_poll_callbacks(self):
        dashboard.save_prefs(dashboard.DEFAULT_PREFS, self.home)
        with patch.object(dashboard, "snapshot", return_value=self.sample_rows()):
            self.strip.refresh()
        self.strip._toggle_panel()
        self.assertEqual(len(self.root.tk.call("after", "info")),
                         3 if dashboard.sys.platform == "win32" else 2)
        self.root.destroy()
        self.assertEqual(len(self.root.tk.call("after", "info")), 0)

    @unittest.skipUnless(dashboard.sys.platform == "win32", "Windows pointer polling")
    def test_open_discards_clicks_from_before_panel_existed(self):
        import ctypes
        dashboard.save_prefs(dashboard.DEFAULT_PREFS, self.home)
        self.strip.render(self.sample_rows())
        with patch.object(ctypes.windll.user32, "GetAsyncKeyState", side_effect=[1, 1, 0, 0]):
            self.strip._toggle_panel()
        self.assertIsNotNone(self.strip.panel)
        self.strip._collapse_panel()

    @unittest.skipUnless(dashboard.sys.platform == "win32", "Windows pointer polling")
    def test_outside_click_dismisses_panel_and_cancels_timer(self):
        import ctypes
        from ctypes import wintypes
        dashboard.save_prefs(dashboard.DEFAULT_PREFS, self.home)
        self.strip.render(self.sample_rows())
        self.strip._toggle_panel()
        self.root.after_cancel(self.strip._outside_timer)
        def outside(pointer):
            point = ctypes.cast(pointer, ctypes.POINTER(wintypes.POINT)).contents
            point.x, point.y = -9999, -9999
            return 1
        with patch.object(ctypes.windll.user32, "GetAsyncKeyState", return_value=0x8000), \
                patch.object(ctypes.windll.user32, "GetCursorPos", side_effect=outside):
            self.strip._mouse_down = False
            self.strip._watch_outside()
        self.assertIsNone(self.strip.panel)
        self.assertIsNone(self.strip._outside_timer)


# Only the new cases run on this subclass; the base module covers panel behavior.
for _name in dir(DashboardMenuTests):
    if _name.startswith("test_"):
        setattr(CompactGuiTests, _name, None)
del DashboardMenuTests
