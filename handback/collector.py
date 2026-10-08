"""Turn confirmed worker completions into inbox mail without a Lead waiting.

Two independent paths publish the same deterministic reply:

- ``spawn`` starts a detached ``collect`` process right after ``send --no-wait``,
  so a result reaches the inbox even while the Lead session is closed.
- ``WatchCollector`` lets ``inbox watch`` (the Lead's Monitor) follow the same
  requests incrementally, covering a collector that died or was never started.

Completion is decided only by the adapter's verified collection (Codex: the
rollout ``task_complete`` of the marked turn). Stop hook observations are never
used, because another Stop hook may still continue the turn.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from . import envelope, inbox
from .state import atomic_json, home_lock
from .invocation import entry_args


OPEN_STATES = frozenset({"prepared", "dispatching", "accepted", "delivery_unknown"})
# Statuses whose marked turn may still appear in the worker's record.
COLLECTABLE = frozenset({"dispatching", "accepted", "delivery_unknown"})
DEFAULT_COLLECT_TIMEOUT = 24 * 60 * 60
ENTRY_SCRIPT = Path(__file__).resolve().parents[1] / "handback.py"


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def finish_request(state, request, result):
    current = _finish_request(state, request, result)
    if current.get("reply_id"):
        from .router import route
        mail = inbox._read_json(state.path / "inbox" / (current["reply_id"] + ".json"))
        route(state, mail)
    return current


def _finish_request(state, request, result):
    """Publish the reply once; repeated or concurrent calls are idempotent."""
    if result["outcome"] == "timeout":
        return request
    if result["outcome"] not in {"completed", "failed"}:
        raise ValueError("unknown collection outcome")
    with state.lock():
        current = state.load_request(request["id"])
        if current.get("status") in {"completed", "failed"}:
            return current
        text = result.get("text") or result.get("error") or "(empty response)"
        if len(json.dumps(text, ensure_ascii=False).encode("utf-8")) > 48 * 1024:
            report = state.path / "results" / (current["id"] + ".json")
            atomic_json(report, result)
            text = "Large worker response. Read the complete result at: " + str(report)
        mail = envelope.make(current["id"], current["handle"], current["return_to"], text,
                             kind="result" if result["outcome"] == "completed" else "error",
                             hop=current.get("hop", 0) + 1,
                             message_id=uuid.uuid5(uuid.NAMESPACE_URL, "agent-relay:" + current["id"]).hex)
        # Repeating collection after a crash must publish byte-identical mail.
        mail["created_utc"] = current["created_utc"]
        inbox.put(state.path / "inbox", mail)
        # The outgoing envelope keeps the address it was sent to; a first send may
        # have rebound the request since (Antigravity provisional handle).
        sent_to = current.get("provisional_handle") or current["handle"]
        inbox.acknowledge(state.path / "inbox", current["outgoing_id"], sent_to)
        current.update(status=result["outcome"], result=result, reply_id=mail["id"],
                       completed_utc=utcnow())
        state.save_request(current)
        return current


def _detach_options():
    if os.name != "nt":
        return [{"start_new_session": True}]
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    # A host that runs commands in a kill-on-close job would end the collector with
    # the command; break away when the job allows it, otherwise stay in the job.
    return [{"creationflags": flags | subprocess.CREATE_BREAKAWAY_FROM_JOB},
            {"creationflags": flags}]


def spawn(state, request, timeout=DEFAULT_COLLECT_TIMEOUT, python=None):
    """Start a detached collector for an accepted request and record its PID."""
    with home_lock(state.home):
        return _spawn(state, request, timeout, python)


def _spawn(state, request, timeout, python):
    log_path = state.path / "log" / ("collector-" + request["id"] + ".log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    command = [python or sys.executable, *entry_args(ENTRY_SCRIPT), "collect", "--request", request["id"],
               "--root", str(state.root), "--timeout", str(timeout)]
    env = dict(os.environ, PYTHONUTF8="1")
    env["HANDBACK_HOME"] = str(state.home)
    process = error = None
    # Persist intent before starting a child. If PID publication crashes, migration
    # sees ambiguous collector metadata and refuses to move a possibly live writer.
    with state.lock():
        current = state.load_request(request["id"])
        previous = current.get("collector")
        current["collector"] = {"starting": True, "starter_pid": os.getpid(),
                                "started_utc": utcnow(), "log": str(log_path)}
        state.save_request(current)
    with log_path.open("ab") as log:
        for options in _detach_options():
            try:
                process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                           close_fds=True, cwd=str(state.root), env=env, **options)
                break
            except OSError as exc:
                error = exc
    if process is None:
        with state.lock():
            current = state.load_request(request["id"])
            if previous is None:
                current.pop("collector", None)
            else:
                current["collector"] = previous
            state.save_request(current)
        raise OSError(f"could not start collector: {error}")
    info = {"pid": process.pid, "started_utc": utcnow(), "timeout": timeout, "log": str(log_path)}
    with state.lock():
        current = state.load_request(request["id"])
        # A watcher can finish first, but the child may still hold the old log open.
        # Record its PID even when the request is already terminal.
        current["collector"] = info
        state.save_request(current)
    return info


class WatchCollector:
    """Follow open requests addressed to one recipient from inside ``inbox watch``."""

    def __init__(self, state, recipient, adapter_for, log=None, clock=time.monotonic):
        self.state = state
        self.recipient = recipient
        self.adapter_for = adapter_for
        self.followers = {}
        self.unsupported = set()
        self.log = log
        self.clock = clock
        self.retries = {}
        self.signatures = {}
        # Whether this recipient still has open requests; unknown counts as active.
        self.active = True

    def _failure(self, request_id, error):
        previous = self.retries.get(request_id, {})
        count = previous.get("count", 0) + 1
        delay = min(300, 2 ** min(count, 9))
        message = (type(error).__name__, str(error))
        if previous.get("delay") != delay or previous.get("message") != message:
            self._note(request_id, error)
        self.retries[request_id] = {"count": count, "delay": delay,
                                    "message": message, "after": self.clock() + delay}

    def _note(self, request_id, error):
        if self.log is None:
            return
        try:
            with home_lock(self.state.home, timeout=0.05):
                self.log.parent.mkdir(parents=True, exist_ok=True)
                with self.log.open("a", encoding="utf-8") as stream:
                    stream.write(f"{utcnow()} request={request_id} {type(error).__name__}: {error}\n")
        except (OSError, ValueError):
            pass

    def poll(self):
        """Publish any newly confirmed results; never raises to the watch loop."""
        try:
            from .router import route_pending
            route_pending(self.state, self.recipient, self.adapter_for)
        except Exception as error:
            self._note("delivery", error)
        try:
            if self.clock() < self.retries.get("-", {}).get("after", 0):
                return
            requests = self.state.requests()
            self.retries.pop("-", None)
        except Exception as error:  # damaged state must not stop mail delivery
            self._failure("-", error)
            return
        self.active = any(request.get("return_to") == self.recipient and request.get("status") in OPEN_STATES
                          for request in requests)
        live = set()
        for request in requests:
            request_id = request.get("id")
            if request.get("return_to") != self.recipient or request.get("status") not in COLLECTABLE:
                continue
            live.add(request_id)
            signature = (request.get("status"), request.get("agent"), request.get("thread"),
                         request.get("handle"), request.get("marker"))
            if self.signatures.get(request_id) != signature:
                self.retries.pop(request_id, None)
                self.followers.pop(request_id, None)
                self.unsupported.discard(request_id)
                self.signatures[request_id] = signature
            if request_id in self.unsupported or self.clock() < self.retries.get(request_id, {}).get("after", 0):
                continue
            try:
                follower = self.followers.get(request_id)
                if follower is None:
                    adapter = self.adapter_for(request.get("agent"))
                    make = getattr(adapter, "incremental_collector", None)
                    if make is None:
                        self.unsupported.add(request_id)
                        continue
                    follower = self.followers[request_id] = (
                        make(request, relay_home=self.state.home) if getattr(adapter, "needs_context", False)
                        else make(request))
                result = follower.poll()
                if result is not None:
                    finish_request(self.state, request, result)
                    live.discard(request_id)
                self.retries.pop(request_id, None)
            except Exception as error:
                self._failure(request_id, error)
        for request_id in set(self.followers) - live:
            del self.followers[request_id]
        for request_id in set(self.signatures) - live:
            self.signatures.pop(request_id, None)
            self.retries.pop(request_id, None)
            self.unsupported.discard(request_id)
