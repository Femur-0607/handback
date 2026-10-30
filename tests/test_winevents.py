"""Foreground hook: z-order repair is pure over a fake user32; the thread starts and stops."""
import threading
import unittest
from unittest.mock import Mock, patch

from handback import winevents


class FakeUser:
    """Windows above `hwnd` in z-order, nearest first."""

    def __init__(self, above, visible=True):
        self.chain = dict(zip([7] + above, above))
        self.visible = visible
        self.SetWindowPos = Mock()
        self.FindWindowW = Mock(return_value=99)

    def IsWindowVisible(self, hwnd):
        return self.visible

    def GetWindow(self, hwnd, relation):
        assert relation == winevents.GW_HWNDPREV
        return self.chain.get(hwnd)


class RaiseAboveTaskbarTests(unittest.TestCase):
    def test_raises_only_when_the_taskbar_is_above(self):
        user = FakeUser([3, 99, 4])
        self.assertTrue(winevents.raise_above_taskbar(user, 7))
        user.SetWindowPos.assert_called_once_with(7, winevents.HWND_TOPMOST, 0, 0, 0, 0,
                                                  winevents.RAISE_FLAGS)
        self.assertTrue(winevents.RAISE_FLAGS & 0x4000)  # never waits on the Tk thread
        user = FakeUser([3, 4])
        self.assertFalse(winevents.raise_above_taskbar(user, 7))
        user.SetWindowPos.assert_not_called()

    def test_hidden_widget_and_given_taskbar(self):
        user = FakeUser([99], visible=False)
        self.assertFalse(winevents.raise_above_taskbar(user, 7))
        user.SetWindowPos.assert_not_called()
        user = FakeUser([5])
        self.assertTrue(winevents.raise_above_taskbar(user, 7, taskbar=5))  # secondary taskbar
        user.FindWindowW.assert_not_called()

    def test_event_flags_and_repairs_with_current_handles(self):
        watcher = winevents.ForegroundWatcher()
        watcher._api = FakeUser([99])
        watcher._event()
        self.assertTrue(watcher.changed.is_set())
        watcher._api.SetWindowPos.assert_not_called()  # no target yet
        watcher.target = 7
        watcher._event()
        watcher._api.SetWindowPos.assert_called_once()

    def test_full_screen_foreground_sinks_the_widget_instead_of_raising_it(self):
        watcher = winevents.ForegroundWatcher()
        watcher._api = FakeUser([99])
        watcher.target = 7
        with patch.object(winevents, "foreground_covers", return_value=True) as covers:
            watcher._event()
        covers.assert_called_once_with(watcher._api, 7)
        watcher._api.SetWindowPos.assert_called_once_with(7, winevents.HWND_BOTTOM, 0, 0, 0, 0,
                                                          winevents.RAISE_FLAGS)
        watcher._api = FakeUser([99], visible=False)  # already withdrawn by Tk
        with patch.object(winevents, "foreground_covers", return_value=True):
            watcher._event()
        watcher._api.SetWindowPos.assert_not_called()


PRIMARY, SECONDARY = (0, 0, 1920, 1080), (1920, 0, 3840, 1080)
GAME = dict(rect=PRIMARY, cls="UnityWndClass")


class FakeScreen:
    """Just enough user32 for foreground_covers: top-level windows in z-order, two monitors.

    Each window is a dict of rect, cls, pid, visible, iconic, cloaked and exstyle;
    the first one is the top of the z-order. The widget sits on the primary monitor.
    """

    def __init__(self, *windows, widget_monitor=1):
        self.windows = [dict(dict(cls="Chrome", pid=1234, visible=True, iconic=False,
                                  cloaked=False, exstyle=0), **w) for w in windows]
        self.widget_monitor = widget_monitor
        from ctypes import wintypes
        import ctypes

        class MonitorInfo(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]
        self.MonitorInfo = MonitorInfo

    def _window(self, hwnd):
        return self.windows[hwnd - 100]

    def GetTopWindow(self, parent):
        return 100 if self.windows else None

    def GetWindow(self, hwnd, relation):
        assert relation == winevents.GW_HWNDNEXT
        return hwnd + 1 if hwnd - 99 < len(self.windows) else None

    def IsWindowVisible(self, hwnd):
        return self._window(hwnd)["visible"]

    def IsIconic(self, hwnd):
        return self._window(hwnd)["iconic"]

    def DwmGetWindowAttribute(self, hwnd, attribute, value, size):
        assert attribute == winevents.DWMWA_CLOAKED
        value._obj.value = self._window(hwnd)["cloaked"]
        return 0

    def MonitorFromWindow(self, hwnd, flags):
        if hwnd < 100:
            return self.widget_monitor
        left, _, right, _ = self._window(hwnd)["rect"]
        return 1 if (left + right) / 2 < 1920 else 2  # the monitor it mostly lies on

    def MonitorFromPoint(self, point, flags):
        return 1 if point.x < 1920 else 2

    def GetWindowThreadProcessId(self, hwnd, pid):
        pid._obj.value = self._window(hwnd)["pid"]
        return 1

    def GetClassNameW(self, hwnd, buffer, size):
        buffer.value = self._window(hwnd)["cls"]
        return len(buffer.value)

    def GetWindowLongW(self, hwnd, index):
        assert index == winevents.GWL_EXSTYLE
        return self._window(hwnd)["exstyle"]

    def GetWindowRect(self, hwnd, rect):
        r = rect._obj
        r.left, r.top, r.right, r.bottom = self._window(hwnd)["rect"]
        return 1

    def GetMonitorInfoW(self, monitor, info):
        r = info._obj.rcMonitor
        r.left, r.top, r.right, r.bottom = PRIMARY if monitor == 1 else SECONDARY
        return 1


class ForegroundCoversTests(unittest.TestCase):
    def test_full_screen_on_the_widget_monitor_only(self):
        covers = winevents.foreground_covers
        self.assertTrue(covers(FakeScreen(GAME), 7))
        self.assertTrue(covers(FakeScreen(GAME), 7, point=(10, 1050)))
        self.assertFalse(covers(FakeScreen(GAME), 7, point=(2000, 1050)))  # moving to the other monitor
        self.assertFalse(covers(FakeScreen(GAME, widget_monitor=2), 7))
        self.assertFalse(covers(FakeScreen(dict(rect=(0, 0, 1920, 1032))), 7))  # maximized above the taskbar
        self.assertFalse(covers(FakeScreen(dict(GAME, cls="WorkerW")), 7))
        self.assertFalse(covers(FakeScreen(dict(GAME, pid=winevents.os.getpid())), 7))
        self.assertFalse(covers(FakeScreen(dict(GAME, visible=False)), 7))
        self.assertFalse(covers(FakeScreen(dict(GAME, iconic=True)), 7))
        self.assertFalse(covers(FakeScreen(GAME), None))
        self.assertFalse(covers(FakeScreen(), 7))
        self.assertFalse(covers(Mock(MonitorFromWindow=Mock(side_effect=OSError)), 7))

    def test_game_stays_on_top_when_focus_moves_to_another_monitor(self):
        browser = dict(rect=(1920, 0, 3840, 1032))  # focused, on the secondary monitor
        self.assertTrue(winevents.foreground_covers(FakeScreen(browser, GAME), 7))
        # Maximized there, its invisible frame reaches 8 px into the game's monitor.
        maximized = dict(rect=(1912, -8, 3848, 1040))
        self.assertTrue(winevents.foreground_covers(FakeScreen(maximized, GAME), 7))
        # Our own widget and shield sit above the game; they never count.
        own = dict(rect=(10, 1040, 300, 1080), pid=winevents.os.getpid(),
                   exstyle=winevents.WS_EX_TOPMOST)
        self.assertTrue(winevents.foreground_covers(FakeScreen(own, browser, GAME), 7))

    def test_windows_floating_over_the_game_do_not_reveal_the_widget(self):
        switcher = dict(rect=(400, 300, 1520, 780), exstyle=winevents.WS_EX_TOPMOST)  # Alt+Tab
        taskbar = dict(rect=(0, 1032, 1920, 1080), cls="Shell_TrayWnd",
                       exstyle=winevents.WS_EX_TOPMOST)
        overlay = dict(rect=PRIMARY, exstyle=winevents.WS_EX_TOPMOST | winevents.WS_EX_TRANSPARENT)
        suspended = dict(rect=(100, 100, 900, 700), cls="ApplicationFrameWindow", cloaked=True)
        empty = dict(rect=(0, 0, 0, 0))  # visible helper windows with no area
        self.assertTrue(winevents.foreground_covers(
            FakeScreen(switcher, taskbar, overlay, suspended, empty, GAME), 7))

    def test_an_ordinary_window_above_the_game_reveals_the_widget(self):
        notes = dict(rect=(200, 200, 1000, 800))  # Alt+Tab into a windowed app on the same monitor
        self.assertFalse(winevents.foreground_covers(FakeScreen(notes, GAME), 7))
        overlay = dict(rect=PRIMARY, exstyle=winevents.WS_EX_TRANSPARENT)  # click-through, full size
        self.assertFalse(winevents.foreground_covers(FakeScreen(overlay, notes, GAME), 7))


@unittest.skipUnless(winevents.sys.platform == "win32", "Windows hooks")
class ForegroundWatcherThreadTests(unittest.TestCase):
    def test_start_and_stop_are_idempotent(self):
        watcher = winevents.ForegroundWatcher()
        self.assertTrue(watcher.start())
        self.assertTrue(watcher.start())
        thread = watcher._thread
        watcher.stop()
        watcher.stop()
        self.assertFalse(thread.is_alive())
        self.assertFalse(watcher.active)
        self.assertNotIn("handback-foreground", [t.name for t in threading.enumerate()])


if __name__ == "__main__":
    unittest.main()
