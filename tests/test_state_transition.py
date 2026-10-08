import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_relay import state_transition as transition


class TransitionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.profile = Path(self.temp.name)
        self.destination = self.profile / '.agent-relay'
        self.previous = self.profile / 'AppData/Local/Packages/Claude_test/LocalCache/Local/agent-relay'
        self.previous.mkdir(parents=True)
        (self.previous / 'projects').mkdir()
        env = patch.dict(os.environ, {'USERPROFILE': str(self.profile)})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop('AGENT_RELAY_HOME', None)
        platform = patch.object(transition.sys, 'platform', 'win32')
        platform.start()
        self.addCleanup(platform.stop)
        home = patch.object(transition, 'state_home', return_value=self.destination)
        home.start()
        self.addCleanup(home.stop)

    def test_legacy_discovery_is_read_only_and_blocks_silent_split(self):
        self.assertEqual(transition.legacy_homes(), [self.previous.resolve()])
        with self.assertRaisesRegex(ValueError, 'refusing to split'):
            transition.require_migrated_default()
        self.assertFalse(self.destination.exists())
        self.assertIn(str(self.previous), transition.legacy_warnings()[0])

    def test_explicit_home_allows_existing_lead_to_finish(self):
        with patch.dict(os.environ, {'AGENT_RELAY_HOME': str(self.previous)}):
            transition.require_migrated_default()
            self.assertEqual(transition.legacy_warnings(), [])
        transition.require_migrated_default(explicit_home=str(self.previous))

    def test_retained_moved_source_does_not_block_destination(self):
        from agent_relay.migration import migrate_state
        migrate_state(self.previous, destination=self.destination)
        self.assertEqual(transition.legacy_homes(), [])
        transition.require_migrated_default()

    def test_invalid_marker_or_different_destination_cannot_bypass_guard(self):
        marker = self.previous / 'MOVED.json'
        for value in ('broken', json.dumps({'destination': str(self.destination)}),
                      json.dumps({'schema': 1, 'destination': str(self.profile / 'different')})):
            marker.write_text(value, encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'refusing to split'):
                transition.require_migrated_default()

    def test_valid_move_to_another_home_still_needs_explicit_selection(self):
        from agent_relay.migration import migrate_state
        migrate_state(self.previous, destination=self.profile / 'different')
        with self.assertRaisesRegex(ValueError, 'refusing to split'):
            transition.require_migrated_default()

    def test_other_platforms_are_unchanged(self):
        with patch.object(transition.sys, 'platform', 'linux'):
            self.assertEqual(transition.legacy_homes(), [])

    def test_empty_previous_directory_is_not_state(self):
        (self.previous / 'projects').rmdir()
        self.assertEqual(transition.legacy_homes(), [])


class CliTransitionTests(unittest.TestCase):
    def test_default_guard_rejects_send_before_adapter_or_state_creation(self):
        from agent_relay import cli
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmp:
            home = Path(tmp) / 'new-state'
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch.dict(os.environ, {'AGENT_RELAY_HOME': str(home)}), \
                    patch.object(cli, 'require_migrated_default', side_effect=ValueError('migration required')), \
                    patch.object(cli, 'get_adapter') as adapter, \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = cli.main(['send', '--root', tmp, '--to', 'codex:fake', '--text', 'work'])
            self.assertEqual(code, 4)
            self.assertIn('migration required', stderr.getvalue())
            adapter.assert_not_called()
            self.assertFalse(home.exists())

    def test_status_reports_moved_path_without_creating_new_files(self):
        from agent_relay import cli
        from agent_relay.migration import migrate_state
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmp:
            root = Path(tmp)
            source, destination = root / 'old', root / 'new'
            source.mkdir()
            (source / 'config.json').write_text('{}', encoding='utf-8')
            migrate_state(source, destination=destination)
            before = {str(path.relative_to(source)) for path in source.rglob('*')}
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch.dict(os.environ, {'AGENT_RELAY_HOME': str(source)}), \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = cli.main(['status', '--root', tmp])
            self.assertEqual(code, 0, stderr.getvalue())
            self.assertIn(str(destination), stderr.getvalue())
            self.assertTrue(json.loads(stdout.getvalue())['warnings'])
            self.assertEqual(before, {str(path.relative_to(source)) for path in source.rglob('*')})

    def test_unmanaged_hook_creates_no_lock_or_state(self):
        from agent_relay import cli
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmp:
            home = Path(tmp) / 'state'
            with patch.dict(os.environ, {'AGENT_RELAY_HOME': str(home)}), \
                    patch('sys.stdin', io.StringIO('{"session_id":"unmanaged"}')), \
                    contextlib.redirect_stdout(io.StringIO()) as stdout, \
                    contextlib.redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(cli.main(['hook', '--agent', 'claude', '--event', 'SessionStart']), 0)
            self.assertEqual(stdout.getvalue(), '')
            self.assertEqual(stderr.getvalue(), '')
            self.assertEqual(list(Path(tmp).iterdir()), [])
