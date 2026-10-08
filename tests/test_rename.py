import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from handback import cli, config, hook_install, skill_install, state, state_transition
from handback.adapters import antigravity


class RenameTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_legacy_environment_warns_without_selecting_it(self):
        with patch.dict(os.environ, {'HANDBACK_HOME': str(self.root / 'new'),
                                    'AGENT_RELAY_HOME': str(self.root / 'old')}):
            self.assertEqual(state.state_home(), self.root / 'new')
            warnings = state_transition.legacy_warnings()
            self.assertTrue(any('handback migrate-state --from' in w and str(self.root / 'old') in w for w in warnings))
            self.assertFalse((self.root / 'new').exists())

    def test_platform_defaults_ignore_legacy_environment(self):
        env = {'USERPROFILE': str(self.root), 'XDG_STATE_HOME': str(self.root / 'xdg'),
               'AGENT_RELAY_HOME': str(self.root / 'old')}
        with patch.dict(os.environ, env, clear=True), patch.object(Path, 'home', return_value=self.root):
            for platform, expected in [('win32', self.root / '.handback'),
                                       ('darwin', self.root / 'Library/Application Support/handback'),
                                       ('linux', self.root / 'xdg/handback')]:
                with patch.object(state.sys, 'platform', platform):
                    self.assertEqual(state.state_home(), expected)

    def test_old_default_discovery_all_platforms(self):
        with patch.dict(os.environ, {'USERPROFILE': str(self.root), 'XDG_STATE_HOME': str(self.root / 'xdg')}, clear=True), \
                patch.object(Path, 'home', return_value=self.root):
            for platform, source in [('win32', self.root / '.agent-relay'),
                                     ('darwin', self.root / 'Library/Application Support/agent-relay'),
                                     ('linux', self.root / 'xdg/agent-relay')]:
                (source / 'projects').mkdir(parents=True)
                with patch.object(state.sys, 'platform', platform):
                    self.assertIn(source.resolve(), state_transition.legacy_homes())
                    self.assertTrue(state_transition.legacy_warnings())

    def test_migrate_cli_from_preserves_original_bytes(self):
        source, target = self.root / '.agent-relay', self.root / '.handback'
        source.mkdir()
        original = b'{"lead":"claude"}\n'
        (source / 'config.json').write_bytes(original)
        with patch.dict(os.environ, {'HANDBACK_HOME': str(target)}), \
                contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(['migrate-state', '--from', str(source)]), 0)
        self.assertTrue(json.loads(out.getvalue())['source_preserved'])
        self.assertEqual((source / 'config.json').read_bytes(), original)
        self.assertEqual((target / 'config.json').read_bytes(), original)
        self.assertTrue((source / 'MOVED.json').exists())

    def test_chained_moved_stores_do_not_warn_or_block(self):
        msix = self.root / 'AppData/Local/Packages/Claude_x/LocalCache/Local/agent-relay'
        old, target = self.root / '.agent-relay', self.root / '.handback'
        msix.mkdir(parents=True)
        (msix / 'projects').mkdir()
        (msix / 'config.json').write_text('{}', encoding='utf-8')
        quiet = contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO())
        with patch.dict(os.environ, {'HANDBACK_HOME': str(old)}), quiet[0], quiet[1]:
            self.assertEqual(cli.main(['migrate-state', '--from', str(msix)]), 0)
        with patch.dict(os.environ, {'HANDBACK_HOME': str(target)}), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(['migrate-state', '--from', str(old)]), 0)
        with patch.dict(os.environ, {'USERPROFILE': str(self.root)}, clear=True), \
                patch.object(state.sys, 'platform', 'win32'):
            self.assertEqual(state.state_home(), target)
            self.assertEqual(state_transition.legacy_homes(), [])
            state_transition.require_migrated_default()

    def test_legacy_project_policy_warns_and_current_wins(self):
        legacy = self.root / '.agent-relay.json'
        legacy.write_text('{"allowed_agents":["claude"]}')
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(config.project_config_path(self.root), legacy)
        self.assertIn('WARN', err.getvalue())
        current = self.root / '.handback.json'
        current.write_text('{}')
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(config.project_config_path(self.root), current)

    def test_legacy_hooks_without_manifest_replace_or_uninstall(self):
        for agent in ('claude', 'codex'):
            for entry in (['-m', 'agent_relay'], [str(self.root / 'agent_relay.py')]):
                for action in ('install', 'uninstall'):
                    with self.subTest(agent=agent, entry=entry, action=action):
                        path = self.root / 'hooks.json'
                        event = 'UserPromptSubmit'
                        args = ['-X', 'utf8', *entry, 'hook', '--agent', agent, '--event', event,
                                '--state-home', str(self.root / 'old')]
                        handler = {'type': 'command', 'command': 'python', 'args': args}
                        if agent == 'codex':
                            import shlex
                            handler = {'type': 'command', 'command': shlex.join(['python', *args])}
                        group = {'hooks': [handler]}
                        path.write_text(json.dumps({'hooks': {event: [group]}, 'keep': 1}))
                        manifest = {'schema': 1, 'installations': {}}
                        _, plans, changes, _ = hook_install._prepare(action, [agent], None,
                            self.root / 'new', {agent: path}, manifest)
                        self.assertEqual(changes[0]['removed'][0]['group'], group)
                        self.assertEqual(json.loads(plans[0]['after'])['keep'], 1)

    def test_legacy_antigravity_module_and_script(self):
        for entry in ('-m agent_relay', 'C:/neutral/agent_relay.py'):
            group = {'Stop': [{'type': 'command', 'command':
                     'python ' + entry + ' hook --agent antigravity --event Stop --state-home C:/neutral/state'}]}
            value = {'agent-relay': group, 'unrelated': {}}
            self.assertTrue(antigravity._remove_legacy_hooks(value))
            self.assertEqual(value, {'unrelated': {}})

    def test_legacy_skill_backup_and_dry_run(self):
        legacy = self.root / '.claude/skills/agent-relay/SKILL.md'
        legacy.parent.mkdir(parents=True)
        legacy.write_bytes(b'legacy original')
        skill_install.install(self.root, dry_run=True)
        self.assertEqual(legacy.read_bytes(), b'legacy original')
        skill_install.install(self.root)
        self.assertFalse(legacy.exists())
        self.assertEqual(next(legacy.parent.glob('*.bak')).read_bytes(), b'legacy original')
        self.assertIn('name: handback', (legacy.parent.parent / 'handback/SKILL.md').read_text())


    def test_result_message_id_namespace_is_unchanged(self):
        import uuid
        from handback import collector
        from handback.state import ProjectState
        request_id = "a" * 32
        project = ProjectState(self.root, home=self.root / "state")
        request = {"id": request_id, "status": "accepted", "handle": "codex:synthetic",
                   "return_to": "claude:synthetic", "root": str(self.root)}
        from handback import envelope, inbox
        outgoing = envelope.make(request_id, request["return_to"], request["handle"], "work")
        inbox.put(project.path / "inbox", outgoing)
        request.update(outgoing_id=outgoing["id"], created_utc=outgoing["created_utc"])
        project.save_request(request)
        collector._finish_request(project, request, {"text": "done", "outcome": "completed"})
        saved = project.load_request(request_id)
        self.assertEqual(saved["reply_id"], uuid.uuid5(uuid.NAMESPACE_URL, "agent-relay:" + request_id).hex)
