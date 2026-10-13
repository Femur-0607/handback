"""Foreground hook: z-order repair is pure over a fake user32; the thread starts and stops."""
import threading
import unittest
from unittest.mock import Mock

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
