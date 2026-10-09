"""Long fixed and popup panels stay reachable without moving the compact strip."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from handback import dashboard
from tests import test_dashboard_gui as gui


class PanelScrollTests(unittest.TestCase):
    destroy_root = gui.DashboardMenuTests.destroy_root
    widgets = gui.DashboardMenuTests.widgets

    def setUp(self):
        gui.DashboardMenuTests.setUp(self)
        self.area = (0, 0, 900, 360)
        for name, value in (("_monitor_area", self.area), ("_virtual_area", self.area),
                            ("taskbar_info", None), ("taskbar_colors", []),
                            ("widget_fullscreen_covered", False)):
            replacement = patch.object(dashboard, name, return_value=value)
            replacement.start()
            self.addCleanup(replacement.stop)
        self.rows = [{"root": f"project-{index}", "name": f"Project {index:02}",
                      "lead": "codex:lead", "workers": ["claude"], "running": [],
                      "unread": [], "last_activity": 1800000000 - index}
                     for index in range(50)]

    def show(self, mode="panel"):
        dashboard.save_prefs({**dashboard.DEFAULT_PREFS, "mode": mode, "max_rows": 0,
                              "compact_x": 200, "compact_y": 280}, self.home)
        self.strip.render(self.rows)
        self.root.deiconify()
        self.root.update()
        if mode == "taskbar":
            self.strip._toggle_panel()
            self.root.update()
            return self.strip._panel_viewport, self.strip.panel
        return self.strip._fixed_viewport, self.root

    def assert_last_row_visible(self, viewport, window):
        last = viewport.content.winfo_children()[-1]
        viewport.scroll("moveto", 1)
        self.root.update()
        self.assertAlmostEqual(viewport.canvas.yview()[1], 1.0, places=3)
        self.assertGreaterEqual(last.winfo_rooty(), window.winfo_rooty())
        self.assertLessEqual(last.winfo_rooty() + last.winfo_height(),
                             window.winfo_rooty() + window.winfo_height())

    def test_fixed_panel_is_bounded_and_wheel_reaches_last_row(self):
        viewport, window = self.show()
        self.assertLessEqual(window.winfo_height(), self.area[3] - self.area[1])
        self.assertGreaterEqual(window.winfo_y(), self.area[1])
        self.assertLessEqual(window.winfo_y() + window.winfo_height(), self.area[3])
        self.assertTrue(viewport.scrollbar.winfo_ismapped())
        self.assertEqual(viewport.wheel(SimpleNamespace(delta=-120, num=None)), "break")
        self.root.update()
        self.assertGreater(viewport.offset, 0)
        self.assert_last_row_visible(viewport, window)

    def test_popup_panel_scrolls_without_moving_compact_widget(self):
        viewport, window = self.show("taskbar")
        compact = (self.root.winfo_x(), self.root.winfo_y(),
                   self.root.winfo_width(), self.root.winfo_height())
        self.assertLessEqual(window.winfo_height(), self.area[3] - self.area[1])
        self.assertTrue(viewport.scrollbar.winfo_ismapped())
        self.assert_last_row_visible(viewport, window)
        self.assertEqual((self.root.winfo_x(), self.root.winfo_y(),
                          self.root.winfo_width(), self.root.winfo_height()), compact)
        self.strip._collapse_panel()
        self.assertIsNone(self.strip._panel_viewport)

    def test_refresh_keeps_pixel_offset_with_unchanged_and_rebuilt_content(self):
        for mode in ("panel", "taskbar"):
            with self.subTest(mode=mode):
                self.strip._collapse_panel()
                viewport, _ = self.show(mode)
                viewport.scroll("moveto", 0.45)
                self.root.update()
                offset = viewport.offset
                self.strip.render(self.rows)
                self.root.update()
                self.assertAlmostEqual(viewport.canvas.canvasy(0), offset, delta=1)
                changed = [{**row} for row in self.rows]
                changed[0]["name"] += " changed"
                self.strip.render(changed)
                self.root.update()
                self.assertAlmostEqual(viewport.canvas.canvasy(0), offset, delta=1)

    def test_shorter_content_resets_offset_and_hides_scrollbar(self):
        viewport, _ = self.show()
        viewport.scroll("moveto", 1)
        self.strip.render(self.rows[:2])
        self.root.update()
        self.assertEqual(viewport.offset, 0)
        self.assertEqual(viewport.canvas.canvasy(0), 0)
        self.assertFalse(viewport.scrollbar.winfo_ismapped())
        self.assertLess(self.root.winfo_height(), self.area[3])

    def test_mode_switch_preserves_compact_frame_and_inactive_wheel_does_nothing(self):
        viewport, _ = self.show()
        root_frame = self.strip._root_frame
        self.show("taskbar")
        self.strip._collapse_panel()
        self.root.update()
        self.assertIs(self.strip.frame, root_frame)
        self.assertTrue(root_frame.winfo_ismapped())
        self.assertFalse(viewport.container.winfo_ismapped())
        self.assertIsNone(viewport.wheel(SimpleNamespace(delta=-120, num=None)))
        self.assertEqual(viewport.offset, 0)

    def test_scrollbar_gestures_do_not_enter_top_level_drag_bindings(self):
        viewport, window = self.show()
        self.assertNotIn(str(window), viewport.scrollbar.bindtags())
        viewport.scrollbar.event_generate("<ButtonPress-1>", x=5, y=50)
        self.root.update()
        self.assertIsNone(self.strip._start)
        viewport.scrollbar.event_generate("<ButtonRelease-1>", x=5, y=50)


if __name__ == "__main__":
    unittest.main()
