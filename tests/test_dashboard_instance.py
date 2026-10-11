"""Single instance: a second launch asks the running dashboard to come forward."""
import ctypes
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from handback import dashboard


class SingleInstanceTests(unittest.TestCase):
    def setUp(self):
        self.kernel = Mock()
        self.kernel.CreateMutexW.return_value = 5
        self.kernel.CreateEventW.return_value = 7
        self.kernel.SetEvent.return_value = 1
        # windll is Windows-only; a Mock also keeps AllowSetForegroundWindow off the real API.
        for target, name, value in ((dashboard.sys, "platform", "win32"),
                                    (dashboard, "_kernel32", Mock(return_value=self.kernel)),
                                    (ctypes, "windll", Mock()),
                                    (dashboard, "Strip", Mock()),
                                    (dashboard, "repair_autostart", Mock()),
                                    (dashboard, "set_app_user_model_id", Mock())):
            fixture = patch.object(target, name, value, create=name == "windll")
            fixture.start()
            self.addCleanup(fixture.stop)

    def last_error(self, code):
        fixture = patch.object(ctypes, "get_last_error", return_value=code, create=True)
        fixture.start()
        self.addCleanup(fixture.stop)

    def test_second_instance_signals_event_and_does_not_start_tk(self):
        self.last_error(183)  # ERROR_ALREADY_EXISTS
        self.assertEqual(dashboard.main(), 0)
        self.kernel.CreateEventW.assert_called_once_with(None, False, False, dashboard.DASHBOARD_SHOW_EVENT)
        self.kernel.SetEvent.assert_called_once_with(7)
        closed = [call.args[0] for call in self.kernel.CloseHandle.call_args_list]
        self.assertEqual(sorted(closed), [5, 7])
        dashboard.Strip.assert_not_called()
        dashboard.repair_autostart.assert_not_called()

    def test_second_instance_without_event_falls_back_to_silent_exit(self):
        self.last_error(183)
        self.kernel.CreateEventW.return_value = 0
        self.assertEqual(dashboard.main(), 0)
        self.kernel.SetEvent.assert_not_called()
        dashboard.Strip.assert_not_called()
        self.kernel.CreateEventW.side_effect = OSError("denied")
        self.assertFalse(dashboard.signal_running_instance())

    def test_primary_creates_auto_reset_event_and_watches_it(self):
        self.last_error(0)
        dashboard.main()
        self.kernel.CreateMutexW.assert_called_once_with(None, False, dashboard.DASHBOARD_MUTEX)
        self.kernel.CreateEventW.assert_called_once_with(None, False, False, dashboard.DASHBOARD_SHOW_EVENT)
        self.kernel.SetEvent.assert_not_called()
        strip = dashboard.Strip.return_value
        strip.watch_show_requests.assert_called_once_with(7)
        strip.run.assert_called_once_with()

    def test_show_requested_only_on_wait_object_0(self):
        self.kernel.WaitForSingleObject.return_value = 0
        self.assertTrue(dashboard.show_requested(7))
        self.kernel.WaitForSingleObject.assert_called_once_with(7, 0)
        self.kernel.WaitForSingleObject.return_value = 258  # WAIT_TIMEOUT
        self.assertFalse(dashboard.show_requested(7))
        self.assertFalse(dashboard.show_requested(None))
        self.kernel.WaitForSingleObject.side_effect = OSError("closed")
        self.assertFalse(dashboard.show_requested(7))

    def test_non_windows_is_a_no_op(self):
        with patch.object(dashboard.sys, "platform", "linux"):
            self.assertEqual(dashboard._single_instance(), ("primary", (None, None)))
            self.assertFalse(dashboard.signal_running_instance())
            self.assertFalse(dashboard.show_requested(7))
        dashboard._kernel32.assert_not_called()


class ShowPollTests(unittest.TestCase):
    """The Tk timer is the only code that reacts to the event."""

    def strip(self):
        strip = dashboard.Strip.__new__(dashboard.Strip)
        strip.root = Mock()
        strip.tk = SimpleNamespace(TclError=RuntimeError)
        strip.show = Mock()
        return strip

    def test_signal_shows_brings_to_front_and_rearms(self):
        strip = self.strip()
        with patch.object(dashboard.sys, "platform", "win32"):
            self.assertTrue(strip.watch_show_requests(7))
        strip.root.after.assert_called_once_with(dashboard.SHOW_POLL_MS, strip._poll_show_request)
        strip.root.after.reset_mock()
        with patch.object(dashboard, "show_requested", return_value=True) as requested, \
                patch.object(dashboard, "bring_to_foreground") as foreground:
            strip._poll_show_request()
        requested.assert_called_once_with(7)
        strip.show.assert_called_once_with()
        foreground.assert_called_once_with(strip.root)
        strip.root.after.assert_called_once_with(dashboard.SHOW_POLL_MS, strip._poll_show_request)

    def test_no_signal_only_rearms_and_errors_do_not_kill_the_timer(self):
        strip = self.strip()
        strip._show_event = 7
        with patch.object(dashboard, "show_requested", return_value=False), \
                patch.object(dashboard, "bring_to_foreground") as foreground:
            strip._poll_show_request()
        strip.show.assert_not_called()
        foreground.assert_not_called()
        self.assertEqual(strip.root.after.call_count, 1)
        strip.show.side_effect = ValueError("boom")
        with patch.object(dashboard, "show_requested", return_value=True), \
                patch.object(dashboard, "bring_to_foreground"):
            with self.assertRaises(ValueError):
                strip._poll_show_request()
        self.assertEqual(strip.root.after.call_count, 2)
        self.assertIsNotNone(strip._show_timer)

    def test_without_event_nothing_is_scheduled(self):
        strip = self.strip()
        self.assertFalse(strip.watch_show_requests(None))
        with patch.object(dashboard.sys, "platform", "linux"):
            self.assertFalse(strip.watch_show_requests(7))
        strip.root.after.assert_not_called()


if __name__ == "__main__":
    unittest.main()
