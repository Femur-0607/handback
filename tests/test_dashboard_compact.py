from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from handback import dashboard
from tests import gui_resources
from tests.test_dashboard_gui import DashboardMenuTests


class CompactPureTests(unittest.TestCase):
    def test_fullscreen_decision_monitor_shell_and_notification_states(self):
        decide = dashboard.fullscreen_covers_widget
        monitor = (-1920, 0, 0, 1080)
        for state in (None, 1, 2, 3, 4, 5, 6, 7):
            self.assertTrue(decide(monitor, monitor, 10, 10, notification_state=state))
            self.assertFalse(decide((-1920, 0, 0, 1032), monitor, 10, 10,
                                    notification_state=state))
            self.assertFalse(decide(monitor, monitor, 10, 20, notification_state=state))
        for name in ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"):
            self.assertFalse(decide(monitor, monitor, 10, 10, name))
        self.assertFalse(decide(monitor, monitor, 10, 10, own_window=True))
        self.assertFalse(decide(None, monitor, 10, 10))
        self.assertFalse(decide(monitor, monitor, None, None))
        self.assertTrue(decide((-1921, -1, 1, 1081), monitor, 10, 10))

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
        self.assertEqual(place(info, area, 250, 30, 50), (-450, 1032, 250, 48))
        self.assertEqual(place({**info, "notification_left": None}, area, 250, 30), (-250, 1032, 250, 48))
        for other in (None, {**info, "auto_hide": True}, {**info, "edge": 0},
                      {**info, "edge": 2}):
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

    def test_compact_read_error_is_distinct_from_idle_and_partial_running_counts(self):
        row = {"root": "r", "name": "project", "last_activity": 0,
               "running": [], "unread": [], "unreadable": 2}
        for running in ([], [{}]):
            with self.subTest(running=running):
                row["running"] = running
                [line] = dashboard.compact_lines([row], dashboard.DEFAULT_PREFS, 100, len)
                self.assertEqual(line["running"], "읽기 오류 2")

    def test_floating_corners_snap_with_dpi_scaled_margin_and_threshold(self):
        area = (-1920, -200, 0, 832)
        place = dashboard.floating_geometry
        for dpi in (96, 144, 192):
            margin, distance = round(8 * dpi / 96), round(12 * dpi / 96)
            left, top = area[0] + margin, area[1] + margin
            right, bottom = area[2] - 250 - margin, area[3] - 32 - margin
            self.assertEqual(place(area, 250, 32, dpi=dpi), (right, bottom, 250, 32))
            for x, dx in ((left, 1), (right, -1)):
                for y, dy in ((top, 1), (bottom, -1)):
                    with self.subTest(dpi=dpi, corner=(x, y)):
                        self.assertEqual(place(area, 250, 32, x + dx * distance,
                                               y + dy * distance, dpi), (x, y, 250, 32))
                        outside = (x + dx * (distance + 1), y + dy * (distance + 1))
                        self.assertEqual(place(area, 250, 32, *outside, dpi), (*outside, 250, 32))

    def test_floating_position_and_size_stay_inside_small_work_area(self):
        place = dashboard.floating_geometry
        self.assertEqual(place((0, 0, 1920, 1032), 250, 32, 600, 500), (600, 500, 250, 32))
        self.assertEqual(place((-1920, -200, 0, 832), 250, 32, -9999, -9999),
                         (-1912, -192, 250, 32))
        self.assertEqual(place((0, 0, 100, 30), 250, 32), (0, 0, 100, 30))
        self.assertEqual(place((0, 0, 100, 30), 96, 28), (2, 1, 96, 28))

    def test_free_drag_never_docks_and_respects_taskbar_edges(self):
        info = {"rect": (0, 1032, 1920, 1080), "edge": 3, "auto_hide": False,
                "notification_left": 1700}
        monitor = (0, 0, 1920, 1080)
        virtual = (-1920, -200, 1920, 1080)
        snap = dashboard.compact_drag_geometry
        for dpi in (96, 144, 192):
            margin = round(8 * dpi / 96)
            self.assertEqual(snap(info, monitor, virtual, 1800, 1040, 250, 32, dpi),
                             (False, (1920 - 250 - margin, 1032 - 32 - margin, 250, 32)))
        secondary = {**info, "rect": (-1920, 832, 0, 880), "notification_left": None}
        self.assertEqual(snap(secondary, (-1920, -200, 0, 880), virtual, -900, 810, 250, 32),
                         (False, (-900, 792, 250, 32)))
        self.assertEqual(snap(info, (-1920, -200, 0, 880), virtual, -900, 810, 250, 32),
                         (False, (-900, 810, 250, 32)))
        top = {**info, "rect": (0, 0, 1920, 48), "edge": 1}
        self.assertEqual(snap(top, monitor, virtual, 700, 0, 250, 32),
                         (False, (700, 56, 250, 32)))
        left = {**info, "rect": (0, 0, 48, 1080), "edge": 0}
        self.assertEqual(snap(left, monitor, virtual, 0, 300, 250, 32),
                         (False, (56, 300, 250, 32)))
        right = {**info, "rect": (1872, 0, 1920, 1080), "edge": 2}
        self.assertEqual(snap(right, monitor, virtual, 1900, 300, 250, 32),
                         (False, (1614, 300, 250, 32)))
        self.assertEqual(snap({**info, "auto_hide": True}, monitor, virtual, 700, 1080, 250, 32),
                         (False, (700, 1040, 250, 32)))
        self.assertEqual(snap(None, monitor, virtual, 9999, 9999, 250, 32,
                              work_area=(48, 24, 1872, 1000)),
                         (False, (1614, 960, 250, 32)))

    def test_pull_from_dock_uses_attachment_threshold_before_work_area_clamp(self):
        monitor = (0, 0, 1920, 1080)
        info = {"rect": (0, 1008, 1920, 1080), "edge": 3, "auto_hide": False}
        for dpi, threshold in ((96, 20), (144, 30), (192, 40)):
            for pull, expected in ((threshold, True), (threshold+1, False),
                                   (threshold+10, False), (threshold-1, True)):
                docked, geometry = dashboard.compact_drag_geometry(info, monitor, monitor,
                    700, 1008-pull, 299, 48, dpi, from_dock=True)
                self.assertEqual(docked, expected)
                self.assertEqual(geometry[1], 1008 if expected else 1008-48-round(8*dpi/96))
        top = {**info, "rect": (0, 0, 1920, 48), "edge": 1}
        self.assertTrue(dashboard.compact_drag_geometry(top, monitor, monitor,
                        700, 20, 299, 48, from_dock=True)[0])
        self.assertEqual(dashboard.compact_drag_geometry(top, monitor, monitor,
                         700, 21, 299, 48, from_dock=True), (False, (700, 56, 299, 48)))

    def test_drag_absolute_position_never_accumulates_snap_or_event_error(self):
        start = (-1900, -120, -1910, -130)
        for cursor, expected in (((-700, -120), (-710, -130)),
                                 ((50, 50), (40, 40)), ((-1900, -120), (-1910, -130))):
            self.assertEqual(dashboard.drag_position(start, cursor), expected)

    def test_drag_threshold_scales_from_four_logical_pixels(self):
        for dpi, threshold in ((96, 4), (120, 5), (144, 6), (192, 8)):
            self.assertEqual(dashboard.drag_threshold(dpi), threshold)

    def test_cached_monitor_selection_handles_crossings_gaps_and_offscreen_points(self):
        monitors = [(-1920, -200, 0, 880), (100, 0, 2020, 1080)]
        for point, index in (((-1, 20), 0), ((100, 20), 1), ((49, 20), 0),
                             ((51, 20), 1), ((9999, 9999), 1), ((-9999, -9999), 0)):
            self.assertEqual(dashboard.monitor_at_point(monitors, point), monitors[index])

    def test_cached_snap_distance_preserves_attachment_and_tray_clamp(self):
        area = (0, 0, 1920, 1080)
        info = {"rect": (0, 1008, 1920, 1080), "edge": 3,
                "auto_hide": False, "notification_left": 1461}
        self.assertEqual(dashboard.compact_drag_geometry(info, area, area, 1800, 978, 299, 48,
                         from_dock=True, snap_distance=30), (True, (1162, 1008, 299, 72)))
        self.assertEqual(dashboard.compact_drag_geometry(info, area, area, 1800, 977, 299, 48,
                         from_dock=True, snap_distance=30), (False, (1613, 952, 299, 48)))

    @unittest.skipUnless(dashboard.sys.platform == "win32", "Windows native move")
    def test_native_move_caches_owned_hwnd_and_never_activates_resizes_or_reorders(self):
        import ctypes
        widget, user = Mock(), Mock()
        widget.winfo_id.return_value = 11
        user.GetAncestor.return_value = 12
        user.SetWindowPos.return_value = 1
        with patch.object(ctypes.windll, "user32", user):
            move = dashboard._native_drag_mover(widget)
            self.assertTrue(move(-1800, -200))
            self.assertTrue(move(-1700, -100))
        user.GetAncestor.assert_called_once_with(11, 2)
        self.assertEqual(user.SetWindowPos.call_args_list[0].args, (12, None, -1800, -200, 0, 0, 0x15))

    def test_virtual_clamp_and_tray_without_room(self):
        clamp = dashboard.clamp_compact
        self.assertEqual(clamp(-9999, -9999, 250, 32, (-1920, -200, 1920, 1080)),
                         (-1920, -200, 250, 32))
        self.assertEqual(clamp(9999, 9999, 250, 32, (-1920, -200, 1920, 1080)),
                         (1670, 1048, 250, 32))
        info = {"rect": (0, 1032, 1920, 1080), "edge": 3, "auto_hide": False,
                "notification_left": 100}
        self.assertEqual(dashboard.compact_geometry(info, (0, 0, 1920, 1080), 250, 32, 1800),
                         (1670, 1032, 250, 48))

    def test_panel_anchor_above_below_and_monitor_clamp(self):
        panel = dashboard.compact_panel_geometry
        self.assertEqual(panel((800, 1032, 250, 48), 500, 400, (0, 0, 1920, 1032)),
                         (550, 632, 500, 400))
        self.assertEqual(panel((800, 10, 250, 32), 500, 400, (0, 0, 1920, 1032)),
                         (550, 42, 500, 400))
        self.assertEqual(panel((-1910, -190, 250, 32), 500, 400, (-1920, -200, 0, 832)),
                         (-1920, -158, 500, 400))

    def test_ago_rejects_invalid_and_implausible_values(self):
        now = 1800000000
        for value in (None, "today", "1700000000", True, [], {}, 1, 0.1, -1,
                      946684799, float("nan"), float("inf"), now + 86401, 10**1000):
            self.assertEqual(dashboard.ago(value, now), "-", repr(value))
        self.assertEqual(dashboard.ago(now + 86400, now), "방금")
        self.assertEqual(dashboard.ago(now + 1, now), "방금")
        self.assertEqual(dashboard.ago(946684800, now=946684800), "방금")


class CompactGuiTests(DashboardMenuTests):
    def setUp(self):
        if dashboard.sys.platform == "win32":
            import ctypes
            from ctypes import wintypes
            user = ctypes.windll.user32
            user.SetThreadDpiAwarenessContext.argtypes = [wintypes.HANDLE]
            user.SetThreadDpiAwarenessContext.restype = wintypes.HANDLE
            previous = user.SetThreadDpiAwarenessContext(-2)  # Match dashboard.main's system DPI awareness.
            if previous:
                self.addCleanup(user.SetThreadDpiAwarenessContext, previous)
        super().setUp()
        # Absolute drag targets (up to x=1400) and dock/panel placement need one
        # consistent desktop, independent of the CI host's screen size or shell.
        area = (0, 0, 1920, 1080)
        height = round(48 * dashboard._window_dpi(self.root) / 96)
        taskbar = {"rect": (0, area[3] - height, area[2], area[3]), "edge": 3,
                   "auto_hide": False, "notification_left": area[2] - 200}
        for name, value in (("_virtual_area", area), ("_monitor_rects", [area]),
                            ("_monitor_area", area), ("_work_area", (area[2], area[3] - height)),
                            ("taskbar_info", taskbar), ("taskbar_colors", [])):
            fixture = patch.object(dashboard, name, return_value=value)
            fixture.start()
            self.addCleanup(fixture.stop)
        detector = patch.object(dashboard, "widget_fullscreen_covered", return_value=False)
        detector.start()
        self.addCleanup(detector.stop)
        self.cursor = [0, 0]
        pointer = patch.object(self.root, "winfo_pointerxy", side_effect=lambda: tuple(self.cursor))
        pointer.start()
        self.addCleanup(pointer.stop)

    def pointer_event(self, method, event):
        # Synthetic events do not move the desktop cursor. Supply its virtual position.
        self.cursor[:] = [event.x_root, event.y_root]
        return method(event)

    @unittest.skipUnless(dashboard.sys.platform == "win32", "Windows z-order")
    def test_zorder_timer_is_singleton_across_ticks_refresh_and_mode_switches(self):
        dashboard.save_prefs({**dashboard.DEFAULT_PREFS, "docked": True}, self.home)
        with patch.object(dashboard, "keep_above_taskbar") as repair, \
                patch.object(dashboard, "widget_fullscreen_covered", return_value=False):
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
            dashboard.save_prefs({**dashboard.DEFAULT_PREFS, "docked": True}, self.home)
            self.strip.render([])
            self.assertIsNotNone(self.strip._zorder_timer)

    @unittest.skipUnless(dashboard.sys.platform == "win32", "Windows visibility")
    def test_fullscreen_hides_collapses_and_restores_without_repair_while_hidden(self):
        dashboard.save_prefs({**dashboard.DEFAULT_PREFS, "docked": True}, self.home)
        with patch.object(dashboard, "widget_fullscreen_covered", return_value=False):
            self.strip.render([])
        self.root.deiconify()
        self.strip._toggle_panel()
        for hidden in (True, True, False):
            self.root.after_cancel(self.strip._zorder_timer)
            with patch.object(dashboard, "widget_fullscreen_covered", return_value=hidden), \
                    patch.object(dashboard, "show_widget_without_activation",
                                 wraps=dashboard.show_widget_without_activation) as show, \
                    patch.object(dashboard, "keep_above_taskbar") as repair:
                was_hidden = self.strip._fullscreen_hidden
                self.strip._watch_zorder()
                self.assertEqual(self.strip._fullscreen_hidden, hidden)
                self.assertIsNone(self.strip.panel)
                self.assertEqual(show.call_count, int(was_hidden != hidden))
                self.assertEqual(repair.call_count, int(not hidden))
                self.strip.refresh(reschedule=False)
                self.root.update_idletasks()
                self.assertEqual(self.root.state(), "withdrawn" if hidden else "normal")
                self.assertEqual(len(self.root.tk.call("after", "info")), 1)

    def test_sampled_background_and_scaled_padding_align_both_lines(self):
        dashboard.save_prefs({**dashboard.DEFAULT_PREFS, "docked": True}, self.home)
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
        dashboard.save_prefs({**dashboard.DEFAULT_PREFS, "docked": True}, self.home)
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

    def test_unchanged_visuals_do_not_rebuild_style_font_or_geometry(self):
        import copy
        dashboard.save_prefs(dashboard.DEFAULT_PREFS, self.home)
        rows = self.sample_rows()
        with patch.object(dashboard, "taskbar_colors", return_value=[(32, 32, 32)]):
            self.strip.render(rows)
            self.root.deiconify()
            self.root.update_idletasks()
            children = self.strip.frame.winfo_children()
            with patch.object(self.strip, "_render_compact_items", wraps=self.strip._render_compact_items) as items, \
                    patch.object(self.root, "geometry", wraps=self.root.geometry) as geometry, \
                    patch.object(self.root, "attributes", wraps=self.root.attributes) as alpha, \
                    patch.object(self.root, "configure", wraps=self.root.configure) as root_style, \
                    patch.object(self.strip.frame, "configure", wraps=self.strip.frame.configure) as frame_style, \
                    patch.object(self.strip.compact_metrics, "configure", wraps=self.strip.compact_metrics.configure) as font:
                for _ in range(50):
                    rows = copy.deepcopy(rows)
                    for row in rows:
                        row["last_activity"] += 3
                    self.strip.render(rows)
                items.assert_not_called()
                geometry.assert_not_called()
                alpha.assert_not_called()
                root_style.assert_not_called()
                frame_style.assert_not_called()
                font.assert_not_called()
                self.assertEqual(self.strip.frame.winfo_children(), children)
                rows[0]["running"].append({})
                self.strip.render(rows)
                items.assert_called_once()
                geometry.assert_not_called()
                alpha.assert_not_called()
                root_style.assert_not_called()
                frame_style.assert_not_called()
                self.assertEqual(self.strip.frame.winfo_children()[0].winfo_children()[1].cget("text"), "● 2")

    def test_retained_status_label_opens_current_conversation(self):
        import copy
        sampler = patch.object(dashboard, "taskbar_colors", return_value=[(32, 32, 32)])
        sampler.start()
        self.addCleanup(sampler.stop)
        dashboard.save_prefs(dashboard.DEFAULT_PREFS, self.home)
        rows = self.sample_rows()
        rows[0]["running"][0]["handle"] = "codex:11111111-1111-1111-1111-111111111111"
        self.strip.render(rows)
        self.root.deiconify()
        self.root.update()
        status = self.strip.frame.winfo_children()[0].winfo_children()[1]
        current = copy.deepcopy(rows)
        current[0]["running"][0]["handle"] = "codex:22222222-2222-2222-2222-222222222222"
        self.strip.render(current)
        self.assertIs(status, self.strip.frame.winfo_children()[0].winfo_children()[1])
        with patch.object(self.strip, "opener") as opener:
            status.event_generate("<Button-1>")
            status.event_generate("<ButtonRelease-1>")
            self.root.update()
            opener.assert_called_once_with("codex://threads/22222222-2222-2222-2222-222222222222")

    def test_small_upward_dock_pull_undocks_and_returns_without_resnapping(self):
        dashboard.save_prefs({**dashboard.DEFAULT_PREFS, "docked": True}, self.home)
        self.strip.render(self.sample_rows())
        self.root.deiconify()
        self.root.update_idletasks()
        x0, y0 = self.root.winfo_x()+10, self.root.winfo_y()+10
        origin_y = self.root.winfo_y()
        threshold = round(20 * dashboard._window_dpi(self.root) / 96)
        self.pointer_event(self.strip._press, SimpleNamespace(x_root=x0, y_root=y0))
        margin = round(8 * dashboard._window_dpi(self.root) / 96)
        for pull, expected in ((threshold, True), (threshold+1, False),
                               (threshold+10, False), (threshold-1, False), (0, False)):
            self.pointer_event(self.strip._drag, SimpleNamespace(x_root=x0, y_root=y0-pull))
            self.root.update_idletasks()
            self.assertEqual(self.strip.docked, expected)
            self.assertEqual(self.root.winfo_y(), origin_y if expected else
                             origin_y-self.root.winfo_height()-margin)
            self.assertEqual(self.strip._drag_from_dock, expected)
        self.pointer_event(self.strip._release, SimpleNamespace(x_root=x0, y_root=y0-threshold+1))
        self.assertFalse(dashboard.load_prefs(self.home)["docked"])

    def test_default_widget_and_free_drag_stop_above_taskbar_without_changing_height(self):
        dashboard.save_prefs(dashboard.DEFAULT_PREFS, self.home)
        self.strip.render(self.sample_rows())
        self.root.deiconify()
        self.root.update()
        height = self.root.winfo_height()
        margin = round(8 * dashboard._window_dpi(self.root) / 96)
        bottom = dashboard.taskbar_info()["rect"][1]
        self.assertFalse(self.strip.docked)
        self.assertEqual(self.root.winfo_y() + height, bottom - margin)
        x0, y0 = self.root.winfo_x() + 10, self.root.winfo_y() + 10
        self.pointer_event(self.strip._press, SimpleNamespace(x_root=x0, y_root=y0))
        moved = SimpleNamespace(x_root=x0, y_root=1080)
        self.pointer_event(self.strip._drag, moved)
        self.root.update_idletasks()
        self.assertFalse(self.strip.docked)
        self.assertEqual(self.root.winfo_height(), height)
        self.assertEqual(self.root.winfo_y() + height, bottom - margin)
        self.pointer_event(self.strip._release, moved)
        self.assertFalse(dashboard.load_prefs(self.home)["docked"])

    def free_drag_fixture(self):
        dashboard.save_prefs({**dashboard.DEFAULT_PREFS, "docked": False,
                              "compact_x": 200, "compact_y": 300}, self.home)
        self.strip.render(self.sample_rows())
        self.root.deiconify()
        self.root.update()
        self.cursor[:] = [210, 310]
        self.strip._press(SimpleNamespace(x_root=-9999, y_root=-9999))

    def test_current_cursor_and_idle_coalescing_ignore_two_hundred_stale_events(self):
        self.free_drag_fixture()
        start = self.strip._start
        with patch.object(self.root, "grab_set", wraps=self.root.grab_set) as capture, \
                patch.object(self.strip, "_move_drag_window", wraps=self.strip._move_drag_window) as move, \
                patch.object(dashboard, "taskbar_info") as taskbar, \
                patch.object(dashboard, "_monitor_rects") as monitors, \
                patch.object(dashboard, "_monitor_area") as monitor, \
                patch.object(dashboard, "_window_dpi") as dpi, \
                patch.object(dashboard, "taskbar_colors") as colors:
            for i in range(1, 201):
                self.cursor[:] = [210+i*6, 310]
                self.strip._drag(SimpleNamespace(x_root=-9999, y_root=-9999))
            timer = self.strip._drag_timer
            self.assertIsNotNone(timer)
            move.assert_not_called()
            capture.assert_called_once()
            self.assertEqual(self.strip._start, start)
            self.root.update_idletasks()
            move.assert_called_once()
            self.assertEqual((self.root.winfo_x(), self.root.winfo_y()), (1400, 300))
            for query in (taskbar, monitors, monitor, dpi, colors):
                query.assert_not_called()
        self.strip._release(SimpleNamespace(x_root=-9999, y_root=-9999))
        self.assertEqual(dashboard.load_prefs(self.home)["compact_x"], 1400)
        self.assertIsNone(self.strip.panel)
        self.assertIsNone(self.strip._drag_timer)
        self.assertIsNone(self.strip._drag_capture_timer)

    def test_threshold_delays_capture_and_click_still_toggles_panel(self):
        self.free_drag_fixture()
        with patch.object(self.root, "grab_set", wraps=self.root.grab_set) as capture:
            self.cursor[0] += self.strip._drag_threshold-1
            self.strip._drag(self.event)
            capture.assert_not_called()
            self.assertIsNone(self.strip._drag_timer)
            self.strip._release(self.event)
            self.assertIsNotNone(self.strip.panel)
            self.strip._collapse_panel()
            self.strip._press(self.event)
            self.cursor[0] += self.strip._drag_threshold
            self.strip._drag(self.event)
            capture.assert_called_once()
            self.strip._release(self.event)
            self.assertIsNone(self.strip.panel)

    def test_release_flushes_latest_cursor_before_idle_and_persists(self):
        self.free_drag_fixture()
        self.cursor[:] = [510, 410]
        self.strip._drag(self.event)
        self.assertIsNotNone(self.strip._drag_timer)
        self.cursor[:] = [710, 510]
        self.strip._release(self.event)
        self.assertEqual((self.root.winfo_x(), self.root.winfo_y()), (700, 500))
        prefs = dashboard.load_prefs(self.home)
        self.assertEqual((prefs["compact_x"], prefs["compact_y"], prefs["docked"]), (700, 500, False))
        self.assertIsNone(self.root.grab_current())
        self.assertIsNone(self.strip._start)

    def test_capture_loss_saves_last_drag_and_suppresses_late_release_click(self):
        self.free_drag_fixture()
        self.cursor[:] = [610, 410]
        self.strip._drag(self.event)
        self.root.grab_release()
        self.cursor[:] = [910, 710]  # Pointer has already left after capture loss.
        self.root.after_cancel(self.strip._drag_capture_timer)
        self.strip._watch_drag_capture()
        self.assertEqual((self.root.winfo_x(), self.root.winfo_y()), (600, 400))
        self.assertEqual(dashboard.load_prefs(self.home)["compact_x"], 600)
        self.strip._release(self.event)
        self.assertIsNone(self.strip.panel)
        self.assertIsNone(self.strip._drag_timer)
        self.assertIsNone(self.strip._drag_capture_timer)

    def test_monitor_crossing_refreshes_only_new_cached_context(self):
        left, right = (0, 0, 900, 1080), (900, 0, 1920, 1080)
        with patch.object(dashboard, "_monitor_rects", return_value=[left, right]) as monitors, \
                patch.object(dashboard, "taskbar_info", return_value=None) as taskbar, \
                patch.object(dashboard, "_window_dpi", return_value=96) as dpi:
            self.free_drag_fixture()
            # free_drag_fixture's render also queries; count only the gesture.
            taskbar.reset_mock()
            dpi.reset_mock()
            dpi.return_value = 144
            for point in ((310, 310), (910, 310), (1010, 310), (9999, 310), (310, 310)):
                self.cursor[:] = point
                self.strip._drag(self.event)
                self.root.update_idletasks()
            taskbar.assert_called_once_with((910, 310))
            dpi.assert_called_once()
            self.assertEqual(len(self.strip._drag_contexts), 2)
            self.assertEqual(self.strip._drag_context[1], left)
            self.strip._release(self.event)

    def test_destroy_during_pending_drag_removes_all_callbacks(self):
        self.free_drag_fixture()
        self.cursor[:] = [510, 410]
        self.strip._drag(self.event)
        self.root.destroy()
        self.assertEqual(len(self.root.tk.call("after", "info")), 0)

    def test_failed_native_move_falls_back_to_tk_geometry(self):
        self.free_drag_fixture()
        self.strip._drag_native_move = Mock(return_value=False)
        self.cursor[:] = [510, 410]
        self.strip._drag(self.event)
        self.root.update_idletasks()
        self.assertEqual((self.root.winfo_x(), self.root.winfo_y()), (500, 400))
        self.strip._drag_native_move.assert_called_once_with(500, 400)
        self.strip._release(self.event)

    def test_row_refresh_during_drag_preserves_new_content_width(self):
        self.free_drag_fixture()
        self.cursor[:] = [510, 410]
        self.strip._drag(self.event)
        self.root.update_idletasks()
        width = self.root.winfo_width()
        rows = self.sample_rows()
        rows[0]["running"] = [{}] * 100000
        rows[0]["unread"] = [{}] * 100000
        self.strip.render(rows)
        self.root.update_idletasks()
        new_width = self.root.winfo_width()
        self.assertGreater(new_width, width)
        self.cursor[0] += 30
        self.strip._drag(self.event)
        self.root.update_idletasks()
        self.assertEqual(self.root.winfo_width(), new_width)
        self.strip._release(self.event)

    def test_palette_skips_fullscreen_black_and_undocked_then_resamples_on_dock(self):
        dashboard.save_prefs({**dashboard.DEFAULT_PREFS, "docked": True}, self.home)
        with patch.object(dashboard, "taskbar_colors", return_value=[(32, 32, 32)]) as sample:
            self.strip.render([])
            good = self.strip._compact_palette
            self.strip._palette_key = None
            with patch.object(dashboard, "widget_fullscreen_covered", return_value=True):
                self.strip.render([])
            self.assertEqual(sample.call_count, 1)
            self.assertEqual(self.strip._compact_palette, good)
            sample.return_value = [(0, 0, 0)]
            self.strip.render([])
            self.assertEqual(self.strip._compact_palette, good)
            dashboard.save_prefs({**dashboard.DEFAULT_PREFS, "docked": False,
                                  "compact_x": 500, "compact_y": 300}, self.home)
            self.strip.render([])
            self.assertEqual(sample.call_count, 2)
            self.assertEqual(self.strip.frame.cget("highlightthickness"), 1)
            self.assertEqual(self.strip.frame.cget("bg"), self.strip.BG)
            # Taskbar attachment is an explicit menu action, including palette refresh.
            self.root.deiconify()
            self.root.update_idletasks()
            menu = self.context_menu()
            settings = self.submenu(menu)
            with patch.object(dashboard, "snapshot", return_value=[]):
                settings.invoke(self.entry(settings, "작업표시줄에 넣기"))
            self.assertTrue(self.strip.docked)
            self.assertTrue(dashboard.load_prefs(self.home)["docked"])
            self.assertEqual(sample.call_count, 3)
            menu = self.context_menu()
            settings = self.submenu(menu)
            with patch.object(dashboard, "snapshot", return_value=[]):
                settings.invoke(self.entry(settings, "작은 위젯"))
            self.root.update_idletasks()
            self.assertFalse(self.strip.docked)
            self.assertFalse(dashboard.load_prefs(self.home)["docked"])
            self.assertEqual((self.root.winfo_x(), self.root.winfo_y()), (500, 300))
            self.assertEqual(sample.call_count, 3)

    @unittest.skipUnless(dashboard.sys.platform == "win32", "Windows timer")
    def test_undocked_fullscreen_uses_same_timer_without_taskbar_repair(self):
        dashboard.save_prefs({**dashboard.DEFAULT_PREFS, "docked": False,
                              "compact_x": 500, "compact_y": 300}, self.home)
        with patch.object(dashboard, "keep_above_taskbar") as repair:
            self.strip.render([])
            baseline = len(self.commands())
            for hidden in (True, True, False):
                self.root.after_cancel(self.strip._zorder_timer)
                with patch.object(dashboard, "widget_fullscreen_covered", return_value=hidden):
                    self.strip._watch_zorder()
                self.assertEqual(self.strip._fullscreen_hidden, hidden)
                self.assertEqual(len(self.commands()), baseline)
                self.assertEqual(len(self.root.tk.call("after", "info")), 1)
            repair.assert_not_called()

    def test_saved_offscreen_free_position_is_clamped_on_load(self):
        dashboard.save_prefs({**dashboard.DEFAULT_PREFS, "docked": False,
                              "compact_x": 99999, "compact_y": -99999}, self.home)
        self.strip.render([])
        x, y, width, height = self.strip._compact_geometry
        margin = round(8 * dashboard._window_dpi(self.root) / 96)
        left, top, right, _ = dashboard._virtual_area(self.root)
        bottom = dashboard.taskbar_info()["rect"][1]
        self.assertEqual((x, y), (right - width - margin, top + margin))
        self.assertLessEqual(y + height, bottom - margin)

    def test_compact_500_refreshes_and_200_panel_cycles(self):
        dashboard.save_prefs(dashboard.DEFAULT_PREFS, self.home)
        rows = self.sample_rows()
        self.root.deiconify()

        def resources():
            return (len(self.widgets(self.root)), len(self.commands()), len(self.root.tk.call("after", "info")))

        def refresh():
            self.strip.render(rows)
            self.root.update()

        def panel_cycle():
            self.strip._toggle_panel()
            panel = self.strip.panel
            self.assertIsNotNone(panel)
            refresh()  # Map/focus the panel before closing it, including native child HWNDs.
            self.assertIs(self.strip.panel, panel)
            self.assertLessEqual(panel.winfo_y() + panel.winfo_height(), self.root.winfo_y())
            self.strip._collapse_panel()
            self.root.update()

        # Warm both measured lifecycles before establishing a single closed-panel
        # baseline. Never move the baseline partway through the measured batches.
        self.warm_gui(refresh)
        self.warm_gui(panel_cycle)
        handles = self.handles()
        windows = gui_resources.windows()
        baseline = resources()
        for i in range(500):
            refresh()
            self.assertEqual(resources(), baseline)
            if i in (99, 499):
                self.assert_handles_unchanged(handles, windows, f"refresh {i + 1}")
        for i in range(200):
            panel_cycle()
            self.assertEqual(resources(), baseline)
            if i in (99, 199):
                self.assert_handles_unchanged(handles, windows, f"panel {i + 1}")

    def test_click_drag_persistence_status_and_mode_switch(self):
        dashboard.save_prefs(dashboard.DEFAULT_PREFS, self.home)
        rows = self.sample_rows()
        rows[0]["running"][0]["handle"] = self.conversation_fixture()[0]["handle"]
        self.strip.render(rows)
        self.root.deiconify()
        self.root.update()
        event = SimpleNamespace(x_root=self.root.winfo_x()+5, y_root=self.root.winfo_y()+5)
        self.pointer_event(self.strip._press, event)
        self.pointer_event(self.strip._release, event)
        self.assertIsNotNone(self.strip.panel)
        self.strip.panel.event_generate("<Escape>")
        self.root.update()
        self.assertIsNone(self.strip.panel)
        self.pointer_event(self.strip._press, event)
        moved = SimpleNamespace(x_root=event.x_root-50, y_root=event.y_root-100)
        old_y = self.root.winfo_y()
        self.pointer_event(self.strip._drag, moved)
        self.root.update_idletasks()
        self.pointer_event(self.strip._release, moved)
        self.assertEqual(self.root.winfo_y(), old_y - 100)
        self.assertEqual(dashboard.load_prefs(self.home)["compact_x"], self.root.winfo_x())
        self.assertEqual(dashboard.load_prefs(self.home)["compact_y"], self.root.winfo_y())
        self.assertFalse(dashboard.load_prefs(self.home)["docked"])
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
        dashboard.save_prefs({**dashboard.DEFAULT_PREFS, "docked": True}, self.home)
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
                if scale == 1:
                    self.assertGreaterEqual(abs(int(self.strip.compact_metrics.cget("size"))), 12)
                self.assertEqual(self.strip._compact_geometry[-1], height)
        finally:
            self.root.tk.call("tk", "scaling", original)

    def test_refresh_error_recovers_identical_content_without_periodic_repaint(self):
        dashboard.save_prefs(dashboard.DEFAULT_PREFS, self.home)
        rows = self.sample_rows()
        def labels():
            return [w.cget("text") for w in self.widgets(self.strip.frame) if isinstance(w, self.tk.Label)]
        with patch.object(dashboard, "snapshot", return_value=rows):
            self.strip.refresh(reschedule=False)
        expected = labels()
        for message in ("temporary read failure", "different read failure"):
            with patch.object(dashboard, "snapshot", side_effect=ValueError(message)):
                self.strip.refresh(reschedule=False)
                self.assertEqual(labels(), ["읽기 오류: " + message])
                error_label = self.strip.frame.winfo_children()[0]
                self.strip.refresh(reschedule=False)
                self.assertIs(self.strip.frame.winfo_children()[0], error_label)
        with patch.object(dashboard, "snapshot", return_value=rows):
            self.strip.refresh(reschedule=False)
            self.assertEqual(labels(), expected)
            widgets = self.widgets(self.strip.frame)
            self.strip.refresh(reschedule=False)
            self.assertEqual(self.widgets(self.strip.frame), widgets)

    def test_compact_request_error_updates_details_and_recovers_with_panel_open(self):
        dashboard.save_prefs(dashboard.DEFAULT_PREFS, self.home)
        request = self.request_state_fixture()

        def labels(parent):
            return [w.cget("text") for w in self.widgets(parent) if isinstance(w, self.tk.Label)]

        self.strip.refresh(reschedule=False)
        self.assertIn("대기", labels(self.strip.frame))
        request.write_text("{", encoding="utf-8")
        with patch.object(self.strip.tooltip, "bind", wraps=self.strip.tooltip.bind) as bind:
            self.strip.refresh(reschedule=False)
        self.assertIn("읽기 오류 1", labels(self.strip.frame))
        self.assertNotIn("대기", labels(self.strip.frame))
        self.assertTrue(any(str(request) in c.args[1] for c in bind.call_args_list))
        self.strip._toggle_panel()
        self.assertIn("읽기 오류 1", labels(self.strip.panel_frame))
        changed = request.with_name("2" * 32 + ".json")
        request.rename(changed)
        with patch.object(self.strip.tooltip, "bind", wraps=self.strip.tooltip.bind) as bind:
            self.strip.refresh(reschedule=False)
        self.assertTrue(any(str(changed) in c.args[1] for c in bind.call_args_list))
        self.assertFalse(any(str(request) in c.args[1] for c in bind.call_args_list))
        dashboard.atomic_json(changed, {"status": "completed"})
        self.strip.refresh(reschedule=False)
        for frame in (self.strip.frame, self.strip.panel_frame):
            self.assertIn("대기", labels(frame))
            self.assertFalse(any("읽기 오류" in text for text in labels(frame)))

    def test_keyboard_enter_space_toggle_panel_and_escape_closes(self):
        dashboard.save_prefs(dashboard.DEFAULT_PREFS, self.home)
        self.strip.render(self.sample_rows())
        self.root.deiconify()
        self.root.update()
        for key in ("<Return>", "<space>"):
            self.root.focus_force()
            self.root.event_generate(key)
            self.root.update()
            self.assertIsNotNone(self.strip.panel)
            self.strip.panel.event_generate(key)
            self.root.update()
            self.assertIsNone(self.strip.panel)
        self.root.focus_force()
        self.root.event_generate("<Return>")
        self.root.update()
        self.strip.panel.event_generate("<Escape>")
        self.root.update()
        self.assertIsNone(self.strip.panel)

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
