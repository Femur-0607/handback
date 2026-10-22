"""Appearance options: opacity, colour theme, and the floating widget's grab bar."""
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from handback import dashboard
from tests import test_dashboard_compact as compact


class AppearancePureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="dashboard-appearance-", dir=Path(__file__).parent)
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)

    def test_appearance_prefs_default_round_trip_and_reject_bad_values(self):
        prefs = dashboard.load_prefs(self.home)
        self.assertEqual((prefs["opacity"], prefs["theme"], prefs["drag_handle"]), (80, "dark", True))
        # A saved choice wins over the default, including the old 100% default.
        dashboard.save_prefs({"opacity": 100}, self.home)
        self.assertEqual(dashboard.load_prefs(self.home)["opacity"], 100)
        dashboard.save_prefs({"opacity": 70, "theme": "system", "drag_handle": False}, self.home)
        prefs = dashboard.load_prefs(self.home)
        self.assertEqual((prefs["opacity"], prefs["theme"], prefs["drag_handle"]), (70, "system", False))
        for opacity, theme, handle in ((55, "blue", "yes"), ("80", None, 0), (True, "Dark", None)):
            dashboard.save_prefs({"opacity": opacity, "theme": theme, "drag_handle": handle}, self.home)
            self.assertEqual(dashboard.load_prefs(self.home), dashboard.DEFAULT_PREFS)

    def test_resolve_theme_follows_windows_app_mode_only_for_system(self):
        for light, expected in ((True, "light"), (False, "dark")):
            with patch.object(dashboard, "system_prefers_light", return_value=light) as probe:
                self.assertEqual(dashboard.resolve_theme("system"), expected)
                self.assertEqual(dashboard.resolve_theme("dark"), "dark")
                self.assertEqual(dashboard.resolve_theme("light"), "light")
                self.assertEqual(dashboard.resolve_theme("unknown"), "dark")
            self.assertEqual(probe.call_count, 1)

    @unittest.skipUnless(sys.platform == "win32", "Windows registry")
    def test_system_theme_reads_apps_use_light_theme_and_defaults_to_dark(self):
        import winreg
        opened = patch.object(winreg, "OpenKey")
        opened.start()
        self.addCleanup(opened.stop)
        for value, expected in ((1, True), (0, False)):
            with patch.object(winreg, "QueryValueEx", return_value=(value, winreg.REG_DWORD)):
                self.assertIs(dashboard.system_prefers_light(), expected)
        with patch.object(winreg, "QueryValueEx", side_effect=FileNotFoundError):
            self.assertFalse(dashboard.system_prefers_light())

    def test_themes_are_complete_and_each_colour_is_unique_for_recolouring(self):
        for name, theme in dashboard.THEMES.items():
            with self.subTest(theme=name):
                self.assertEqual(set(theme), {*dashboard.THEME_KEYS, "agents"})
                colours = [theme[key] for key in dashboard.THEME_KEYS] + list(theme["agents"].values())
                self.assertEqual(len(colours), len(set(colours)))
                self.assertTrue(all(colour == colour.lower() for colour in colours))
        # The docked widget keeps using the same light/dark text colours as the themes.
        self.assertEqual(dashboard.compact_palette([(240, 240, 240)])["fg"], dashboard.THEMES["light"]["fg"])
        self.assertEqual(dashboard.compact_palette([(20, 20, 20)])["dim"], dashboard.THEMES["dark"]["dim"])


class AppearanceGuiTests(compact.CompactGuiTests):
    def render(self, rows=None, **prefs):
        dashboard.save_prefs({**dashboard.DEFAULT_PREFS, "compact_x": 200, "compact_y": 300, **prefs},
                             self.home)
        self.strip.render(self.sample_rows() if rows is None else rows)
        self.root.deiconify()
        self.root.update()

    def alpha_calls(self, **prefs):
        # Test windows are forced invisible, so record the requested opacity instead.
        with patch.object(self.root, "attributes", wraps=self.root.attributes) as attributes:
            self.render(**prefs)
        return [call.args[1] for call in attributes.call_args_list
                if call.args[:1] == ("-alpha",) and len(call.args) > 1]

    def test_floating_widget_has_grab_bar_first_and_a_click_on_it_opens_nothing(self):
        self.render()
        handle = self.strip._handle
        self.assertIsNotNone(handle)
        self.assertEqual(handle.cget("cursor"), "fleur")
        self.assertIs(self.strip.frame.pack_slaves()[0], handle)
        # Row indices stay stable: the first child is still the first project row.
        self.assertIsNot(self.strip.frame.winfo_children()[0], handle)
        grip = handle.winfo_children()[0]
        for target in (handle, grip):
            with self.subTest(target=target), patch.object(self.strip, "_toggle_panel") as toggle:
                self.cursor[:] = [target.winfo_rootx() + 1, target.winfo_rooty() + 1]
                target.event_generate("<Button-1>", x=1, y=1)
                target.event_generate("<ButtonRelease-1>", x=1, y=1)
                toggle.assert_not_called()
        # The rest of the widget still opens the panel on a click.
        name = self.strip.frame.winfo_children()[0].winfo_children()[0]
        with patch.object(self.strip, "_toggle_panel") as toggle:
            self.cursor[:] = [name.winfo_rootx() + 1, name.winfo_rooty() + 1]
            name.event_generate("<Button-1>", x=1, y=1)
            name.event_generate("<ButtonRelease-1>", x=1, y=1)
            toggle.assert_called_once()

    def test_grab_bar_drags_the_widget_and_saves_its_place(self):
        self.render()
        handle = self.strip._handle
        start = [handle.winfo_rootx() + 4, handle.winfo_rooty() + 2]
        self.cursor[:] = start
        handle.event_generate("<Button-1>", x=4, y=2)
        self.cursor[:] = [start[0] + 80, start[1] + 40]
        handle.event_generate("<B1-Motion>", x=4, y=2)
        self.root.update_idletasks()
        handle.event_generate("<ButtonRelease-1>", x=4, y=2)
        self.root.update_idletasks()
        self.strip._stop_settle()
        self.assertEqual((self.root.winfo_x(), self.root.winfo_y()), (280, 340))
        prefs = dashboard.load_prefs(self.home)
        self.assertEqual((prefs["compact_x"], prefs["compact_y"]), (280, 340))
        self.assertIsNone(self.strip.panel)

    def test_grab_bar_follows_option_floating_and_docked(self):
        self.render(drag_handle=False)
        self.assertIsNone(self.strip._handle)
        self.render()
        floating = self.strip._handle.winfo_reqheight()
        with patch.object(dashboard, "taskbar_colors", return_value=[(32, 32, 32)]):
            self.render(docked=True, drag_handle=False)
            self.assertIsNone(self.strip._handle)
            free = self.strip.compact_metrics.cget("size")
            self.render(docked=True)
        self.assertTrue(self.strip.docked)
        handle = self.strip._handle
        self.assertIsNotNone(handle)
        self.assertIs(self.strip.frame.pack_slaves()[0], handle)
        self.assertEqual(handle.cget("bg"), self.strip._compact_palette["bg"])
        self.assertLess(handle.winfo_reqheight(), floating)
        # The bar and both lines still fit the taskbar height.
        self.assertGreaterEqual(abs(free), abs(self.strip.compact_metrics.cget("size")))
        self.root.update_idletasks()
        self.assertLessEqual(self.strip.frame.winfo_reqheight(), self.root.winfo_height())

    def test_docked_grab_bar_click_opens_nothing_and_pulling_it_undocks(self):
        with patch.object(dashboard, "taskbar_colors", return_value=[(32, 32, 32)]):
            self.render(docked=True)
            handle = self.strip._handle
            start = [handle.winfo_rootx() + 4, handle.winfo_rooty() + 2]
            with patch.object(self.strip, "_toggle_panel") as toggle:
                self.cursor[:] = start
                handle.event_generate("<Button-1>", x=4, y=2)
                handle.event_generate("<ButtonRelease-1>", x=4, y=2)
                toggle.assert_not_called()
            self.cursor[:] = start
            handle.event_generate("<Button-1>", x=4, y=2)
            self.cursor[:] = [start[0] - 100, start[1] - 300]
            handle.event_generate("<B1-Motion>", x=4, y=2)
            self.root.update_idletasks()
            self.strip._flush_drag()
            self.root.event_generate("<ButtonRelease-1>", x=4, y=2)
            self.root.update_idletasks()
            self.strip._stop_settle()
        self.assertFalse(self.strip.docked)
        self.assertFalse(dashboard.load_prefs(self.home)["docked"])

    def test_opacity_applies_to_floating_widget_panels_but_not_docked_widget(self):
        self.assertEqual(self.alpha_calls(opacity=80), [0.8])
        self.assertEqual(self.alpha_calls(opacity=80), [])  # unchanged view: no repeated call
        with patch.object(dashboard, "taskbar_colors", return_value=[(32, 32, 32)]):
            self.assertEqual(self.alpha_calls(opacity=80, docked=True), [1.0])
        self.assertIn(0.6, self.alpha_calls(opacity=60, mode="panel"))
        self.render(opacity=70)
        calls, current = [], self.tk.Wm.attributes

        def record(window, *args):
            calls.append((window, args))
            return current(window, *args)
        with patch.object(self.tk.Wm, "attributes", record):
            self.strip._toggle_panel()
        self.assertIn((self.strip.panel, ("-alpha", 0.7)), calls)

    def test_light_theme_colours_widget_menu_and_options_window(self):
        light = dashboard.THEMES["light"]
        self.render(theme="light")
        self.assertEqual((self.strip.BG, self.strip.FG, self.strip.COLORS),
                         (light["bg"], light["fg"], dashboard.AGENT_COLORS_LIGHT))
        self.assertEqual(self.strip.frame.cget("bg"), light["bg"])
        # The sample has waiting results: the light border is tinted with the light accent.
        self.assertEqual(self.root.cget("bg"), dashboard.blend(light["border"], light["yellow"], 0.7))
        dot = self.strip.frame.winfo_children()[0].winfo_children()[-1]
        self.assertEqual(dot.cget("fg"), dashboard.AGENT_COLORS_LIGHT["claude"])
        self.strip._menu(SimpleNamespace(x_root=0, y_root=0), None)
        menu = self.strip._context_menu
        self.assertEqual(menu.canvas.cget("bg"), light["surface"])
        self.strip._close_menu()
        self.strip._options()
        window = self.strip._options_window
        self.assertEqual(window.cget("bg"), light["bg"])
        window.destroy()
        # The docked widget keeps following the taskbar colour, not the theme.
        with patch.object(dashboard, "taskbar_colors", return_value=[(32, 32, 32)]):
            self.render(theme="light", docked=True)
        self.assertEqual(self.strip.frame.cget("bg"), "#202020")

    def test_theme_change_recolours_open_windows_at_once(self):
        dark, light = dashboard.THEMES["dark"], dashboard.THEMES["light"]
        self.render()
        self.strip._options()
        window = self.strip._options_window
        radio = next(widget for widget in self.widgets(window)
                     if isinstance(widget, self.tk.Radiobutton) and widget.cget("text") == "라이트")
        heading = next(widget for widget in self.widgets(window)
                       if isinstance(widget, self.tk.Label) and widget.cget("text") == "모양")
        self.assertEqual((window.cget("bg"), heading.cget("fg")), (dark["bg"], dark["yellow"]))
        radio.invoke()
        self.assertEqual(dashboard.load_prefs(self.home)["theme"], "light")
        self.assertEqual(self.strip.theme, "light")
        self.assertEqual((window.cget("bg"), heading.cget("fg")), (light["bg"], light["yellow"]))
        self.assertEqual(radio.cget("selectcolor"), light["surface"])
        self.assertEqual(self.strip.frame.cget("bg"), light["bg"])
        with patch.object(dashboard, "system_prefers_light", return_value=False):
            next(widget for widget in self.widgets(window)
                 if isinstance(widget, self.tk.Radiobutton) and widget.cget("text") == "시스템").invoke()
        self.assertEqual(self.strip.theme, "dark")
        self.assertEqual(window.cget("bg"), dark["bg"])
        window.destroy()

    def test_options_window_saves_each_appearance_choice(self):
        self.render()
        self.strip._options()
        window = self.strip._options_window
        self.assertEqual({key: window.variables[key].get() for key in ("theme", "opacity", "drag_handle")},
                         {"theme": "dark", "opacity": 80, "drag_handle": True})
        next(widget for widget in self.widgets(window)
             if isinstance(widget, self.tk.Radiobutton) and widget.cget("text") == "70%").invoke()
        next(widget for widget in self.widgets(window)
             if isinstance(widget, self.tk.Checkbutton) and widget.cget("text") == "드래그 손잡이 표시").invoke()
        window.destroy()
        prefs = dashboard.load_prefs(self.home)
        self.assertEqual((prefs["opacity"], prefs["drag_handle"]), (70, False))
        self.assertIsNone(self.strip._handle)
        self.assertEqual(self.strip._opacity, 70)

    def test_text_tags_follow_the_theme_too(self):
        text = self.tk.Text(self.root)
        text.tag_configure("error", foreground=dashboard.THEMES["dark"]["red"])
        dashboard.recolor(text, {dashboard.THEMES["dark"]["red"]: dashboard.THEMES["light"]["red"]})
        self.assertEqual(text.tag_cget("error", "foreground"), dashboard.THEMES["light"]["red"])


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite(loader.loadTestsFromTestCase(AppearancePureTests))
    names = sorted(name for name, method in AppearanceGuiTests.__dict__.items()
                   if name.startswith("test_") and callable(method))
    suite.addTests(AppearanceGuiTests(name) for name in names)
    return suite


if __name__ == "__main__":
    unittest.main()
