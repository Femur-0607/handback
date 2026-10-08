import unittest
from unittest.mock import patch

from tests import gui_resources, test_dashboard_gui


class GuiResourceAssertionTests(unittest.TestCase):
    def check_resources(self, actual, windows):
        # Exercise the shared assertion without creating a Tk window.
        case = test_dashboard_gui.DashboardMenuTests()
        with patch.object(case, "handles", return_value=actual), \
                patch.object(gui_resources, "windows", return_value=windows):
            case.assert_handles_unchanged((49, 30), {1: "TkTopLevel"}, "refresh 500")

    def test_unchanged_resources_pass(self):
        self.check_resources((49, 30), {1: "TkTopLevel"})

    def test_delayed_resource_release_passes_with_identical_hwnds(self):
        for actual in ((49, 29), (48, 30), (48, 29)):
            with self.subTest(actual=actual):
                self.check_resources(actual, {1: "TkTopLevel"})

    def test_each_resource_increase_fails_even_if_the_other_decreases(self):
        for actual, increase in (((50, 29), (1, 0)), ((48, 31), (0, 1)), ((50, 31), (1, 1))):
            with self.subTest(actual=actual):
                with self.assertRaises(AssertionError) as failure:
                    self.check_resources(actual, {1: "TkTopLevel"})
                message = str(failure.exception)
                self.assertIn("refresh 500", message)
                self.assertIn(f"GDI/USER {actual}, baseline (49, 30), increase {increase}", message)

    def test_hwnd_addition_removal_or_replacement_fails_without_resource_growth(self):
        for windows in ({1: "TkTopLevel", 2: "TkChild"}, {}, {2: "TkTopLevel"}):
            for actual in ((49, 30), (49, 29)):
                with self.subTest(windows=windows, actual=actual):
                    with self.assertRaises(AssertionError) as failure:
                        self.check_resources(actual, windows)
                    message = str(failure.exception)
                    self.assertIn("increase (0, 0)", message)
                    if 2 in windows:
                        self.assertIn(f"'added': {{'0x2': '{windows[2]}'}}", message)
                    if 1 not in windows:
                        self.assertIn("'removed': {'0x1': 'TkTopLevel'}", message)
