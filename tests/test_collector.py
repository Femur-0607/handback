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
from handback.adapters import antigravity, codex
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

    def accepted(self, thread="t1", return_to="claude:lead", agent="codex"):
        request_id = os.urandom(16).hex()
        outgoing = inbox.put(self.state.path / "inbox", envelope.make(
            request_id, return_to, agent + ":" + thread, "[relay x] work", kind="request"))
        request = {"schema": 1, "id": request_id, "marker": "[relay " + request_id[:8] + "]",
                   "thread": thread, "handle": agent + ":" + thread, "agent": agent,
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
    def setUp(self):
        super().setUp()
        self.now = [0]
        self.adapter = codex.CodexAdapter()

    def completed_rollout(self, thread="t1"):
        request = self.accepted(thread=thread)
        path = self.rollout(thread)
        append(path, event("task_started", turn_id="u1"), user(request["marker"], "u1"),
               event("task_complete", turn_id="u1", last_agent_message="저장할 결과"))
        return request, path

    def watcher(self):
        return collector.WatchCollector(self.state, "claude:lead", lambda a: self.adapter,
                                        clock=lambda: self.now[0])

    def fail_once(self, phase):
        target, name, error = {
            "finish": (collector, "finish_request", OSError("publication failed")),
            "lock": (self.state, "lock", TimeoutError("relay lock busy")),
            "mail": (inbox, "put", PermissionError("mail write denied")),
            "request": (self.state, "save_request", PermissionError("request write denied")),
        }[phase]
        original = getattr(target, name)
        failed = False

        def attempt(*args, **kwargs):
            nonlocal failed
            if not failed:
                failed = True
                raise error
            return original(*args, **kwargs)

        return patch.object(target, name, side_effect=attempt)

    def assert_completed_once(self, request):
        current = self.state.load_request(request["id"])
        self.assertEqual(current["status"], "completed")
        self.assertEqual(current["id"], request["id"])
        self.assertEqual(current["return_to"], request["return_to"])
        self.assertEqual(current["result"]["text"], "저장할 결과")
        mail = inbox.pending(self.state.path / "inbox", request["return_to"], request["id"],
                             include_acknowledged=True)
        self.assertEqual(len(mail), 1)
        self.assertEqual((mail[0]["id"], mail[0]["kind"], mail[0]["body"]),
                         (current["reply_id"], "result", "저장할 결과"))
        self.assertFalse(inbox._is_acknowledged(self.state.path / "inbox", mail[0]))
        return mail[0]

    def check_publication_retry(self, phase):
        request, path = self.completed_rollout()
        watch = self.watcher()
        with self.fail_once(phase), patch.object(self.adapter, "deliver") as deliver:
            watch.poll()
            self.assertEqual(self.state.load_request(request["id"])["status"], "accepted")
            follower = watch.followers[request["id"]]
            self.assertIsInstance(follower, codex.RolloutCollector)
            self.assertEqual(follower.tailer.offset, path.stat().st_size)
            # A request save failure happens AFTER durable mail publication.
            partial = inbox.pending(self.state.path / "inbox", "claude:lead", request["id"])
            self.assertEqual(len(partial), int(phase == "request"))
            with patch.object(follower, "poll", wraps=follower.poll) as poll:
                self.now[0] = 1
                watch.poll()
                self.assertEqual(watch.retries[request["id"]]["count"], 1)
                self.now[0] = 2
                watch.poll()
                self.assert_completed_once(request)
                poll.assert_not_called()
            deliver.assert_not_called()
        self.assertEqual(watch.pending_results, {})
        self.assertEqual(watch.followers, {})
        self.assertEqual(watch.retries, {})
        # Success and a subsequent watch do not implicitly ACK the result.
        watch.poll()
        mail = self.assert_completed_once(request)
        inbox.acknowledge(self.state.path / "inbox", mail["id"], request["return_to"])
        self.watcher().poll()
        self.assertEqual(inbox.pending(self.state.path / "inbox", "claude:lead", request["id"]), [])
        self.assertEqual(len(inbox.pending(self.state.path / "inbox", "claude:lead", request["id"],
                                           include_acknowledged=True)), 1)

    def test_consumed_completion_retries_finish_without_new_records(self):
        self.check_publication_retry("finish")

    def test_consumed_completion_recovers_after_lock_timeout(self):
        self.check_publication_retry("lock")

    def test_consumed_completion_recovers_after_mail_write_failure(self):
        self.check_publication_retry("mail")

    def test_consumed_completion_recovers_after_request_write_failure(self):
        self.check_publication_retry("request")

    def test_new_watcher_recovers_from_source_after_each_publication_failure(self):
        for phase in ("finish", "lock", "mail", "request"):
            with self.subTest(phase=phase):
                request, _ = self.completed_rollout(phase)
                watch = self.watcher()
                with self.fail_once(phase):
                    watch.poll()
                self.assertEqual(self.state.load_request(request["id"])["status"], "accepted")
                # No cached result or tailer is transferred to the replacement.
                del watch
                self.watcher().poll()
                self.assert_completed_once(request)

    def test_publication_failure_keeps_failure_count(self):
        request, _ = self.completed_rollout()
        watch = self.watcher()
        with patch.object(collector, 'finish_request', side_effect=ValueError('ACK mismatch')) as finish:
            for second in range(15):
                self.now[0] = second
                watch.poll()
            self.assertEqual(finish.call_count, 4)
            self.assertEqual(watch.retries[request["id"]]['delay'], 16)
        self.now[0] = 30
        watch.poll()
        self.assert_completed_once(request)

    def test_another_collector_completion_discards_pending_result(self):
        request, _ = self.completed_rollout()
        watch = self.watcher()
        with self.fail_once("finish"):
            watch.poll()
        collector.finish_request(self.state, request, codex.collect_reply(request["thread"], request["marker"], 1))
        with patch.object(collector, "finish_request") as finish:
            watch.poll()
            finish.assert_not_called()
        self.assert_completed_once(request)
        self.assertEqual(watch.pending_results, {})
        self.assertEqual(watch.followers, {})
        self.assertEqual(watch.retries, {})

    def test_changed_request_signature_discards_stale_result(self):
        request, _ = self.completed_rollout()
        watch = self.watcher()
        with self.fail_once("finish"):
            watch.poll()
        request["marker"] = "[relay different]"
        self.state.save_request(request)
        self.now[0] = 2
        watch.poll()
        self.assertEqual(self.state.load_request(request["id"])["status"], "accepted")
        self.assertEqual(inbox.pending(self.state.path / "inbox", "claude:lead"), [])
        self.assertEqual(watch.pending_results, {})


class AntigravityFinishRetryTests(Base):
    def check_retained_result(self, settled):
        request = self.accepted(agent="antigravity")
        gemini = Path(self.temp.name) / "gemini"
        path = antigravity.transcript_path(request["thread"], gemini)
        path.parent.mkdir(parents=True)
        # The first poll stops at the later user input. A second poll would read
        # that user's answer and replace the marked turn's candidate.
        append(path,
               {"type": "USER_INPUT", "content": request["marker"], "created_at": "2026-10-07T09:00:00Z"},
               {"type": "PLANNER_RESPONSE", "status": "DONE", "content": "original"},
               {"type": "USER_INPUT", "content": "unmarked input", "created_at": "2026-10-07T09:01:00Z"},
               {"type": "PLANNER_RESPONSE", "status": "DONE", "content": "other turn"})
        observations = antigravity.observation_dir(self.home) / (request["thread"] + ".jsonl")
        observations.parent.mkdir(parents=True)
        start = 1791363600
        if settled:
            append(observations, {"event": "Stop", "at": start + 5, "fullyIdle": True})
        append(observations, {"event": "Stop", "at": start + 65, "fullyIdle": True})
        adapter = antigravity.AntigravityAdapter(gemini=gemini)
        now = [0]
        watch = collector.WatchCollector(self.state, "claude:lead", lambda a: adapter, clock=lambda: now[0])
        with patch.object(collector, "finish_request", side_effect=PermissionError("publication denied")):
            watch.poll()
        follower = watch.followers[request["id"]]
        self.assertIsInstance(follower, antigravity.TranscriptCollector)
        self.assertLess(follower.tailer.offset, path.stat().st_size)
        now[0] = 2
        watch.poll()
        current = self.state.load_request(request["id"])
        self.assertEqual(current["status"], "completed" if settled else "failed")
        if settled:
            self.assertEqual(current["result"]["text"], "original")
        else:
            self.assertIn("A later input arrived", current["result"]["error"])
        mail = inbox.pending(self.state.path / "inbox", "claude:lead", request["id"])
        self.assertEqual(len(mail), 1)
        self.assertEqual(mail[0]["kind"], "result" if settled else "error")
        self.assertEqual(watch.pending_results, {})

    def test_settled_completion_is_retained_before_later_transcript_records(self):
        self.check_retained_result(settled=True)

    def test_failed_completion_is_retained_before_later_transcript_records(self):
        self.check_retained_result(settled=False)


class CollectRecoveryTests(Base):
    def test_wait_and_detached_entrypoint_reread_source_after_publication_error(self):
        for command in ("wait", "collect"):
            with self.subTest(command=command):
                request = self.accepted(thread=command)
                append(self.rollout(command), event("task_started", turn_id="u1"), user(request["marker"], "u1"),
                       event("task_complete", turn_id="u1", last_agent_message="recovered"))
                args = [command, "--root", str(self.root), "--request", request["id"], "--timeout", "1"]
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                        patch.object(codex.CodexAdapter, "deliver") as deliver:
                    with patch.object(cli, "finish_request", side_effect=PermissionError("publication denied")):
                        self.assertEqual(cli.main(args), 5)
                    self.assertEqual(self.state.load_request(request["id"])["status"], "accepted")
                    self.assertIn("publication denied", err.getvalue())
                    self.assertEqual(cli.main(args), 0)
                    deliver.assert_not_called()
                self.assertEqual(self.state.load_request(request["id"])["status"], "completed")
                mail = inbox.pending(self.state.path / "inbox", "claude:lead", request["id"])
                self.assertEqual([m["body"] for m in mail], ["recovered"])
