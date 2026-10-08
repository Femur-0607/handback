import json
from contextlib import contextmanager
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from handback import dashboard, envelope, inbox
from handback.state import ProjectState


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dashboard-test-", dir=Path(__file__).parent)
        self.home = Path(self.temp.name) / "home"
        self.checkout = Path(self.temp.name) / "Game"
        self.checkout.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def project(self, key, root, workers=("codex", "antigravity")):
        folder = self.home / "projects" / key
        (folder / "requests").mkdir(parents=True)
        self.write(folder / "topology.json", {"lead": "claude:abc", "workers": list(workers),
                                              "fallback": "ask", "root": str(root)})
        return folder

    def write(self, path, value):
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def test_row_counts_open_requests_and_unacknowledged_mail(self):
        folder = self.project("a" * 64, self.checkout)
        self.write(folder / "threads.json", {"codex:t1": {"name": "4-15 정리"}})
        for request_id, status in (("1" * 32, "accepted"), ("2" * 32, "completed")):
            self.write(folder / "requests" / (request_id + ".json"),
                       {"id": request_id, "handle": "codex:t1", "agent": "codex", "status": status})
        read = inbox.put(folder / "inbox", envelope.make("r1", "codex:t1", "claude:abc", "done"))
        inbox.put(folder / "inbox", envelope.make("r2", "codex:t1", "claude:abc", "done too"))
        inbox.acknowledge(folder / "inbox", read["id"])

        [row] = dashboard.snapshot(self.home)
        self.assertEqual((row["name"], row["combo"]), ("Game", "Claude → Codex + Antigravity"))
        self.assertEqual([item["name"] for item in row["running"]], ["4-15 정리"])
        self.assertEqual([item["from"] for item in row["unread"]], ["4-15 정리"])
        self.assertGreater(row["last_activity"], 0)

    def test_outgoing_request_copies_are_not_unread(self):
        folder = self.project("d" * 64, self.checkout)
        inbox.put(folder / "inbox", envelope.make("r1", "claude:abc", "codex:t1", "brief", kind="request"))
        inbox.put(folder / "inbox", envelope.make("r2", "codex:t1", "claude:abc", "done"))
        [row] = dashboard.snapshot(self.home)
        self.assertEqual([item["body"] for item in row["unread"]], ["done"])

    def test_acknowledge_all_keeps_messages_and_skips_requests(self):
        folder = self.project("e" * 64, self.checkout)
        inbox.put(folder / "inbox", envelope.make("r1", "claude:abc", "codex:t1", "brief", kind="request"))
        for body in ("one", "two"):
            inbox.put(folder / "inbox", envelope.make("r", "codex:t1", "claude:abc", body))
        self.assertEqual(dashboard.acknowledge_all(str(self.checkout), self.home), 2)
        self.assertEqual([m["kind"] for m in inbox.pending(folder / "inbox")], ["request"])
        self.assertEqual(len(list((folder / "inbox").glob("*.json"))), 3)
        self.assertEqual(dashboard.snapshot(self.home)[0]["unread"], [])

    def test_release_moves_state_aside_and_clears_prefs(self):
        folder = self.project("f" * 64, self.checkout)
        dashboard.save_prefs({"max_rows": 2, "order": [str(self.checkout)], "hidden": []}, self.home)
        target = dashboard.release(str(self.checkout), self.home)
        self.assertFalse(folder.exists())
        self.assertTrue((target / "topology.json").exists())
        self.assertEqual(target.parent, self.home / "released")
        self.assertEqual(dashboard.snapshot(self.home), [])
        self.assertEqual(dashboard.load_prefs(self.home)["order"], [])

    def test_release_refuses_open_requests_and_unknown_roots(self):
        folder = self.project("9" * 64, self.checkout)
        self.write(folder / "requests" / ("1" * 32 + ".json"), {"id": "1" * 32, "status": "accepted"})
        with self.assertRaises(ValueError):
            dashboard.release(str(self.checkout), self.home)
        self.assertTrue(folder.exists())
        with self.assertRaises(ValueError):
            dashboard.release(str(self.checkout / "nope"), self.home)

    def test_release_does_not_archive_a_request_created_between_check_and_move(self):
        folder = self.project("8" * 64, self.checkout)
        identity = {"root": str(self.checkout), "checkout_root": str(self.checkout),
                    "key": folder.name, "common_dir": None}
        with patch("handback.state.project_identity", return_value=identity):
            state = ProjectState(self.checkout, home=self.home)
        request = {"id": "2" * 32, "status": "accepted"}
        started, saved = threading.Event(), threading.Event()
        errors, saved_before_move = [], []

        def create_request():
            started.set()
            try:
                state.save_request(request)
                saved.set()
            except Exception as error:
                errors.append(error)

        writer = threading.Thread(target=create_request)
        rename = Path.rename

        def race(path, target):
            if path == folder:
                writer.start()
                self.assertTrue(started.wait(2))
                saved_before_move.append(saved.wait(0.5))
            return rename(path, target)

        try:
            with patch.object(Path, "rename", race):
                archived = dashboard.release(str(self.checkout), self.home)
        finally:
            if writer.ident is not None:
                writer.join(5)
        self.assertFalse(writer.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(saved_before_move, [False], "Request writer crossed the archive transaction")
        self.assertTrue(saved.is_set())
        self.assertEqual(state.load_request(request["id"]), request)
        self.assertFalse((archived / "requests" / (request["id"] + ".json")).exists())

    def test_release_checks_requests_even_when_project_is_hidden_from_snapshot(self):
        folder = self.project("7" * 64, self.checkout)
        self.write(folder / "requests" / ("1" * 32 + ".json"), {"id": "1" * 32, "status": "accepted"})
        with patch.object(dashboard.tempfile, "gettempdir", return_value=self.temp.name):
            with self.assertRaisesRegex(ValueError, "진행 중인 요청"):
                dashboard.release(str(self.checkout), self.home)
        self.assertTrue(folder.exists())

    def test_release_rechecks_request_created_while_waiting_for_ownership(self):
        folder = self.project("5" * 64, self.checkout)
        identity = {"root": str(self.checkout), "checkout_root": str(self.checkout),
                    "key": folder.name, "common_dir": None}
        with patch("handback.state.project_identity", return_value=identity):
            state = ProjectState(self.checkout, home=self.home)
        waiting, outcomes = threading.Event(), []
        home_lock = dashboard.home_lock

        @contextmanager
        def observed_lock(*args, **kwargs):
            waiting.set()
            with home_lock(*args, **kwargs) as locked:
                yield locked

        def unregister():
            try:
                outcomes.append(dashboard.release(str(self.checkout), self.home))
            except Exception as error:
                outcomes.append(error)

        worker = threading.Thread(target=unregister)
        with patch.object(dashboard, "home_lock", observed_lock):
            with state.lock():
                worker.start()
                self.assertTrue(waiting.wait(2))
                state.save_request({"id": "3" * 32, "status": "accepted"})
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(outcomes), 1)
        self.assertIsInstance(outcomes[0], ValueError)
        self.assertIn("진행 중인 요청", str(outcomes[0]))
        self.assertTrue(folder.exists())

    def test_release_refuses_damaged_request_state(self):
        folder = self.project("6" * 64, self.checkout)
        path = folder / "requests" / ("1" * 32 + ".json")
        for content in ('{"status":', 'null', '{}'):
            with self.subTest(content=content):
                path.write_text(content, encoding="utf-8")
                with self.assertRaises(ValueError):
                    dashboard.release(str(self.checkout), self.home)
                self.assertTrue(folder.exists())

    def test_missing_and_temp_roots_are_hidden(self):
        self.project("b" * 64, Path(self.temp.name) / "gone")
        self.project("c" * 64, self.checkout)
        with patch.object(dashboard.tempfile, "gettempdir", return_value=self.temp.name):
            self.assertEqual(dashboard.snapshot(self.home), [])
        self.assertEqual(len(dashboard.snapshot(self.home)), 1)

    def test_snapshot_without_state_is_empty(self):
        self.assertEqual(dashboard.snapshot(self.home), [])

    def rows(self, *names):
        return [{"root": name, "name": name, "last_activity": 100 - index, "running": [], "unread": []}
                for index, name in enumerate(names)]

    def test_arrange_pins_order_hides_and_limits_rows(self):
        rows = self.rows("a", "b", "c", "d")
        prefs = {**dashboard.DEFAULT_PREFS}
        shown, overflow = dashboard.arrange(rows, prefs)
        self.assertEqual([r["root"] for r in shown], ["a", "b", "c"])
        self.assertEqual([r["root"] for r in overflow], ["d"])
        prefs = {"max_rows": 2, "order": ["c"], "hidden": ["a"]}
        shown, overflow = dashboard.arrange(rows, prefs)
        self.assertEqual([r["root"] for r in shown], ["c", "b"])
        self.assertEqual([r["root"] for r in overflow], ["d"])
        shown, overflow = dashboard.arrange(rows, {**prefs, "max_rows": 0})
        self.assertEqual(([r["root"] for r in shown], overflow), (["c", "b", "d"], []))

    def test_move_pins_displayed_order_and_clamps(self):
        prefs = dict(dashboard.DEFAULT_PREFS)
        self.assertEqual(dashboard.move(prefs, ["a", "b", "c"], "c", -1)["order"], ["a", "c", "b"])
        self.assertEqual(dashboard.move(prefs, ["a", "b"], "a", -1)["order"], ["a", "b"])
        self.assertIs(dashboard.move(prefs, ["a"], "zzz", 1), prefs)

    def test_prefs_round_trip_and_reject_bad_values(self):
        self.assertEqual(dashboard.load_prefs(self.home), dashboard.DEFAULT_PREFS)
        dashboard.save_prefs({"max_rows": 2, "order": ["x"], "hidden": ["y"]}, self.home)
        self.assertEqual(dashboard.load_prefs(self.home), {**dashboard.DEFAULT_PREFS, "max_rows": 2, "order": ["x"], "hidden": ["y"]})
        self.write(dashboard.prefs_path(self.home), {"max_rows": -1, "order": "x"})
        self.assertEqual(dashboard.load_prefs(self.home), dashboard.DEFAULT_PREFS)

    def test_ago_formats_korean_relative_time(self):
        self.assertEqual(dashboard.ago(0), "-")
        self.assertEqual(dashboard.ago(1700000100, now=1700000130), "방금")
        self.assertEqual(dashboard.ago(1700000000.1, now=1700000600), "9분 전")
        self.assertEqual(dashboard.ago(1700000001, now=1700007300), "2시간 전")
        self.assertEqual(dashboard.ago(1700000001, now=1700200000), "2일 전")

    def test_compact_preferences_legacy_defaults_and_validation(self):
        dashboard.save_prefs({}, self.home)
        self.assertEqual(dashboard.load_prefs(self.home)["mode"], "taskbar")
        self.assertIsNone(dashboard.load_prefs(self.home)["compact_x"])
        self.assertIsNone(dashboard.load_prefs(self.home)["compact_y"])
        self.assertTrue(dashboard.load_prefs(self.home)["docked"])
        dashboard.save_prefs({"mode": "panel", "compact_x": -450}, self.home)
        self.assertEqual(dashboard.load_prefs(self.home)["compact_x"], -450)
        self.assertEqual(dashboard.load_prefs(self.home)["mode"], "panel")
        self.assertTrue(dashboard.load_prefs(self.home)["docked"])
        dashboard.save_prefs({"compact_x": 500, "compact_y": -200, "docked": False}, self.home)
        self.assertEqual((dashboard.load_prefs(self.home)["compact_x"],
                          dashboard.load_prefs(self.home)["compact_y"],
                          dashboard.load_prefs(self.home)["docked"]), (500, -200, False))
        for y, docked in ((True, 1), ("50", "false"), (2.5, None)):
            dashboard.save_prefs({"compact_y": y, "docked": docked}, self.home)
            self.assertEqual(dashboard.load_prefs(self.home), dashboard.DEFAULT_PREFS)
        for x in (True, "50", 2.5, None):
            dashboard.save_prefs({"mode": "unknown", "compact_x": x}, self.home)
            self.assertEqual(dashboard.load_prefs(self.home), dashboard.DEFAULT_PREFS)


if __name__ == "__main__":
    unittest.main()
