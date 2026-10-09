"""Automatic inbox publication after send --no-wait, without a waiting Lead."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from handback import cli, collector, envelope, inbox
from handback.adapters import codex
from handback.state import ProjectState


def event(kind, **fields):
    return {"type": "event_msg", "payload": {"type": kind, **fields}}


def user(text, turn=None):
    record = {"type": "event_msg", "payload": {"type": "user_message", "message": text}}
    if turn:
        record["payload"]["turn_id"] = turn
    return record


def append(path, *records):
    with Path(path).open("ab") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False).encode("utf-8") + b"\r\n")


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.root = base / "project"
        self.root.mkdir()
        self.home = base / "state"
        self.codex_home = base / "codex"
        env = patch.dict(os.environ, {"HANDBACK_HOME": str(self.home), "CODEX_HOME": str(self.codex_home)})
        env.start()
        self.addCleanup(env.stop)
        for key in ("HANDBACK_LEAD", "HANDBACK_WORKERS"):
            os.environ.pop(key, None)
        self.state = ProjectState(self.root)

    def rollout(self, thread):
        path = self.codex_home / "sessions" / "2026" / "10" / "07" / f"rollout-x-{thread}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        return path

    def accepted(self, thread="t1", return_to="claude:lead"):
        request_id = os.urandom(16).hex()
        outgoing = inbox.put(self.state.path / "inbox", envelope.make(
            request_id, return_to, "codex:" + thread, "[relay x] work", kind="request"))
        request = {"schema": 1, "id": request_id, "marker": "[relay " + request_id[:8] + "]",
                   "thread": thread, "handle": "codex:" + thread, "agent": "codex",
                   "return_to": return_to, "outgoing_id": outgoing["id"],
                   "created_utc": "2026-10-07T00:00:00+00:00", "status": "accepted", "hop": 0,
                   "root": str(self.state.root)}
        self.state.save_request(request)
        return request


class IncrementalRolloutTests(Base):
    def test_poll_reads_only_appended_records_and_waits_for_task_complete(self):
        request = self.accepted()
        path = self.rollout("t1")
        follower = codex.CodexAdapter().incremental_collector(request)
        self.assertIsNone(follower.poll())
        append(path, event("task_started", turn_id="u1"), user(request["marker"] + " 한글", "u1"))
        self.assertIsNone(follower.poll())
        offset = follower.tailer.offset
        append(path, event("agent_message", message="중간"))
        self.assertIsNone(follower.poll())
        self.assertGreater(follower.tailer.offset, offset)
        append(path, event("task_complete", turn_id="u1", last_agent_message="최종 답"))
        self.assertEqual(follower.poll()["text"], "최종 답")


class ExecutionRecoveryTests(Base):
    def test_unknown_delivery_confirmed_completion_persists_execution_once(self):
        request = self.accepted()
        self.state.register_thread({"handle": request["handle"], "agent": "codex", "name": "worker",
                                    "execution": {"model": "old", "reasoning_effort": "high"}})
        request.update(status="delivery_unknown", execution={"model": "selected", "reasoning_effort": "ultra"})
        self.state.save_request(request)
        completed = collector.finish_request(self.state, request, {"outcome": "completed", "text": "confirmed"})
        self.assertEqual(self.state.threads()[request["handle"]]["execution"], request["execution"])
        self.assertEqual(self.state.threads()[request["handle"]]["name"], "worker")
        later = {"model": "later", "reasoning_effort": "low"}
        self.state.register_thread({"handle": request["handle"], "execution": later})
        collector.finish_request(self.state, completed, {"outcome": "completed", "text": "replayed"})
        self.assertEqual(self.state.threads()[request["handle"]]["execution"], later)

    def test_unknown_delivery_timeout_does_not_change_execution(self):
        request = self.accepted()
        original = {"model": "old", "reasoning_effort": "high"}
        self.state.register_thread({"handle": request["handle"], "execution": original})
        request.update(status="delivery_unknown", execution={"model": "selected", "reasoning_effort": "ultra"})
        self.state.save_request(request)
        collector.finish_request(self.state, request, {"outcome": "timeout"})
        self.assertEqual(self.state.threads()[request["handle"]]["execution"], original)

    def test_correlated_failed_turn_confirms_execution_selection(self):
        request = self.accepted()
        request.update(status="delivery_unknown", execution={"model": "selected", "reasoning_effort": "ultra"})
        self.state.save_request(request)
        collector.finish_request(self.state, request, {"outcome": "failed", "text": "aborted marked turn"})
        self.assertEqual(self.state.threads()[request["handle"]]["execution"], request["execution"])


class WatchCollectorTests(Base):
    def watch_once(self, recipient="claude:lead", **kwargs):
        poll = collector.WatchCollector(self.state, recipient, lambda agent: codex.CodexAdapter(),
                                        log=self.state.path / "log" / "w.log").poll
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            inbox.watch(self.state.path / "inbox", recipient, once=True, before_scan=poll, **kwargs)
        return [json.loads(line) for line in out.getvalue().splitlines()]

    def test_watch_publishes_and_prints_confirmed_result_once(self):
        request = self.accepted()
        append(self.rollout("t1"), event("task_started", turn_id="u1"), user(request["marker"], "u1"),
               event("task_complete", turn_id="u1", last_agent_message="결과"))
        events = self.watch_once()
        self.assertEqual([(e["kind"], e["body"]) for e in events], [("result", "결과")])
        self.assertEqual(self.state.load_request(request["id"])["status"], "completed")
        # A second watcher replays it until acknowledged, but never duplicates the mail.
        self.assertEqual(len(self.watch_once()), 1)
        inbox.acknowledge(self.state.path / "inbox", events[0]["id"], "claude:lead")
        self.assertEqual(self.watch_once(), [])

    def test_unfinished_turn_and_other_recipient_publish_nothing(self):
        request = self.accepted()
        other = self.accepted(thread="t2", return_to="claude:other")
        append(self.rollout("t1"), event("task_started", turn_id="u1"), user(request["marker"], "u1"))
        append(self.rollout("t2"), event("task_started", turn_id="u2"), user(other["marker"], "u2"),
               event("task_complete", turn_id="u2", last_agent_message="남의 답"))
        self.assertEqual(self.watch_once(), [])
        self.assertEqual(self.state.load_request(request["id"])["status"], "accepted")
        self.assertEqual(self.state.load_request(other["id"])["status"], "accepted")

    def test_damaged_follower_is_logged_and_watch_continues(self):
        self.accepted()
        broken = collector.WatchCollector(self.state, "claude:lead",
                                          lambda agent: (_ for _ in ()).throw(RuntimeError("boom")),
                                          log=self.state.path / "log" / "w.log")
        broken.poll()
        self.assertIn("boom", (self.state.path / "log" / "w.log").read_text(encoding="utf-8"))


class SendSpawnTests(Base):
    class Fake:
        def deliver(self, thread, mail):
            return {"accepted": True, "returncode": 0}

    def invoke(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main([*args, "--root", str(self.root)])
        return code, out.getvalue(), err.getvalue()

    def test_no_wait_starts_collector_unless_disabled(self):
        with patch.object(cli, "get_adapter", return_value=self.Fake()), \
                patch.object(collector, "spawn", return_value={"pid": 4242}) as spawn:
            code, raw, err = self.invoke("send", "--to", "codex:a", "--text", "work", "--no-wait")
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(raw)["collector_pid"], 4242)
            self.assertEqual(spawn.call_args.args[1]["handle"], "codex:a")
            code, raw, err = self.invoke("send", "--to", "codex:b", "--text", "work", "--no-wait", "--no-collect")
            self.assertEqual((code, json.loads(raw)["collector_pid"]), (0, None))
            self.assertEqual(spawn.call_count, 1)

    def test_collector_start_failure_keeps_accepted_request(self):
        with patch.object(cli, "get_adapter", return_value=self.Fake()), \
                patch.object(collector, "spawn", side_effect=OSError("denied")):
            code, _, err = self.invoke("send", "--to", "codex:a", "--text", "work", "--no-wait")
        self.assertEqual(code, 0)
        self.assertIn("Collector not started", err)
        self.assertEqual(self.state.requests()[0]["status"], "accepted")

    def test_detached_collector_process_publishes_after_completion(self):
        request = self.accepted()
        path = self.rollout("t1")
        children = []
        popen = collector.subprocess.Popen

        def stop(child):
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)

        def start(*args, **kwargs):
            child = popen(*args, **kwargs)
            children.append(child)
            # Reap this exact child before the temporary directory even if spawn
            # fails to publish its PID or an assertion fails below.
            self.addCleanup(stop, child)
            return child

        with patch.object(collector.subprocess, "Popen", side_effect=start):
            info = collector.spawn(self.state, request, timeout=60)
        self.assertEqual(len(children), 1)
        self.assertEqual(self.state.load_request(request["id"])["collector"]["pid"], info["pid"])
        time.sleep(1)
        self.assertEqual(inbox.pending(self.state.path / "inbox", "claude:lead"), [])
        append(path, event("task_started", turn_id="u1"), user(request["marker"], "u1"),
               event("task_complete", turn_id="u1", last_agent_message="분리 수집"))
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not inbox.pending(self.state.path / "inbox", "claude:lead"):
            time.sleep(0.2)
        mail = inbox.pending(self.state.path / "inbox", "claude:lead")
        self.assertEqual([m["body"] for m in mail], ["분리 수집"], Path(info["log"]).read_text(encoding="utf-8"))
        # The exit log is flushed before interpreter shutdown closes stdout.
        self.assertEqual(children[0].wait(timeout=10), 0, Path(info["log"]).read_text(encoding="utf-8"))
        self.assertIn("exit=0", Path(info["log"]).read_text(encoding="utf-8"))

    def test_pid_recorded_when_watcher_finishes_before_child_registration(self):
        request = self.accepted()

        def start(*args, **kwargs):
            self.assertTrue(self.state.load_request(request['id'])['collector']['starting'])
            collector.finish_request(self.state, request, {'outcome': 'completed', 'text': 'watch won'})
            return SimpleNamespace(pid=os.getpid())

        with patch.object(collector.subprocess, 'Popen', side_effect=start):
            collector.spawn(self.state, request)
        current = self.state.load_request(request['id'])
        self.assertEqual(current['status'], 'completed')
        self.assertEqual(current['collector']['pid'], os.getpid())
        from handback.migration import migrate_state
        with self.assertRaisesRegex(ValueError, 'Live collector PID'):
            migrate_state(self.home, self.home.parent / 'migrated', dry_run=True)

    def test_failed_pid_publication_leaves_migration_blocking_intent(self):
        request = self.accepted()
        save = self.state.save_request

        def fail_pid(value):
            if value.get('collector', {}).get('pid'):
                raise OSError('simulated PID write failure')
            return save(value)

        with patch.object(collector.subprocess, 'Popen', return_value=SimpleNamespace(pid=os.getpid())), \
                patch.object(self.state, 'save_request', side_effect=fail_pid):
            with self.assertRaisesRegex(OSError, 'PID write failure'):
                collector.spawn(self.state, request)
        collector.finish_request(self.state, request, {'outcome': 'completed', 'text': 'done'})
        from handback.migration import migrate_state
        with self.assertRaisesRegex(ValueError, 'Ambiguous collector'):
            migrate_state(self.home, self.home.parent / 'migrated', dry_run=True)

    def test_definite_spawn_failure_restores_prior_collector_record(self):
        request = self.accepted()
        with patch.object(collector.subprocess, 'Popen', side_effect=OSError('cannot spawn')):
            with self.assertRaisesRegex(OSError, 'could not start collector'):
                collector.spawn(self.state, request)
        self.assertNotIn('collector', self.state.load_request(request['id']))


if __name__ == "__main__":
    unittest.main()

class RetryTests(Base):
    def test_exponential_cap_silent_success_and_status_reset(self):
        request = self.accepted()
        now = [0]
        calls = []
        fail = [True]
        class Follower:
            def poll(inner):
                calls.append(now[0])
                if fail[0]:
                    raise RuntimeError('same')
        adapter = SimpleNamespace(incremental_collector=lambda r: Follower())
        log = self.state.path / 'log' / 'retry.log'
        watch = collector.WatchCollector(self.state, 'claude:lead', lambda a: adapter,
                                         log=log, clock=lambda: now[0])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            for second in range(2000):
                now[0] = second
                watch.poll()
        self.assertEqual(calls[:9], [0, 2, 6, 14, 30, 62, 126, 254, 510])
        self.assertEqual(calls[9] - calls[8], 300)
        self.assertEqual(len(log.read_text().splitlines()), 9)
        self.assertEqual(out.getvalue(), '')
        request['status'] = 'delivery_unknown'
        self.state.save_request(request)
        now[0] = 2000
        watch.poll()
        self.assertEqual(watch.retries[request['id']]['delay'], 2)
        fail[0] = False
        now[0] = 2002
        watch.poll()
        self.assertNotIn(request['id'], watch.retries)
        fail[0] = True
        now[0] = 2003
        watch.poll()
        self.assertEqual(watch.retries[request['id']]['delay'], 2)

    def test_state_read_errors_are_also_throttled(self):
        now = [0]
        watch = collector.WatchCollector(self.state, 'claude:lead', lambda a: None, clock=lambda: now[0])
        with patch.object(self.state, 'requests', side_effect=ValueError('broken')) as read:
            watch.poll()
            watch.poll()
            self.assertEqual(read.call_count, 1)
            now[0] = 2
            watch.poll()
            self.assertEqual(read.call_count, 2)

class FinishRetryTests(Base):
    def test_publication_failure_keeps_failure_count(self):
        self.accepted()
        now = [0]
        follower = SimpleNamespace(poll=lambda: {'outcome': 'completed', 'text': 'reply'})
        adapter = SimpleNamespace(incremental_collector=lambda r: follower)
        watch = collector.WatchCollector(self.state, 'claude:lead', lambda a: adapter, clock=lambda: now[0])
        with patch.object(collector, 'finish_request', side_effect=ValueError('ACK mismatch')) as finish:
            for second in range(15):
                now[0] = second
                watch.poll()
            self.assertEqual(finish.call_count, 4)
            self.assertEqual(next(iter(watch.retries.values()))['delay'], 16)
