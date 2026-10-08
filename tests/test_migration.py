import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid

from agent_relay import migration
from agent_relay.state import atomic_json, home_lock, state_warnings


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "old-state"
        self.destination = self.root / "new-state"
        self.source.mkdir()
        atomic_json(self.source / "config.json", {"agents": {"codex": {"enabled": True}}, "unicode": "한글"})

    def request(self, status="completed", collector=None):
        request_id = uuid.uuid4().hex
        data = {"id": request_id, "status": status}
        if collector is not None:
            data["collector"] = collector
        path = self.source / "projects" / ("a" * 64) / "requests" / (request_id + ".json")
        atomic_json(path, data)
        return path

    def migrate(self, **kwargs):
        return migration.migrate_state(self.source, self.destination, **kwargs)

    def test_internal_lock_inode_survives_payload_publication(self):
        with home_lock(self.destination, exclusive=True, allow_moved=True):
            path = next((self.destination / ".locks").glob("*.lock"))
            before = path.stat().st_ino
            self.migrate()
            self.assertEqual(path.stat().st_ino, before)

    def test_partial_payload_publication_can_resume(self):
        real = migration.os.rename
        def interrupt(source, target):
            if Path(source).name == "config.json":
                raise OSError("publication interrupted")
            return real(source, target)
        with mock.patch.object(migration.os, "rename", side_effect=interrupt):
            with self.assertRaisesRegex(OSError, "publication interrupted"):
                self.migrate()
        self.assertEqual(migration._json(self.destination / migration.RECEIPT)["phase"], "pending")
        self.assertEqual(self.migrate()["status"], "migrated")
        self.assertEqual((self.destination / "config.json").read_bytes(), (self.source / "config.json").read_bytes())

    def test_unknown_lead_delivery_blocks_migration(self):
        atomic_json(self.source / "projects" / ("a" * 64) / "inbox/delivered" / ("b" * 32 + ".json"),
                    {"id": "b" * 32, "status": "delivery_unknown"})
        with self.assertRaisesRegex(ValueError, "Unknown Lead delivery"):
            self.migrate()

    def test_live_inbox_watcher_blocks_migration(self):
        import time
        from agent_relay import watcher
        lease = watcher.lease_path(self.source / "projects" / ("a" * 64), "claude:lead")
        atomic_json(lease, {"recipient": "claude:lead", "pid": 1, "started": 0, "heartbeat": time.time()})
        with self.assertRaisesRegex(ValueError, "Live inbox watcher"):
            self.migrate()
        atomic_json(lease, {"recipient": "claude:lead", "pid": 1, "started": 0, "heartbeat": 0})
        self.assertEqual(self.migrate()["status"], "migrated")

    def inventory(self):
        return {str(path.relative_to(self.root)): path.read_bytes() if path.is_file() else None
                for path in self.root.rglob("*")}

    def test_copy_verifies_hashes_preserves_source_and_is_idempotent(self):
        request = self.request()
        binary = self.source / "log" / "raw.bin"
        binary.parent.mkdir()
        binary.write_bytes(bytes(range(256)))
        original = {path.relative_to(self.source): path.read_bytes() for path in (request, binary, self.source / "config.json")}
        result = self.migrate()
        self.assertEqual(result["status"], "migrated")
        self.assertEqual(result["file_count"], 3)
        for relative, value in original.items():
            self.assertEqual((self.source / relative).read_bytes(), value)
            self.assertEqual(hashlib.sha256((self.destination / relative).read_bytes()).digest(), hashlib.sha256(value).digest())
        marker = json.loads((self.source / "MOVED.json").read_text())
        self.assertEqual(marker["destination"], str(self.destination.resolve()))
        before = self.inventory()
        self.assertEqual(self.migrate()["status"], "already_migrated")
        self.assertEqual(self.inventory(), before)
        self.assertTrue(any("new state home" in warning for warning in state_warnings(self.source)))
        with self.assertRaises(ValueError):
            with home_lock(self.source):
                pass
        with home_lock(self.destination):
            pass

    def test_dry_run_is_completely_read_only_and_uses_default_destination(self):
        self.request()
        before = self.inventory()
        with mock.patch.object(migration, "state_home", return_value=self.destination):
            result = migration.migrate_state(self.source, dry_run=True)
        self.assertEqual(result["status"], "ready_copy")
        self.assertEqual(result["destination"], str(self.destination.resolve()))
        self.assertEqual(self.inventory(), before)

    def test_every_open_status_is_rejected(self):
        for status in migration.OPEN_STATES:
            with self.subTest(status=status):
                path = self.request(status)
                with self.assertRaisesRegex(ValueError, "Open request"):
                    self.migrate()
                path.unlink()
        self.assertEqual([p.name for p in self.destination.iterdir() if p.name != ".locks"] if self.destination.exists() else [], [])
        self.assertFalse((self.source / "MOVED.json").exists())

    def test_unknown_status_corrupt_json_and_ambiguous_pid_are_rejected(self):
        path = self.request("unknown")
        with self.assertRaisesRegex(ValueError, "Unknown request status"):
            self.migrate()
        path.write_text("{", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Invalid JSON"):
            self.migrate()
        path.unlink()
        path = self.request(collector={"started_utc": "unknown"})
        with self.assertRaisesRegex(ValueError, "Ambiguous collector"):
            self.migrate()
        path.unlink()
        self.request(collector={"pid": True})
        with self.assertRaisesRegex(ValueError, "Invalid recorded collector PID"):
            self.migrate()

    def test_live_collector_is_checked_with_read_only_process_api(self):
        self.request(collector={"pid": os.getpid()})
        self.assertTrue(migration.pid_alive(os.getpid()))
        with self.assertRaisesRegex(ValueError, "Live collector PID"):
            self.migrate()
        self.assertEqual([p.name for p in self.destination.iterdir() if p.name != ".locks"] if self.destination.exists() else [], [])

    def test_dead_collector_allows_copy_but_inspection_errors_reject(self):
        self.request(collector={"pid": 12345})
        with mock.patch.object(migration, "pid_alive", side_effect=ValueError("inspection denied")):
            with self.assertRaisesRegex(ValueError, "inspection denied"):
                self.migrate()
        with mock.patch.object(migration, "pid_alive", return_value=False):
            self.assertEqual(self.migrate()["dead_collector_pids"], [12345])

    def test_destination_conflict_never_overwrites(self):
        self.destination.mkdir()
        original = b"existing destination bytes"
        (self.destination / "config.json").write_bytes(original)
        with self.assertRaisesRegex(ValueError, "not empty"):
            self.migrate()
        self.assertEqual((self.destination / "config.json").read_bytes(), original)
        self.assertFalse((self.source / "MOVED.json").exists())

    def test_nested_and_same_paths_rejected_in_both_directions(self):
        for destination in (self.source, self.source / "nested", self.root):
            with self.subTest(destination=destination):
                with self.assertRaisesRegex(ValueError, "non-overlapping"):
                    migration.migrate_state(self.source, destination)

    def test_empty_or_invalid_move_marker_is_rejected_even_in_dry_run(self):
        for marker in ({}, {"schema": 1, "destination": str(self.destination)}):
            atomic_json(self.source / "MOVED.json", marker)
            with self.assertRaisesRegex(ValueError, "MOVED.json conflicts"):
                self.migrate(dry_run=True)
        self.assertEqual([p.name for p in self.destination.iterdir() if p.name != ".locks"] if self.destination.exists() else [], [])

    def test_changed_complete_receipt_fingerprint_cannot_claim_idempotent_success(self):
        self.migrate()
        path = self.destination / migration.RECEIPT
        receipt = json.loads(path.read_text(encoding="utf-8"))
        receipt["fingerprint"] = "f" * 64
        atomic_json(path, receipt)
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run):
                with self.assertRaisesRegex(ValueError, "do not match"):
                    self.migrate(dry_run=dry_run)

    def test_unrelated_application_directory_is_rejected_before_reading_files(self):
        unrelated = self.root / ".codex"
        unrelated.mkdir()
        (unrelated / "auth.json").write_bytes(b"not a relay file")
        with mock.patch.object(migration, "_snapshot") as snapshot:
            with self.assertRaisesRegex(ValueError, "does not identify"):
                migration.migrate_state(unrelated, self.destination, dry_run=True)
        snapshot.assert_not_called()

    def test_added_file_or_new_request_during_copy_rejects_publication(self):
        real = migration._copy_file
        for mutation in (lambda: (self.source / "late.bin").write_bytes(b"late"), lambda: self.request("accepted")):
            with self.subTest(mutation=mutation):
                initial = set(self.source.rglob("*"))
                triggered = False
                def changing(source, destination, expected):
                    nonlocal triggered
                    real(source, destination, expected)
                    if not triggered:
                        triggered = True
                        mutation()
                with mock.patch.object(migration, "_copy_file", side_effect=changing):
                    with self.assertRaisesRegex(ValueError, "Source changed"):
                        self.migrate()
                self.assertEqual([p.name for p in self.destination.iterdir() if p.name != ".locks"] if self.destination.exists() else [], [])
                self.assertFalse((self.source / "MOVED.json").exists())
                # Remove only newly created fixture files, leaving directory cleanup
                # to this test's TemporaryDirectory.
                for path in set(self.source.rglob("*")) - initial:
                    if path.is_file():
                        path.unlink()

    def test_copied_hash_mismatch_refuses_publication(self):
        real = migration._copy_file
        def corrupt(source, destination, expected):
            real(source, destination, expected)
            destination.write_bytes(b"corruption")
        with mock.patch.object(migration, "_copy_file", side_effect=corrupt):
            with self.assertRaisesRegex(ValueError, "hash verification failed"):
                self.migrate()
        self.assertEqual([p.name for p in self.destination.iterdir() if p.name != ".locks"] if self.destination.exists() else [], [])
        self.assertEqual(list(self.root.glob(".*.migrating-*")), [])

    def test_interrupted_after_publish_resumes_without_recopied_or_lost_state(self):
        with mock.patch.object(migration, "_write_marker", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.migrate()
        self.assertTrue(self.destination.exists())
        self.assertFalse((self.source / "MOVED.json").exists())
        with self.assertRaisesRegex(ValueError, "incomplete"):
            with home_lock(self.destination):
                pass
        before = self.inventory()
        self.assertEqual(self.migrate(dry_run=True)["status"], "ready_resume")
        self.assertEqual(self.inventory(), before)
        self.assertEqual(self.migrate()["status"], "migrated")
        self.assertEqual(self.migrate()["status"], "already_migrated")

    def test_changed_source_after_pending_copy_is_not_accepted(self):
        with mock.patch.object(migration, "_write_marker", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.migrate()
        (self.source / "new.bin").write_bytes(b"later")
        with self.assertRaisesRegex(ValueError, "conflicts with source snapshot"):
            self.migrate()
        self.assertFalse((self.source / "MOVED.json").exists())

    def test_shared_activity_lock_prevents_migration_process(self):
        script = """import sys
from agent_relay.migration import migrate_state
try:
    migrate_state(sys.argv[1], sys.argv[2], timeout=0.15)
except TimeoutError:
    print('busy')
"""
        with home_lock(self.source):
            result = subprocess.run([sys.executable, "-c", script, str(self.source), str(self.destination)],
                                    cwd=Path(__file__).resolve().parents[1], capture_output=True,
                                    text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "busy")
        self.assertEqual([p.name for p in self.destination.iterdir() if p.name != ".locks"] if self.destination.exists() else [], [])

    def test_hardlinks_and_temporary_state_are_rejected(self):
        link = self.source / "linked.bin"
        try:
            os.link(self.source / "config.json", link)
        except OSError:
            # The Windows sandbox may forbid creating hardlinks; exercise the
            # filesystem metadata branch without claiming live link coverage.
            metadata = SimpleNamespace(st_mode=0o100600, st_nlink=2, st_file_attributes=0)
            with mock.patch.object(Path, "lstat", return_value=metadata):
                with self.assertRaisesRegex(ValueError, "linked"):
                    migration._file_info(link)
        else:
            with self.assertRaisesRegex(ValueError, "linked"):
                self.migrate()
            link.unlink()
        (self.source / "unfinished.tmp").write_bytes(b"{")
        with self.assertRaisesRegex(ValueError, "Unresolved temporary"):
            self.migrate()

    def test_symlink_and_junction_entries_are_refused(self):
        link = self.source / "linked-directory"
        try:
            link.symlink_to(self.root, target_is_directory=True)
        except OSError:
            self.skipTest("symbolic link creation unavailable")
        try:
            with self.assertRaisesRegex(ValueError, "links and junctions"):
                self.migrate()
        finally:
            link.unlink()


if __name__ == "__main__":
    unittest.main()
