"""Win32 taskbar-button helpers are no-ops off Windows and tolerate API failures."""
import unittest
from unittest.mock import MagicMock, patch

from handback import dashboard


def fake_ctypes(style):
    user = MagicMock()
    user.GetAncestor.return_value = 77
    user.GetWindowLongW.return_value = style
    shell = MagicMock()
    windll = MagicMock(user32=user, shell32=shell)
    return user, shell, patch("ctypes.windll", windll, create=True)


class TaskbarHelperTests(unittest.TestCase):
    def widget(self, mapped=True):
        widget = MagicMock()
        widget.winfo_id.return_value = 5
        widget.winfo_ismapped.return_value = mapped
        return widget

    def test_non_windows_is_noop(self):
        with patch.object(dashboard.sys, "platform", "linux"):
            self.assertFalse(dashboard.set_app_user_model_id())
            self.assertFalse(dashboard.enable_taskbar_button(self.widget()))
            self.assertFalse(dashboard.restore_if_minimized(self.widget()))

    def test_app_user_model_id_set_and_failure_ignored(self):
        user, shell, patcher = fake_ctypes(0)
        with patch.object(dashboard.sys, "platform", "win32"), patcher:
            self.assertTrue(dashboard.set_app_user_model_id())
            shell.SetCurrentProcessExplicitAppUserModelID.assert_called_once()
            shell.SetCurrentProcessExplicitAppUserModelID.side_effect = OSError("no")
            self.assertFalse(dashboard.set_app_user_model_id())

    def test_enable_swaps_toolwindow_for_appwindow_and_reshows(self):
        user, _, patcher = fake_ctypes(0x80 | 0x8)
        with patch.object(dashboard.sys, "platform", "win32"), patcher:
            widget = self.widget()
            self.assertTrue(dashboard.enable_taskbar_button(widget))
        user.SetWindowLongW.assert_called_once_with(77, -20, 0x8 | 0x40000)
        widget.withdraw.assert_called_once_with()
        widget.deiconify.assert_called_once_with()

    def test_enable_is_idempotent(self):
        user, _, patcher = fake_ctypes(0x40000)
        with patch.object(dashboard.sys, "platform", "win32"), patcher:
            self.assertFalse(dashboard.enable_taskbar_button(self.widget()))
        user.SetWindowLongW.assert_not_called()

    def test_enable_unmapped_skips_reshow_and_failures_are_noops(self):
        user, _, patcher = fake_ctypes(0x80)
        with patch.object(dashboard.sys, "platform", "win32"), patcher:
            widget = self.widget(mapped=False)
            self.assertTrue(dashboard.enable_taskbar_button(widget))
            widget.withdraw.assert_not_called()
            user.GetAncestor.side_effect = OSError("boom")
            self.assertFalse(dashboard.enable_taskbar_button(self.widget()))

    def test_restore_only_when_iconic(self):
        user, _, patcher = fake_ctypes(0)
        with patch.object(dashboard.sys, "platform", "win32"), patcher:
            user.IsIconic.return_value = False
            self.assertFalse(dashboard.restore_if_minimized(self.widget()))
            user.IsIconic.return_value = True
            self.assertTrue(dashboard.restore_if_minimized(self.widget()))
            user.ShowWindow.assert_called_once_with(77, 4)


if __name__ == "__main__":
    unittest.main()
