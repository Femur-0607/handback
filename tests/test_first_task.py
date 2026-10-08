import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from handback import cli, config, envelope, inbox
from handback.state import ProjectState
from tests.test_cli import FakeAdapter


class FirstTaskTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / 'project'
        self.root.mkdir()
        env = patch.dict(os.environ, {'HANDBACK_HOME': str(Path(temp.name) / 'state')})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop('HANDBACK_LEAD', None)
        os.environ.pop('HANDBACK_WORKERS', None)
        self.state = ProjectState(self.root)
        self.adapter = FakeAdapter()
        self.adapter.result = {'outcome': 'completed', 'text': 'RELAY_OK'}
        self.adapter.detect = lambda: {'installed': True}
        for mock in (patch.object(cli, 'get_adapter', return_value=self.adapter),
                     patch('handback.router.sandboxed', return_value=False),
                     patch('handback.router.route', return_value=None)):
            mock.start()
            self.addCleanup(mock.stop)

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main([*args, '--root', str(self.root)])
        return code, out.getvalue(), err.getvalue()

    def test_try_success(self):
        with patch.object(self.adapter, 'new_thread', wraps=self.adapter.new_thread) as create:
            code, raw, err = self.run_cli('try', '--json')
        self.assertEqual(code, 0, err)
        result = json.loads(raw)
        self.assertTrue(result['acknowledged'])
        self.assertEqual(create.call_args.args[2], 'read-only')
        self.assertEqual(len(self.adapter.deliveries), 1)
        self.assertEqual(inbox.pending(self.state.path / 'inbox', 'claude:lead'), [])
        self.assertEqual(result['message_id'], self.state.load_request(result['request_id'])['reply_id'])

    def test_try_keep_and_custom_lead(self):
        code, raw, _ = self.run_cli('try', '--json', '--keep', '--lead', 'claude:custom')
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(raw)['acknowledged'])
        self.assertEqual(len(inbox.pending(self.state.path / 'inbox', 'claude:custom')), 1)

    def test_missing_codex(self):
        self.adapter.detect = lambda: {'installed': False}
        code, raw, _ = self.run_cli('try', '--json')
        self.assertEqual(code, 5)
        self.assertIn('HANDBACK_CODEX', json.loads(raw)['fix'])
        self.assertFalse(self.adapter.deliveries)
        self.assertFalse(self.state.read_json('topology.json', {}))

    def test_existing_topology_reused(self):
        config.use_topology(self.root, 'claude:existing', ['codex'])
        before = self.state.read_json('topology.json', {})
        self.assertEqual(self.run_cli('try')[0], 0)
        self.assertEqual(before, self.state.read_json('topology.json', {}))
        self.assertEqual(self.state.requests()[0]['return_to'], 'claude:existing')

    def test_existing_topology_refused(self):
        before = {'lead': 'codex:existing', 'workers': ['antigravity'], 'fallback': 'ask'}
        self.state.write_json('topology.json', before)
        code, raw, _ = self.run_cli('try', '--json')
        self.assertEqual(code, 5)
        self.assertIn('handback use --root', json.loads(raw)['fix'])
        self.assertEqual(before, self.state.read_json('topology.json', {}))
        self.assertFalse(self.adapter.deliveries)

    def test_timeout_and_unknown_never_resubmit(self):
        for unknown in (False, True):
            with self.subTest(unknown=unknown):
                self.adapter.result = {'outcome': 'timeout'}
                self.adapter.delivery = {'accepted': False, 'unknown': True} if unknown else {'accepted': True}
                # Separate worker identity for each open request.
                self.adapter.new_thread = lambda *a, **k: 'unknown' if unknown else 'timeout'
                code, raw, _ = self.run_cli('try', '--timeout', '1')
                self.assertEqual(code, 4 if unknown else 3)
                self.assertIn('Recover (do not resubmit): handback wait --root', raw)
                self.assertIn('--request', raw)
        self.assertEqual(len(self.adapter.deliveries), 2)
        self.assertEqual(len(self.state.requests()), 2)

    def test_wrong_reply_not_acked(self):
        self.adapter.result['text'] = 'wrong'
        code, _, _ = self.run_cli('try')
        self.assertEqual(code, 2)
        self.assertEqual(len(inbox.pending(self.state.path / 'inbox', 'claude:lead')), 1)

    def test_identity_mismatch_not_acked(self):
        original = cli.inbox._read_json
        def read(path):
            value = original(path)
            if value.get('kind') == 'result':
                value['request_id'] = 'different'
            return value
        with patch.object(cli.inbox, '_read_json', side_effect=read):
            self.assertEqual(self.run_cli('try')[0], 2)
        self.assertEqual(len(inbox.pending(self.state.path / 'inbox', 'claude:lead')), 1)

    def test_request_ack_and_list(self):
        path = self.state.path / 'inbox'
        messages = [envelope.make('request-a', 'codex:w', 'claude:lead', 'answer', kind=k)
                    for k in ('result', 'error', 'progress')]
        messages += [envelope.make('request-b', 'codex:w', 'claude:lead', 'other'),
                     envelope.make('request-a', 'codex:w', 'claude:other', 'other recipient')]
        for mail in messages:
            inbox.put(path, mail)
        code, raw, _ = self.run_cli('inbox', 'list', '--for', 'claude:lead', '--request', 'request-a')
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(raw)), 3)
        code, raw, _ = self.run_cli('inbox', 'ack', '--for', 'claude:lead', '--request', 'request-a')
        self.assertEqual(code, 0)
        self.assertEqual({x['id'] for x in json.loads(raw)['acknowledged']}, {m['id'] for m in messages[:2]})
        self.assertEqual(len(inbox.pending(path)), 3)
        self.assertEqual(self.run_cli('inbox', 'ack', '--for', 'claude:lead', '--request', 'request-a')[0], 0)

    def test_ack_no_result(self):
        inbox.put(self.state.path / 'inbox', envelope.make('r', 'claude:lead', 'codex:w', 'task', kind='request'))
        code, _, err = self.run_cli('inbox', 'ack', '--for', 'codex:w', '--request', 'r')
        self.assertEqual(code, 5)
        self.assertIn('no result/error', err)

    def test_ack_both_flags_rejected(self):
        with self.assertRaises(SystemExit) as error:
            self.run_cli('inbox', 'ack', '--for', 'claude:lead', '--request', 'r', '--id', 'a' * 32)
        self.assertEqual(error.exception.code, 2)

    def test_wait_and_send_message_id(self):
        code, raw, err = self.run_cli('send', '--to', 'codex:w', '--text', 'reply')
        self.assertEqual(code, 0)
        req = self.state.requests()[0]
        self.assertIn(req['reply_id'], err)
        code, raw, err = self.run_cli('wait', '--request', req['id'])
        self.assertEqual(code, 0)
        self.assertEqual(raw.strip(), 'RELAY_OK')
        self.assertIn(req['reply_id'], err)

    def test_combination_error(self):
        code, _, err = self.run_cli('use', '--lead', 'codex:real', '--workers', 'codex')
        self.assertEqual(code, 5)
        self.assertIn('sandbox queue DB', err)
        self.assertIn(config.SUPPORTED_COMBINATIONS, err)

    def test_antigravity_warning(self):
        self.state.home.mkdir(parents=True, exist_ok=True)
        (self.state.home / 'config.json').write_text(json.dumps({'agents': {'antigravity': {'enabled': True}}}))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(['new', '--cwd', str(self.root), '--name', 'readonly', '--worker', 'antigravity', '--sandbox', 'read-only'])
        self.assertEqual(code, 0)
        self.assertIn('cannot enforce read-only', err.getvalue())
        code, _, err = self.run_cli('send', '--to', 'antigravity:w', '--text', 'Read-only review')
        self.assertEqual(code, 0)
        self.assertIn("brief's instruction", err)

    def test_collection_exception_preserves_recovery(self):
        with patch.object(self.adapter, 'fallback_collect', side_effect=OSError('collection unavailable')):
            code, raw, _ = self.run_cli('try', '--json')
        self.assertEqual(code, 4)
        self.assertIn(json.loads(raw)['request_id'], json.loads(raw)['recovery'])
        self.assertEqual(len(self.adapter.deliveries), 1)

    def test_environment_conflict_does_not_write_topology(self):
        with patch.dict(os.environ, {'HANDBACK_LEAD': 'claude:other'}):
            self.assertEqual(self.run_cli('try')[0], 5)
        self.assertFalse(self.state.read_json('topology.json', {}))
        self.assertFalse(self.adapter.deliveries)

    def test_worker_failure_keeps_error(self):
        self.adapter.result = {'outcome': 'failed', 'error': 'worker stopped'}
        code, raw, _ = self.run_cli('try', '--json')
        self.assertEqual(code, 2)
        self.assertFalse(json.loads(raw)['acknowledged'])
        self.assertEqual(inbox.pending(self.state.path / 'inbox', 'claude:lead')[0]['kind'], 'error')

    def test_try_sandbox_precondition(self):
        with patch('handback.router.sandboxed', return_value=True):
            code, raw, _ = self.run_cli('try', '--json')
        self.assertEqual(code, 5)
        self.assertIn('normal terminal', json.loads(raw)['error'])
        self.assertFalse(self.adapter.deliveries)

    def test_new_unsupported_combination(self):
        self.state.write_json('topology.json', {'lead': 'codex:real', 'workers': ['codex'], 'fallback': 'ask'})
        with contextlib.redirect_stderr(io.StringIO()) as err:
            code = cli.main(['new', '--cwd', str(self.root), '--name', 'bad', '--worker', 'codex'])
        self.assertEqual(code, 5)
        self.assertIn(config.SUPPORTED_COMBINATIONS, err.getvalue())
        self.assertFalse(self.adapter.deliveries)

    def test_warning_for_file_brief(self):
        self.state.home.mkdir(parents=True, exist_ok=True)
        (self.state.home / 'config.json').write_text(json.dumps({'agents': {'antigravity': {'enabled': True}}}))
        brief = self.root / 'brief.txt'
        brief.write_text('Do not modify files.', encoding='utf-8')
        code, _, err = self.run_cli('send', '--to', 'antigravity:w', '--file', str(brief))
        self.assertEqual(code, 0)
        self.assertIn('cannot enforce read-only', err)

    def test_warning_does_not_require_brief_read_access(self):
        self.state.home.mkdir(parents=True, exist_ok=True)
        (self.state.home / 'config.json').write_text(json.dumps({'agents': {'antigravity': {'enabled': True}}}))
        brief = self.root / 'brief.txt'
        brief.write_text('read-only', encoding='utf-8')
        original = Path.read_text
        def read(path, *args, **kwargs):
            if path == brief:
                raise PermissionError('worker can read this path')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'read_text', read):
            code, _, err = self.run_cli('send', '--to', 'antigravity:w', '--file', str(brief))
        self.assertEqual(code, 0)
        self.assertIn('cannot enforce read-only', err)
