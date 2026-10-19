import unittest

from handback import dock_motion as motion


BOTTOM = {"rect": (0, 1032, 1920, 1080), "edge": 3, "auto_hide": False}
TOP = {"rect": (0, 0, 1920, 48), "edge": 1, "auto_hide": False}


class TaskbarGapTest(unittest.TestCase):
    def test_gap_measures_across_the_desktop_facing_edge_only(self):
        self.assertEqual(motion.taskbar_gap(BOTTOM, (700, 1040)), 0)
        self.assertEqual(motion.taskbar_gap(BOTTOM, (700, 1031)), 1)
        self.assertEqual(motion.taskbar_gap(BOTTOM, (700, 1000)), 32)
        self.assertIsNone(motion.taskbar_gap(BOTTOM, (1920, 1000)))
        self.assertEqual(motion.taskbar_gap(TOP, (700, 48)), 1)
        self.assertEqual(motion.taskbar_gap(TOP, (700, 80)), 33)

    def test_unavailable_taskbars_never_pull(self):
        for info in (None, {**BOTTOM, "auto_hide": True}, {**BOTTOM, "edge": 0},
                     {**BOTTOM, "edge": 2}, {**BOTTOM, "rect": (0, 1080, 1920, 1080)}):
            with self.subTest(info=info):
                self.assertIsNone(motion.taskbar_gap(info, (700, 1040)))


class MagnetTest(unittest.TestCase):
    def test_strength_is_zero_outside_radius_and_full_inside_taskbar(self):
        self.assertEqual(motion.magnet_strength(None, 56), 0)
        self.assertEqual(motion.magnet_strength(56, 56), 0)
        self.assertEqual(motion.magnet_strength(500, 56), 0)
        self.assertEqual(motion.magnet_strength(0, 56), 1)
        self.assertAlmostEqual(motion.magnet_strength(28, 56), 0.5)

    def test_strength_rises_monotonically_and_eases_at_both_ends(self):
        values = [motion.magnet_strength(gap, 56) for gap in range(56, -1, -1)]
        self.assertEqual(values, sorted(values))
        # Eased: the first pixel of approach barely moves, the middle moves most.
        self.assertLess(values[1] - values[0], values[28] - values[27])

    def test_pull_blends_toward_target_and_keeps_raw_tracking_outside(self):
        self.assertEqual(motion.magnet_pull((700, 960), (700, 1033), 0), (700, 960))
        self.assertEqual(motion.magnet_pull((700, 960), (700, 1033), 1), (700, 1033))
        self.assertEqual(motion.magnet_pull((700, 960), (700, 1032), 0.5), (700, 996))


class BreakawayTest(unittest.TestCase):
    def test_lags_behind_pointer_until_threshold_then_tracks_one_to_one(self):
        shown, broke = motion.breakaway((0, -10), 28)
        self.assertFalse(broke)
        self.assertTrue(-10 < shown[1] < 0)
        self.assertEqual(motion.breakaway((0, 0), 28), ((0, 0), False))
        self.assertEqual(motion.breakaway((0, -28), 28), ((0, -28), True))
        self.assertEqual(motion.breakaway((30, -40), 28), ((30, -40), True))

    def test_stretch_grows_with_pull_and_pops_at_the_threshold(self):
        stretches = [abs(motion.breakaway((0, -pull), 28)[0][1]) for pull in range(28)]
        self.assertEqual(stretches, sorted(stretches))
        # The widget visibly jumps when it pops free.
        self.assertGreater(28 - stretches[-1], 10)

    def test_keeps_the_pull_direction(self):
        shown, _ = motion.breakaway((-12, -12), 28)
        self.assertEqual(shown[0], shown[1])
        self.assertLess(shown[0], 0)


class SettleTest(unittest.TestCase):
    def test_ends_exactly_on_target_within_the_duration(self):
        for start, target in (((700, 960), (700, 1033)), ((-1900, 840), (-1850, 833)),
                              ((0, 0), (3, 0))):
            with self.subTest(start=start):
                frames = motion.settle_frames(start, target)
                self.assertEqual(frames[-1], target)
                self.assertLessEqual(len(frames), motion.SETTLE_MS // motion.SETTLE_FRAME_MS + 1)

    def test_small_overshoot_then_returns(self):
        frames = [frame[1] for frame in motion.settle_frames((0, 0), (0, 100))]
        self.assertGreater(max(frames), 100)
        self.assertLessEqual(max(frames), 110)
        self.assertLess(frames[0], 50)

    def test_critical_damping_never_overshoots(self):
        frames = [frame[1] for frame in motion.settle_frames((0, 0), (0, 100), damping=1)]
        self.assertEqual(frames, sorted(frames))
        self.assertEqual(frames[-1], 100)

    def test_no_motion_needed(self):
        self.assertEqual(motion.settle_frames((5, 5), (5, 5)), [(5, 5)])
        self.assertEqual(motion.settle_frames((0, 0), (5, 5), duration_ms=0), [(5, 5)])

    def test_spring_offset_starts_at_one_and_decays(self):
        self.assertAlmostEqual(motion.spring_offset(0, 30, 0.7), 1)
        self.assertAlmostEqual(motion.spring_offset(0, 30, 1), 1)
        self.assertLess(abs(motion.spring_offset(1, 30, 0.7)), 0.01)

    def test_preview_and_lean_leave_room_for_the_release_spring(self):
        self.assertLess(motion.MAGNET_LEAN, motion.DOCK_PREVIEW)
        self.assertLess(motion.DOCK_PREVIEW, 1)

    def test_distances_scale_with_dpi(self):
        self.assertEqual(motion.scaled(motion.MAGNET_RADIUS, 96), 56)
        self.assertEqual(motion.scaled(motion.MAGNET_RADIUS, 192), 112)
        self.assertEqual(motion.scaled(motion.BREAKAWAY_DISTANCE, 144), 42)


if __name__ == "__main__":
    unittest.main()
