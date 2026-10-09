"""Native region ownership and floating widget corner lifecycle."""
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from handback import dashboard


class CornerRegionTests(unittest.TestCase):
    def setUp(self):
        import ctypes
        self.widget = Mock()
        self.widget.winfo_id.return_value = 11
        self.widget.winfo_ismapped.return_value = True
        self.user, self.gdi = Mock(), Mock()
        self.user.GetAncestor.return_value = 12
        self.user.SetWindowRgn.return_value = 1
        self.gdi.CreateRoundRectRgn.return_value = 31
        self.platform = patch.object(dashboard.sys, "platform", "win32")
        self.native = patch.object(ctypes, "windll",
                                   SimpleNamespace(user32=self.user, gdi32=self.gdi), create=True)
        self.platform.start()
        self.native.start()
        self.addCleanup(self.platform.stop)
        self.addCleanup(self.native.stop)

    def test_success_transfers_region_to_selected_native_window(self):
        for wrapper, hwnd in ((True, 12), (False, 11)):
            with self.subTest(wrapper=wrapper):
                self.user.reset_mock()
                self.gdi.reset_mock()
                self.assertTrue(dashboard._set_window_corners(self.widget, 300, 44, 10,
                                                              wrapper=wrapper))
                if wrapper:
                    self.user.GetAncestor.assert_called_once_with(11, 2)
                else:
                    self.user.GetAncestor.assert_not_called()
                self.gdi.CreateRoundRectRgn.assert_called_once_with(0, 0, 300, 44, 20, 20)
                self.user.SetWindowRgn.assert_called_once_with(hwnd, 31, True)
                self.gdi.DeleteObject.assert_not_called()

    def test_failure_or_native_exception_deletes_unowned_region(self):
        for result in (0, OSError("unavailable")):
            with self.subTest(result=result):
                self.gdi.DeleteObject.reset_mock()
                self.user.SetWindowRgn.side_effect = result if isinstance(result, Exception) else None
                self.user.SetWindowRgn.return_value = result
                self.assertFalse(dashboard._set_window_corners(self.widget, 300, 44, 10))
                self.gdi.DeleteObject.assert_called_once_with(31)

    def test_missing_handle_or_failed_region_does_not_delete_invalid_handle(self):
        self.user.GetAncestor.return_value = None
        self.assertFalse(dashboard._set_window_corners(self.widget, 300, 44, 10))
        self.gdi.CreateRoundRectRgn.assert_not_called()
        self.user.GetAncestor.return_value = 12
        self.gdi.CreateRoundRectRgn.return_value = None
        self.assertFalse(dashboard._set_window_corners(self.widget, 300, 44, 10))
        self.user.SetWindowRgn.assert_not_called()
        self.gdi.DeleteObject.assert_not_called()

    def test_square_reset_and_hidden_window_do_not_allocate_region(self):
        self.widget.winfo_ismapped.return_value = False
        self.assertTrue(dashboard._set_window_corners(self.widget, 300, 44, 0))
        self.user.SetWindowRgn.assert_called_once_with(12, None, False)
        self.gdi.CreateRoundRectRgn.assert_not_called()
        self.gdi.DeleteObject.assert_not_called()

    def test_radius_is_bounded_by_small_window(self):
        self.assertTrue(dashboard._set_window_corners(self.widget, 20, 8, 15))
        self.gdi.CreateRoundRectRgn.assert_called_once_with(0, 0, 20, 8, 8, 8)


class CornerLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.strip = dashboard.Strip.__new__(dashboard.Strip)
        self.strip.root = Mock()
        self.strip.root.winfo_width.return_value = 300
        self.strip.root.winfo_height.return_value = 44
        self.strip._root_frame = Mock()
        self.strip._root_frame.winfo_width.return_value = 296
        self.strip._root_frame.winfo_height.return_value = 40
        self.strip.mode, self.strip.docked = "taskbar", False
        self.strip._corner_key, self.strip._corner_dpi = None, 144
        self.strip._corner_syncing = False
        self.strip._corner_timer = None
        self.strip.root.after_idle.return_value = "corner-idle"
        self.event = SimpleNamespace(widget=self.strip.root)
        platform = patch.object(dashboard.sys, "platform", "win32")
        native = patch.object(dashboard, "_set_window_corners", return_value=True)
        dpi = patch.object(dashboard, "_window_dpi")
        platform.start()
        self.apply = native.start()
        self.dpi = dpi.start()
        self.addCleanup(platform.stop)
        self.addCleanup(native.stop)
        self.addCleanup(dpi.stop)

    def sync(self, event=None, **kwargs):
        self.strip._sync_window_corners(event, **kwargs)
        if self.strip._corner_timer is not None:
            self.strip._apply_window_corners()

    def test_moves_are_cached_resize_and_dpi_changes_rebuild(self):
        self.sync(self.event)
        self.assertEqual(self.apply.call_args_list, [call(self.strip.root, 300, 44, 15),
                         call(self.strip._root_frame, 296, 40, 13, wrapper=False)])
        for _ in range(200):
            self.strip._sync_window_corners(self.event)
        self.assertEqual(self.apply.call_count, 2)
        self.strip.root.after_idle.assert_called_once()
        self.dpi.assert_not_called()
        self.strip.root.winfo_width.return_value = 420
        self.strip._root_frame.winfo_width.return_value = 416
        self.sync(self.event)
        self.assertEqual(self.apply.call_args_list[-2:], [call(self.strip.root, 420, 44, 15),
                         call(self.strip._root_frame, 416, 40, 13, wrapper=False)])
        self.strip._corner_dpi = 192
        self.sync()
        self.assertEqual(self.apply.call_args_list[-2:], [call(self.strip.root, 420, 44, 20),
                         call(self.strip._root_frame, 416, 40, 18, wrapper=False)])
        self.assertEqual(self.apply.call_count, 6)

    def test_dock_and_panel_restore_square_and_float_restores_round(self):
        self.sync()
        self.strip.docked = True
        self.sync()
        self.assertEqual(self.apply.call_args_list[-2:], [call(self.strip.root, 300, 44, 0),
                         call(self.strip._root_frame, 296, 40, 0, wrapper=False)])
        self.strip.docked = False
        self.sync()
        self.assertEqual(self.apply.call_args_list[-2:], [call(self.strip.root, 300, 44, 15),
                         call(self.strip._root_frame, 296, 40, 13, wrapper=False)])
        self.strip.mode = "panel"
        self.sync()
        self.assertEqual(self.apply.call_args_list[-2:], [call(self.strip.root, 300, 44, 0),
                         call(self.strip._root_frame, 296, 40, 0, wrapper=False)])

    def test_remap_and_root_frame_resize_reapply_but_unrelated_child_configure_is_ignored(self):
        self.sync()
        self.sync(SimpleNamespace(widget=Mock()), force=True)
        self.assertEqual(self.apply.call_count, 2)
        self.sync(self.event, force=True)
        self.assertEqual(self.apply.call_count, 4)
        self.strip._root_frame.winfo_width.return_value = 294
        self.sync(SimpleNamespace(widget=self.strip._root_frame))
        self.assertEqual(self.apply.call_count, 6)

    def test_failed_update_retries_and_native_reentrant_configure_is_ignored(self):
        self.apply.return_value = False
        self.sync()
        self.assertIsNone(self.strip._corner_key)
        def configure(*args, **kwargs):
            self.strip._sync_window_corners(self.event)
            return True
        self.apply.side_effect = configure
        self.sync()
        self.assertEqual(self.apply.call_count, 4)
        self.assertFalse(self.strip._corner_syncing)
        self.assertEqual(self.strip._corner_key, (300, 44, 15, 296, 40, 2))

    def test_pending_geometry_finishes_before_native_update_and_requests_coalesce(self):
        self.strip._sync_window_corners()
        self.apply.assert_not_called()
        self.strip.root.winfo_width.return_value = 420
        self.strip._root_frame.winfo_width.return_value = 416
        self.strip._sync_window_corners(self.event)
        self.strip.root.after_cancel.assert_called_once_with("corner-idle")
        self.apply.assert_not_called()
        self.strip._apply_window_corners()
        self.assertEqual(self.apply.call_args_list, [call(self.strip.root, 420, 44, 15),
                         call(self.strip._root_frame, 416, 40, 13, wrapper=False)])
        self.assertIsNone(self.strip._corner_timer)

    def test_destroy_cancels_pending_native_update(self):
        for name in ("_outside_timer", "_refresh_timer", "_zorder_timer",
                     "_drag_timer", "_drag_capture_timer"):
            setattr(self.strip, name, None)
        self.strip._sync_window_corners()
        self.strip._destroyed(self.event)
        self.strip.root.after_cancel.assert_called_once_with("corner-idle")
        self.assertIsNone(self.strip._corner_timer)
        self.apply.assert_not_called()


@unittest.skipUnless(sys.platform == "win32", "Windows native window regions")
class HiddenNativeCornerTests(unittest.TestCase):
    def test_hidden_native_region_clips_corners_resizes_and_resets(self):
        import ctypes
        from ctypes import wintypes
        user, gdi = ctypes.windll.user32, ctypes.windll.gdi32
        user.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                         wintypes.DWORD, ctypes.c_int, ctypes.c_int,
                                         ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                         wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p]
        user.CreateWindowExW.restype = wintypes.HWND
        user.DestroyWindow.argtypes = [wintypes.HWND]
        user.GetWindowRgn.argtypes = [wintypes.HWND, wintypes.HRGN]
        user.GetWindowRgn.restype = ctypes.c_int
        gdi.CreateRectRgn.argtypes = [ctypes.c_int] * 4
        gdi.CreateRectRgn.restype = wintypes.HRGN
        gdi.PtInRegion.argtypes = [wintypes.HRGN, ctypes.c_int, ctypes.c_int]
        gdi.PtInRegion.restype = wintypes.BOOL
        gdi.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        gdi.DeleteObject.restype = wintypes.BOOL
        # Never shown, focused, or added to the taskbar.
        hwnd = user.CreateWindowExW(0x80, "STATIC", "handback corner test", 0x80000000,
                                    0, 0, 300, 60, None, None, None, None)
        self.assertTrue(hwnd)
        self.addCleanup(user.DestroyWindow, hwnd)
        region = gdi.CreateRectRgn(0, 0, 0, 0)
        self.assertTrue(region)
        self.addCleanup(gdi.DeleteObject, region)
        widget = Mock()
        widget.winfo_id.return_value = hwnd
        widget.winfo_ismapped.return_value = False
        for width, radius in ((180, 10), (300, 15)):
            with self.subTest(width=width, radius=radius):
                self.assertTrue(dashboard._set_window_corners(widget, width, 60, radius))
                self.assertEqual(user.GetWindowRgn(hwnd, region), 3)  # COMPLEXREGION
                self.assertFalse(gdi.PtInRegion(region, 0, 0))
                self.assertFalse(gdi.PtInRegion(region, width-1, 59))
                self.assertTrue(gdi.PtInRegion(region, width//2, 1))
                self.assertTrue(gdi.PtInRegion(region, width-2, 30))
        self.assertTrue(dashboard._set_window_corners(widget, 300, 60, 0))
        self.assertEqual(user.GetWindowRgn(hwnd, region), 0)  # No custom region.


if __name__ == "__main__":
    unittest.main()
