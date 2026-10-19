"""✉ click-to-clear, idle auto-hide, attention effects, and the options window."""
import copy
import unittest
from unittest.mock import patch

from handback import dashboard
from tests import test_dashboard_compact as compact

LINKED = "codex:22222222-2222-2222-2222-222222222222"


class AttentionPureTests(unittest.TestCase):
    def test_blend_clamps_and_mixes_channels(self):
        self.assertEqual(dashboard.blend("#000000", "#ffffff", 0), "#000000")
        self.assertEqual(dashboard.blend("#000000", "#ffffff", 1), "#ffffff")
        self.assertEqual(dashboard.blend("#000000", "#ffffff", 2), "#ffffff")
        self.assertEqual(dashboard.blend("#102030", "#305070", .5), "#203850")

    def test_idle_ignores_hidden_projects(self):
        rows = [{"root": "a", "running": [], "unread": []},
                {"root": "b", "running": [{}], "unread": []}]
        prefs = dict(dashboard.DEFAULT_PREFS)
        self.assertFalse(dashboard.is_idle(rows, prefs))
        self.assertTrue(dashboard.is_idle(rows, {**prefs, "hidden": ["b"]}))
        rows[0]["unreadable"] = 1
        self.assertFalse(dashboard.is_idle(rows, {**prefs, "hidden": ["b"]}))

    def test_unread_tip_names_destination_and_clearing(self):
        linked = {"unread": [{"sender": LINKED, "from": "w"}]}
        other = {"unread": [{"sender": "claude:lead", "from": "w"}]}
        self.assertEqual(dashboard.unread_tip(linked, True), "Codex 대화 열기 · 클릭하면 확인 처리")
        self.assertEqual(dashboard.unread_tip(other, False), "결과 보기")


class AttentionGuiTests(compact.CompactGuiTests):
    def unread_fixture(self, sender=LINKED, **prefs):
        dashboard.save_prefs({**dashboard.DEFAULT_PREFS, "compact_x": 200, "compact_y": 300, **prefs},
                             self.home)
        rows = self.sample_rows()
        rows[0]["unread"][0].update(sender=sender, id="result-1")
        self.strip.render(rows)
        self.root.deiconify()
        self.root.update()
        status = self.strip.frame.winfo_children()[0].winfo_children()[2]
        self.cursor[:] = [status.winfo_rootx() + 2, status.winfo_rooty() + 2]
        return rows, status

    def click(self, widget, rows):
        cleared = copy.deepcopy(rows)
        cleared[0]["unread"] = []
        with patch.object(dashboard, "acknowledge_ids", return_value=1) as ack, \
                patch.object(dashboard, "snapshot", return_value=cleared):
            widget.event_generate("<Button-1>", x=2, y=2)
            widget.event_generate("<ButtonRelease-1>", x=2, y=2)
        return ack

    def test_unread_click_clears_results_and_opens_conversation(self):
        rows, status = self.unread_fixture()
        with patch.object(self.strip, "opener") as opener:
            ack = self.click(status, rows)
        ack.assert_called_once_with("0", ["result-1"], self.home)
        opener.assert_called_once_with(dashboard.conversation_link(LINKED))
        # The refresh after clearing removed the ✉ count from the widget.
        self.assertEqual(self.strip.frame.winfo_children()[0].winfo_children()[2].cget("text"), "")
        self.assertIsNone(self.strip.panel)

    def test_unread_click_without_ack_option_only_opens(self):
        rows, status = self.unread_fixture(ack_on_click=False)
        with patch.object(self.strip, "opener") as opener:
            ack = self.click(status, rows)
        ack.assert_not_called()
        opener.assert_called_once()

    def test_unread_click_without_conversation_link_shows_cleared_results(self):
        rows, status = self.unread_fixture(sender="claude:lead")
        self.assertEqual(status.cget("cursor"), "hand2")
        with patch.object(self.strip, "_open") as viewer, patch.object(self.strip, "opener") as opener:
            ack = self.click(status, rows)
        ack.assert_called_once()
        opener.assert_not_called()
        viewer.assert_called_once_with(rows[0], 0, acknowledged=True)

    def test_highlight_option_draws_badge_and_tints_floating_border(self):
        rows, status = self.unread_fixture()
        palette_yellow = self.strip.YELLOW
        self.assertEqual(status.cget("bg"), palette_yellow)
        self.assertNotEqual(self.root.cget("bg"), self.strip.BORDER)
        _, status = self.unread_fixture(highlight_unread=False)
        self.assertEqual(status.cget("bg"), self.strip.BG)
        self.assertEqual(status.cget("fg"), palette_yellow)
        self.assertEqual(self.root.cget("bg"), self.strip.BORDER)

    def test_new_result_pulses_then_settles(self):
        rows, status = self.unread_fixture()
        self.assertNotIn("0", self.strip._pulse_until)  # first render is not an arrival
        more = copy.deepcopy(rows)
        more[0]["unread"].append({**rows[0]["unread"][0], "id": "result-2"})
        with patch.object(dashboard.time, "monotonic", return_value=100.0):
            self.strip.render(more)
        self.assertEqual(self.strip._pulse_until["0"], 100.0 + dashboard.PULSE_SECONDS)
        status = self.strip.frame.winfo_children()[0].winfo_children()[2]
        with patch.object(dashboard.time, "monotonic", return_value=100.36):  # mid-blink
            self.strip._animate()
        self.assertNotEqual(status.cget("bg"), self.strip.YELLOW)
        with patch.object(dashboard.time, "monotonic", return_value=200.0):  # pulse over
            self.strip._animate()
        self.assertEqual(status.cget("bg"), self.strip.YELLOW)

    def test_animation_off_keeps_static_colours_and_no_timer(self):
        dashboard.save_prefs({**dashboard.DEFAULT_PREFS, "animate_running": False,
                              "animate_unread": False}, self.home)
        self.strip.render(self.sample_rows())
        self.assertIsNone(self.strip._anim_timer)
        dashboard.save_prefs(dashboard.DEFAULT_PREFS, self.home)
        self.strip.render(self.sample_rows())
        self.assertIsNotNone(self.strip._anim_timer)  # the running row breathes

    def test_auto_hide_when_idle_and_tray_show_holds_until_next_activity(self):
        dashboard.save_prefs({**dashboard.DEFAULT_PREFS, "auto_hide": True}, self.home)
        busy = self.sample_rows()
        idle = copy.deepcopy(busy)
        for row in idle:
            row["running"], row["unread"] = [], []
        self.root.deiconify()
        self.strip.render(idle)
        self.root.update()
        self.assertTrue(self.strip._idle_hidden)
        self.assertFalse(self.root.winfo_ismapped())
        with patch.object(dashboard, "show_widget_without_activation",
                          side_effect=lambda widget, visible: widget.deiconify() if visible
                          else widget.withdraw()):
            self.strip._toggle_visible()  # tray click while idle-hidden shows it
            self.root.update()
            self.assertTrue(self.root.winfo_ismapped())
            self.strip.render(idle)
            self.root.update()
            self.assertTrue(self.root.winfo_ismapped())  # stays until something happens
            self.strip.render(busy)
            self.strip.render(idle)
            self.root.update()
            self.assertFalse(self.root.winfo_ismapped())
        dashboard.save_prefs(dashboard.DEFAULT_PREFS, self.home)
        with patch.object(dashboard, "show_widget_without_activation",
                          side_effect=lambda widget, visible: widget.deiconify()):
            self.strip.render(idle)
        self.assertFalse(self.strip._idle_hidden)

    def test_options_window_applies_each_change_at_once(self):
        self.strip._options()
        window = self.strip._options_window
        self.assertIsNotNone(window)
        boxes = {widget.cget("text"): widget for widget in self.widgets(window)
                 if isinstance(widget, self.tk.Checkbutton)}
        boxes["할 일이 없으면 숨기기"].invoke()
        self.assertTrue(dashboard.load_prefs(self.home)["auto_hide"])
        boxes["✉ 클릭 시 확인 처리"].invoke()
        self.assertFalse(dashboard.load_prefs(self.home)["ack_on_click"])
        radios = [widget for widget in self.widgets(window)
                  if isinstance(widget, self.tk.Radiobutton) and widget.cget("text") == "2줄"]
        radios[0].invoke()
        self.assertEqual(dashboard.load_prefs(self.home)["max_rows"], 2)
        self.strip._options()  # a second open reuses the window
        self.assertIs(self.strip._options_window, window)
        window.destroy()


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite(loader.loadTestsFromTestCase(AttentionPureTests))
    names = sorted(name for name, method in AttentionGuiTests.__dict__.items()
                   if name.startswith("test_") and callable(method))
    suite.addTests(AttentionGuiTests(name) for name in names)
    return suite


if __name__ == "__main__":
    unittest.main()
