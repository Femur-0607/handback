"""Tray icon: Win32 plumbing is mocked; the event queue drives Tk actions on the Tk thread."""
import ast
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from handback import dashboard, tray

LBUTTONUP, RBUTTONUP, CONTEXTMENU = tray.WM_LBUTTONUP, tray.WM_RBUTTONUP, tray.WM_CONTEXTMENU


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def make_tray(clock=None):
    icon = tray.Tray("icon.ico", "handback — 대기", clock=clock or FakeClock())
    icon._notify = MagicMock(return_value=True)
    icon.active = True
    return icon


class TrayMessageTests(unittest.TestCase):
    def test_left_and_right_clicks_become_queue_events_with_cursor(self):
        clock = FakeClock()
        icon = make_tray(clock)
        icon._cursor = lambda: (640, 480)
        self.assertTrue(icon._handle(tray.CALLBACK_MESSAGE, 0, LBUTTONUP))
        clock.now += 1
        icon._handle(tray.CALLBACK_MESSAGE, 0, RBUTTONUP)
        clock.now += 1
        icon._handle(tray.CALLBACK_MESSAGE, 0, CONTEXTMENU | (1 << 16))  # HIWORD is the icon id
        self.assertEqual(icon.events.get_nowait(), ("left", (640, 480)))
        self.assertEqual(icon.events.get_nowait(), ("right", (640, 480)))
        self.assertEqual(icon.events.get_nowait(), ("right", (640, 480)))

    def test_duplicate_notifications_for_one_click_are_debounced(self):
        clock = FakeClock()
        icon = make_tray(clock)
        icon._handle(tray.CALLBACK_MESSAGE, 0, LBUTTONUP)
        icon._handle(tray.CALLBACK_MESSAGE, 0, tray.NIN_SELECT)
        icon._handle(tray.CALLBACK_MESSAGE, 0, RBUTTONUP)
        icon._handle(tray.CALLBACK_MESSAGE, 0, CONTEXTMENU)
        self.assertEqual([kind for kind, _ in list(icon.events.queue)], ["left", "right"])
        clock.now += 1
        icon._handle(tray.CALLBACK_MESSAGE, 0, tray.NIN_SELECT)
        self.assertEqual(icon.events.qsize(), 3)

    def test_taskbar_created_readds_the_icon(self):
        icon = make_tray()
        icon._taskbar_created = 0xC123
        self.assertTrue(icon._handle(0xC123, 0, 0))
        icon._notify.assert_called_once_with(tray.NIM_ADD)

    def test_destroy_removes_the_icon_and_queues_quit(self):
        icon = make_tray()
        icon._handle(tray.WM_DESTROY, 0, 0)
        icon._notify.assert_called_once_with(tray.NIM_DELETE)
        self.assertEqual(icon.events.get_nowait()[0], "quit")

    def test_unknown_messages_fall_through(self):
        icon = make_tray()
        self.assertFalse(icon._handle(0x0113, 0, 0))
        icon._notify.assert_not_called()

    def test_tooltip_is_modified_only_when_it_changes_and_is_clipped(self):
        icon = make_tray()
        self.assertFalse(icon.set_tooltip("handback — 대기"))
        icon._notify.assert_not_called()
        self.assertTrue(icon.set_tooltip("handback — 결과 2개"))
        self.assertFalse(icon.set_tooltip("handback — 결과 2개"))
        icon._notify.assert_called_once_with(tray.NIM_MODIFY)
        icon.set_tooltip("x" * 500)
        self.assertEqual(len(icon.tooltip), tray.TIP_LIMIT)

    def test_non_windows_is_a_noop(self):
        with patch.object(tray.sys, "platform", "linux"):
            icon = tray.Tray("icon.ico")
            self.assertFalse(icon.start())
            self.assertFalse(icon.set_tooltip("handback — 결과 1개"))
            icon.stop()
        self.assertTrue(icon.events.empty())

    def test_tray_module_never_touches_tk(self):
        source = Path(tray.__file__).read_text(encoding="utf-8")
        imported = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        self.assertFalse({"tkinter", "dashboard"} & imported)
        self.assertNotIn(".after(", source)


class FakeStrip(dashboard.Strip):
    """A Strip whose Tk calls are mocks: only the tray-to-Tk wiring is under test."""

    def __init__(self):
        self.tray = tray.Tray("icon.ico", clock=FakeClock())
        self.tray._notify = MagicMock(return_value=True)
        self.tray.active = True
        self.root = MagicMock()
        self.tooltip = MagicMock()
        self.tk = SimpleNamespace(TclError=RuntimeError)
        self._tray_timer = None
        self._tray_text = "대기"
        self._user_hidden = False
        self._fullscreen_hidden = False
        self._context_menu = None
        self._collapse_panel = MagicMock()
        self._close_menu = MagicMock()
        self._menu = MagicMock()


class DashboardTrayTests(unittest.TestCase):
    def test_left_click_toggles_visibility(self):
        strip = FakeStrip()
        with patch.object(dashboard, "show_widget_without_activation") as show:
            strip.tray.events.put(("left", (1, 2)))
            strip._poll_tray()
            strip.root.withdraw.assert_called_once_with()
            self.assertTrue(strip._user_hidden)
            strip.tray.events.put(("left", (1, 2)))
            strip._poll_tray()
            show.assert_called_once_with(strip.root, True)
            strip.root.lift.assert_called_once_with()
            self.assertFalse(strip._user_hidden)

    def test_right_click_opens_the_root_menu_at_the_cursor_and_takes_foreground(self):
        strip = FakeStrip()
        strip._context_menu = SimpleNamespace(window="menu-window")
        with patch.object(dashboard, "bring_to_foreground") as foreground:
            strip.tray.events.put(("right", (640, 480)))
            strip._poll_tray()
        event, project = strip._menu.call_args.args
        self.assertEqual((event.x_root, event.y_root, project), (640, 480, None))
        foreground.assert_called_once_with("menu-window")

    def test_quit_event_stops_the_tray_and_destroys_root(self):
        strip = FakeStrip()
        tray_icon = strip.tray
        tray_icon.stop = MagicMock()
        tray_icon.events.put(("quit", None))
        strip._poll_tray()
        tray_icon.stop.assert_called_once_with()
        strip.root.destroy.assert_called_once_with()
        self.assertIsNone(strip.tray)
        strip.root.after.assert_not_called()  # polling ends with the tray

    def test_poll_updates_tooltip_from_state_and_reschedules(self):
        strip = FakeStrip()
        strip._tray_text = "결과 2개"
        strip._poll_tray()
        strip.tray._notify.assert_called_once_with(tray.NIM_MODIFY)
        strip._poll_tray()  # unchanged: no second NIM_MODIFY
        strip.tray._notify.assert_called_once_with(tray.NIM_MODIFY)
        strip.root.after.assert_called_with(dashboard.TRAY_POLL_MS, strip._poll_tray)

    def test_close_hides_with_tray_and_destroys_without(self):
        strip = FakeStrip()
        strip.close()
        strip.root.withdraw.assert_called_once_with()
        strip.root.destroy.assert_not_called()
        strip.tray = None
        strip.close()
        strip.root.destroy.assert_called_once_with()

    def test_shown_again_while_fullscreen_covered_waits_for_the_watcher(self):
        strip = FakeStrip()
        strip._user_hidden, strip._fullscreen_hidden = True, True
        with patch.object(dashboard, "show_widget_without_activation") as show:
            strip.show()
        show.assert_not_called()
        self.assertFalse(strip._user_hidden)

    def test_start_tray_is_inert_off_windows_and_when_creation_fails(self):
        strip = FakeStrip()
        strip.tray = None
        with patch.object(dashboard.sys, "platform", "linux"):
            self.assertFalse(strip._start_tray())
        with patch.object(dashboard.sys, "platform", "win32"), \
                patch.object(dashboard, "Tray") as factory:
            factory.return_value.start.return_value = False
            self.assertFalse(strip._start_tray())
            self.assertIsNone(strip.tray)
            factory.return_value.start.return_value = True
            self.assertTrue(strip._start_tray())
            strip.root.after.assert_called_once_with(dashboard.TRAY_POLL_MS, strip._poll_tray)


class StateTextTests(unittest.TestCase):
    def row(self, root, running=0, unread=0, unreadable=0):
        return {"root": root, "running": [{}] * running, "unread": [{}] * unread, "unreadable": unreadable}

    def test_summaries(self):
        self.assertEqual(dashboard.tray_state_text([]), "대기")
        self.assertEqual(dashboard.tray_state_text([self.row("a"), self.row("b", unread=2)]), "결과 2개")
        self.assertEqual(dashboard.tray_state_text([self.row("a", running=1, unread=3)]), "진행 1 · 결과 3개")
        self.assertEqual(dashboard.tray_state_text([self.row("a", unread=3)], hidden=["a"]), "대기")
        self.assertEqual(dashboard.tray_state_text([self.row("a", unreadable=1)]), "읽기 오류")


class AppUserModelIdTests(unittest.TestCase):
    def test_set_and_failures_are_ignored(self):
        shell = MagicMock()
        windll = MagicMock(shell32=shell)
        with patch.object(dashboard.sys, "platform", "linux"):
            self.assertFalse(dashboard.set_app_user_model_id())
        with patch.object(dashboard.sys, "platform", "win32"), patch("ctypes.windll", windll, create=True):
            self.assertTrue(dashboard.set_app_user_model_id())
            shell.SetCurrentProcessExplicitAppUserModelID.side_effect = OSError("no")
            self.assertFalse(dashboard.set_app_user_model_id())

    def test_taskbar_button_code_is_gone(self):
        self.assertFalse(hasattr(dashboard, "enable_taskbar_button"))
        self.assertFalse(hasattr(dashboard, "restore_if_minimized"))


if __name__ == "__main__":
    unittest.main()
