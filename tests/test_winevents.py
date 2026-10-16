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


class FakeScreen:
    """Just enough user32 for foreground_covers: one foreground window, two monitors."""

    def __init__(self, rect, monitor=(0, 0, 1920, 1080), same_monitor=True, cls="Chrome",
                 pid=1234, visible=True, iconic=False):
        self.rect, self.monitor, self.same, self.cls = rect, monitor, same_monitor, cls
        self.pid, self.visible, self.iconic = pid, visible, iconic
        from ctypes import wintypes
        import ctypes

        class MonitorInfo(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]
        self.MonitorInfo = MonitorInfo

    def GetForegroundWindow(self):
        return 50

    def IsWindowVisible(self, hwnd):
        return self.visible

    def IsIconic(self, hwnd):
        return self.iconic

    def MonitorFromWindow(self, hwnd, flags):
        return 1 if hwnd == 50 or self.same else 2

    def MonitorFromPoint(self, point, flags):
        return 1 if self.same else 2

    def GetWindowThreadProcessId(self, hwnd, pid):
        pid._obj.value = self.pid
        return 1

    def GetClassNameW(self, hwnd, buffer, size):
        buffer.value = self.cls
        return len(self.cls)

    def GetWindowRect(self, hwnd, rect):
        r = rect._obj
        r.left, r.top, r.right, r.bottom = self.rect
        return 1

    def GetMonitorInfoW(self, monitor, info):
        r = info._obj.rcMonitor
        r.left, r.top, r.right, r.bottom = self.monitor
        return 1


class ForegroundCoversTests(unittest.TestCase):
    def test_full_screen_on_the_widget_monitor_only(self):
        covers = winevents.foreground_covers
        self.assertTrue(covers(FakeScreen((0, 0, 1920, 1080)), 7))
        self.assertTrue(covers(FakeScreen((0, 0, 1920, 1080)), 7, point=(10, 1050)))
        self.assertFalse(covers(FakeScreen((0, 0, 1920, 1032)), 7))  # maximized above the taskbar
        self.assertFalse(covers(FakeScreen((0, 0, 1920, 1080), same_monitor=False), 7))
        self.assertFalse(covers(FakeScreen((0, 0, 1920, 1080), cls="WorkerW"), 7))
        self.assertFalse(covers(FakeScreen((0, 0, 1920, 1080), pid=winevents.os.getpid()), 7))
        self.assertFalse(covers(FakeScreen((0, 0, 1920, 1080), visible=False), 7))
        self.assertFalse(covers(FakeScreen((0, 0, 1920, 1080), iconic=True), 7))
        self.assertFalse(covers(FakeScreen((0, 0, 1920, 1080)), None))
        self.assertFalse(covers(Mock(GetForegroundWindow=Mock(side_effect=OSError)), 7))


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
