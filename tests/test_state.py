from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import uuid

from agent_relay.state import ProjectState, atomic_json, home_lock, moved_destination, project_identity, state_home, state_warnings


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.cleanup_temporary)
        environment = mock.patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": self.temporary.name})
        environment.start()
        self.addCleanup(environment.stop)
        self.root = Path(self.temporary.name) / "project"
        self.root.mkdir()
        self.home = Path(self.temporary.name) / "state"
        self.state = ProjectState(self.root, home=self.home)

    def cleanup_temporary(self):
        # Git/indexer directory handles can briefly outlive a Windows subprocess.
        # Retry only our exact temporary fixture, never a caller-provided path.
        Path(self.temporary.name).resolve().relative_to(Path(__file__).parent.resolve())
        for attempt in range(5):
            try:
                self.temporary.cleanup()
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))

    def test_reads_do_not_create_state(self):
        self.assertEqual(self.state.read_json("topology.json", {}), {})
        self.assertEqual(self.state.requests(), [])
        self.assertEqual(self.state.threads(), {})
        self.assertFalse(self.home.exists())

    def test_non_git_roots_have_distinct_stable_keys(self):
        other = self.root.parent / "other"
        other.mkdir()
        self.assertEqual(project_identity(self.root), project_identity(self.root / "."))
        self.assertNotEqual(project_identity(self.root)["key"], project_identity(other)["key"])
        self.assertIsNone(project_identity(self.root)["common_dir"])

    @unittest.skipUnless(shutil.which("git"), "git unavailable")
    def test_real_git_worktrees_share_identity(self):
        def git(*args):
            return subprocess.run(["git", "-c", "user.name=Relay Test", "-c", "user.email=test@example.invalid",
                                   "-C", str(self.root), *args], capture_output=True, text=True,
                                  encoding="utf-8", timeout=15, check=True)
        git("init", "-q")
        git("commit", "-q", "--allow-empty", "-m", "temporary test")
        other = self.root.parent / "worktree"
        git("worktree", "add", "-q", "--detach", str(other))
        self.assertEqual(project_identity(self.root)["key"], project_identity(other)["key"])
        first = ProjectState(self.root, home=self.home)
        second = ProjectState(other, home=self.home)
        first.write_json("topology.json", {"lead": "claude"})
        self.assertEqual(second.read_json("topology.json"), {"lead": "claude"})
        nested = other / "nested"
        nested.mkdir()
        self.assertEqual(ProjectState(nested, home=self.home).checkout_root, other.resolve())

    def test_atomic_write_unicode_and_partial_files(self):
        self.state.write_json("sample.json", {"text": "한글\n두 번째 줄"})
        self.assertEqual(self.state.read_json("sample.json"), {"text": "한글\n두 번째 줄"})
        self.assertEqual(list(self.state.path.glob("*.tmp")), [])
        self.assertNotIn(b"\r\n", (self.state.path / "sample.json").read_bytes())
        self.state.write_json("sample.json", {"text": "replacement"})
        self.assertEqual(self.state.read_json("sample.json")["text"], "replacement")

    def test_atomic_failure_preserves_existing_file(self):
        path = self.root / "atomic.json"
        atomic_json(path, {"value": 1})
        with mock.patch("agent_relay.state.os.replace", side_effect=OSError("simulated failure")):
            with self.assertRaises(OSError):
                atomic_json(path, {"value": 2})
        self.assertEqual(json.loads(path.read_text()), {"value": 1})
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_path_and_request_id_validation(self):
        for invalid in ("../escape.json", self.root / "absolute.json"):
            with self.assertRaises(ValueError):
                self.state.write_json(invalid, {})
        for invalid in ("../outside", "a" * 31, "A" * 32, "g" * 32, None):
            with self.assertRaises(ValueError):
                self.state.load_request(invalid)
        request_id = uuid.uuid4().hex
        self.assertIsNone(self.state.load_request(request_id))
        self.state.save_request({"id": request_id, "status": "accepted"})
        self.assertEqual(self.state.load_request(request_id)["status"], "accepted")
        self.state.write_json("requests/stray.json", {"id": "stray"})
        self.assertEqual(len(self.state.requests()), 1)

    def test_corrupt_request_blocks_ownership_check(self):
        self.state.write_json("requests/" + "0" * 32 + ".json", {"id": "1" * 32})
        with self.assertRaisesRegex(ValueError, "Invalid request record"):
            self.state.requests()
        self.state.write_json("requests/" + "0" * 32 + ".json", {"id": "0" * 32})
        (self.state.path / "requests" / ("0" * 32 + ".json")).write_text("{", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Invalid JSON state"):
            self.state.requests()
        self.state.write_json("requests/" + "0" * 32 + ".json", None)
        with self.assertRaisesRegex(ValueError, "Invalid request record"):
            self.state.requests()

    def test_corrupt_state_raises_instead_of_resetting(self):
        self.state.path.mkdir(parents=True)
        (self.state.path / "threads.json").write_text("{", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.state.register_thread({"handle": "codex:first"})
        self.assertEqual((self.state.path / "threads.json").read_text(), "{")

    def test_nested_locks_and_concurrent_thread_registration(self):
        with self.state.lock():
            with ProjectState(self.root, home=self.home).lock():
                self.state.write_json("nested.json", {"ok": True})
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda index: self.state.register_thread(
                {"handle": f"codex:{index}", "name": f"worker {index}"}), range(16)))
        self.assertEqual(len(self.state.threads()), 16)
        self.state.register_thread({"handle": "codex:0", "cwd": str(self.root)})
        self.assertEqual(self.state.threads()["codex:0"]["name"], "worker 0")

    def test_cross_process_lock_timeout_and_release(self):
        script = """from agent_relay.state import ProjectState
import sys
state = ProjectState(sys.argv[1], home=sys.argv[2])
try:
    with state.lock(timeout=0.15):
        print('acquired')
except TimeoutError:
    print('busy')
"""
        command = [sys.executable, "-c", script, str(self.root), str(self.home)]
        with self.state.lock():
            busy = subprocess.run(command, cwd=Path(__file__).resolve().parents[1],
                                  capture_output=True, text=True, timeout=10)
        self.assertEqual(busy.returncode, 0, busy.stderr)
        self.assertEqual(busy.stdout.strip(), "busy")
        acquired = subprocess.run(command, cwd=Path(__file__).resolve().parents[1],
                                  capture_output=True, text=True, timeout=10)
        self.assertEqual(acquired.returncode, 0, acquired.stderr)
        self.assertEqual(acquired.stdout.strip(), "acquired")

    def test_crashed_lock_owner_does_not_leave_stale_ownership(self):
        script = """from agent_relay.state import ProjectState
import os, sys
with ProjectState(sys.argv[1], home=sys.argv[2]).lock():
    os._exit(0)
"""
        result = subprocess.run([sys.executable, "-c", script, str(self.root), str(self.home)],
                                cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0)
        with self.state.lock(timeout=0.2):
            self.state.write_json("recovered.json", {"ok": True})

    def test_state_home_explicit_and_platform_defaults(self):
        with mock.patch.dict(os.environ, {"AGENT_RELAY_HOME": str(self.home)}, clear=True):
            self.assertEqual(state_home(), self.home.resolve())
        with mock.patch.dict(os.environ, {"USERPROFILE": str(self.root), "LOCALAPPDATA": str(self.home)}, clear=True), \
                mock.patch("agent_relay.state.sys.platform", "win32"):
            self.assertEqual(state_home(), self.root / ".agent-relay")
        redirected = self.root / "Packages" / "sandbox.test" / "AC"
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(redirected)}, clear=True), \
                mock.patch("agent_relay.state.sys.platform", "win32"), \
                mock.patch("agent_relay.state.Path.home", return_value=self.root):
            self.assertEqual(state_home(), self.root / ".agent-relay")
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.home)}, clear=True), \
                mock.patch("agent_relay.state.sys.platform", "linux"):
            self.assertEqual(state_home(), self.home / "agent-relay")
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch("agent_relay.state.sys.platform", "darwin"), \
                mock.patch("agent_relay.state.Path.home", return_value=self.root):
            self.assertEqual(state_home(), self.root / "Library" / "Application Support" / "agent-relay")

    def test_shared_home_lock_allows_other_readers_and_excludes_migration(self):
        script = """import sys
from agent_relay.state import home_lock
try:
    with home_lock(sys.argv[1], exclusive=sys.argv[2] == 'exclusive', timeout=0.15):
        print('acquired')
except TimeoutError:
    print('busy')
"""
        def child(mode):
            result = subprocess.run([sys.executable, "-c", script, str(self.home), mode],
                                    cwd=Path(__file__).resolve().parents[1], capture_output=True,
                                    text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout.strip()
        with home_lock(self.home):
            self.assertEqual(child("shared"), "acquired")
            self.assertEqual(child("exclusive"), "busy")
            with self.assertRaisesRegex(ValueError, "Cannot upgrade"):
                with home_lock(self.home, exclusive=True):
                    pass
        with home_lock(self.home, exclusive=True):
            self.assertEqual(child("shared"), "busy")
            with home_lock(self.home):
                pass
        self.assertTrue((self.home / ".locks").is_dir())

    def test_moved_marker_warns_and_blocks_project_mutations(self):
        destination = self.root.parent / "new-state"
        marker = {"schema": 1, "id": "a" * 32, "source": str(self.home.resolve()),
                  "destination": str(destination.resolve()), "fingerprint": "f" * 64, "created_utc": "fixture"}
        atomic_json(self.home / "MOVED.json", marker)
        atomic_json(destination / ".migration-receipt.json", {**marker, "phase": "complete"})
        self.assertEqual(moved_destination(self.home), destination.resolve())
        self.assertTrue(any(str(destination) in text for text in state_warnings(self.home)))
        with self.assertRaisesRegex(ValueError, "State was migrated"):
            self.state.write_json("topology.json", {})
        self.assertFalse(self.state.path.exists())

    def test_pending_or_corrupt_migration_blocks_activity(self):
        atomic_json(self.home / ".migration-receipt.json", {"schema": 1, "phase": "pending"})
        with self.assertRaisesRegex(ValueError, "incomplete"):
            with home_lock(self.home):
                pass
        self.assertTrue(any("incomplete" in text for text in state_warnings(self.home)))
        with home_lock(self.home, exclusive=True, allow_moved=True):
            pass
        atomic_json(self.home / "MOVED.json", None)
        with self.assertRaisesRegex(ValueError, "Invalid MOVED"):
            with home_lock(self.home):
                pass

    def test_msix_warning_uses_realpath_without_mutation(self):
        virtualized = str(self.root / "Packages" / "Claude_fixture" / "LocalCache" / "Local" / "agent-relay")
        with mock.patch("agent_relay.state.os.path.realpath", return_value=virtualized), \
                mock.patch("agent_relay.state.moved_destination", return_value=None):
            self.assertTrue(any("MSIX" in text for text in state_warnings(self.home)))
        self.assertFalse(self.home.exists())


if __name__ == "__main__":
    unittest.main()
