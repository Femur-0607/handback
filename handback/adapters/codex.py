"""Codex app-server creation, queue delivery, and correlated rollout fallback.

The JSONL fallback is intentionally separate from the unverified app hook path.
It consumes complete UTF-8 lines in binary mode, so CRLF and partial writes cannot
change the byte offset or attach a later user's answer to a relay request.
"""
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time

from .base import AdapterError, AdapterUnavailable, BaseAdapter
from ..discovery import codex_bundled_candidates as _bundled_candidates

REPLY_EVENTS = {"task_complete"}
FAIL_EVENTS = {"turn_aborted", "error"}
POLL_INTERVAL = 0.25


def _process_options():
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def _version(executable):
    try:
        result = subprocess.run([str(executable), "--version"], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=15, **_process_options())
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def resolve_codex(executable=None):
    """Explicit argument/env override, newest working app bundle, then PATH.

    An invalid explicit override fails rather than silently choosing another CLI.
    """
    override = executable or os.environ.get("HANDBACK_CODEX")
    if override:
        candidate = os.path.expanduser(os.path.expandvars(str(override)))
        candidate = shutil.which(candidate) or candidate
        if _version(candidate):
            return str(candidate)
        raise AdapterUnavailable(f"Codex executable override is not working: {candidate}")
    for candidate in _bundled_candidates():
        if _version(candidate):
            return candidate
    candidate = shutil.which("codex")
    if candidate and _version(candidate):
        return candidate
    raise AdapterUnavailable("No working Codex executable found; set HANDBACK_CODEX or install Codex")


def codex_home():
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))


class AppServer:
    """Bounded JSON-RPC client over a private app-server stdio process."""

    def __init__(self, codex, cwd, timeout=30):
        self.proc = subprocess.Popen([str(codex), "app-server"], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     text=True, encoding="utf-8", errors="replace", cwd=cwd,
                                     **_process_options())
        self.timeout = timeout
        self.next_id = 0
        self._responses = queue.Queue()
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()

    def _read_stdout(self):
        try:
            for line in self.proc.stdout:
                try:
                    reply = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                # Notifications can be numerous; only RPC replies need buffering.
                if isinstance(reply, dict) and "id" in reply and "method" not in reply:
                    self._responses.put(reply)
        finally:
            self._responses.put(None)

    def call(self, method, params, notify=False, timeout=None):
        msg = {"method": method, "params": params}
        if not notify:
            self.next_id += 1
            msg["id"] = self.next_id
        try:
            self.proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()
        except (OSError, ValueError) as exc:
            raise AdapterError(f"codex app-server closed during {method}") from exc
        if notify:
            return None
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        while True:
            try:
                reply = self._responses.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty as exc:
                raise AdapterError(f"codex app-server timed out during {method}") from exc
            if reply is None:
                raise AdapterError(f"codex app-server closed during {method}")
            if reply.get("id") == self.next_id:
                if "error" in reply:
                    raise AdapterError(f"{method} failed: {reply['error']}")
                if "result" not in reply:
                    raise AdapterError(f"{method} returned no result")
                return reply["result"]

    def initialize(self):
        self.call("initialize", {"clientInfo": {"name": "handback", "title": "handback", "version": "1"},
                                 "capabilities": {"experimentalApi": True}})
        self.call("initialized", {}, notify=True)

    def close(self):
        graceful = True
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except (OSError, ValueError):
            pass
        exit_code = self.proc.poll()
        if exit_code is None:
            # EOF lets the server flush asynchronous thread settings to the
            # native index. Immediate terminate loses the first turn's effort.
            try:
                exit_code = self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                graceful = False
                self.proc.terminate()
                try:
                    exit_code = self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    exit_code = self.proc.wait(timeout=5)
        self._reader.join(timeout=1)
        if self.proc.stdout:
            self.proc.stdout.close()
        return graceful and exit_code == 0


def open_in_app(thread_id):
    url = f"codex://threads/{thread_id}"
    if sys.platform == "win32":
        os.startfile(url)
    else:
        subprocess.run(["open" if sys.platform == "darwin" else "xdg-open", url],
                       check=False, timeout=15)


def rollout_path(thread, deadline):
    # Literal filename comparison prevents a thread/name containing glob metacharacters
    # from accidentally selecting another conversation's rollout.
    suffix = str(thread) + ".jsonl"
    while True:
        found = [path for path in codex_home().joinpath("sessions").rglob("rollout-*.jsonl")
                 if path.name.endswith(suffix)]
        if found:
            return str(max(found, key=lambda path: path.stat().st_mtime))
        if time.time() >= deadline:
            return None
        time.sleep(min(POLL_INTERVAL, max(0, deadline - time.time())))


def _user_text(record):
    payload = record.get("payload") or {}
    if not isinstance(payload, dict):
        return None
    if record.get("type") == "event_msg" and payload.get("type") == "user_message":
        return payload.get("message") if isinstance(payload.get("message"), str) else ""
    if record.get("type") == "response_item" and payload.get("type") == "message" and payload.get("role") == "user":
        content = payload.get("content") or []
        if isinstance(content, str):
            return content
        return "\n".join(part["text"] for part in content if isinstance(part, dict) and isinstance(part.get("text"), str))
    return None


def carries_marker(record, marker):
    """Match the user-side input only, never an assistant quoting the marker."""
    text = _user_text(record)
    return bool(marker) and text is not None and marker in text


class BinaryRolloutTailer:
    """Read only complete JSONL records, preserving exact file byte offsets."""

    def __init__(self, path):
        self.path = Path(path)
        self.offset = 0
        self.generation = 0

    def read_records(self):
        try:
            if self.path.stat().st_size < self.offset:
                self.offset = 0
                self.generation += 1
            with self.path.open("rb") as stream:
                stream.seek(self.offset)
                while True:
                    line = stream.readline()
                    if not line.endswith(b"\n"):
                        return
                    self.offset = stream.tell()
                    try:
                        record = json.loads(line)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    if isinstance(record, dict):
                        yield record
        except FileNotFoundError:
            return


class ReplyTracker:
    """Bind a marker to its active turn and reject later, unmarked user input."""

    def __init__(self, marker):
        self.marker = marker
        self.active_turn = None
        self.target_turn = None
        self.seen = False

    def feed(self, record):
        payload = record.get("payload") or {}
        if not isinstance(payload, dict):
            return None
        kind = payload.get("type") if record.get("type") == "event_msg" else None
        turn = payload.get("turn_id") or payload.get("turnId")
        if kind == "task_started":
            self.active_turn = turn
            if self.seen and self.target_turn is None:
                self.target_turn = turn
            return None
        if carries_marker(record, self.marker):
            if not self.seen:
                self.seen = True
                self.target_turn = turn or self.active_turn
            return None
        if self.seen and _user_text(record) is not None:
            if self.target_turn and self.active_turn == self.target_turn:
                # Same-turn user input (an answer to the worker's in-turn question, or
                # the user steering it) belongs to this request; only a new turn is foreign.
                return None
            return {"text": "", "outcome": "failed", "turn": self.target_turn,
                    "error": "A later user message arrived before the relay reply; refusing to collect its answer"}
        if kind not in REPLY_EVENTS | FAIL_EVENTS:
            return None
        if not self.seen:
            if not turn or not self.active_turn or turn == self.active_turn:
                self.active_turn = None
            return None
        if self.target_turn and turn and turn != self.target_turn:
            return None
        if self.target_turn and not turn and self.active_turn and self.active_turn != self.target_turn:
            return None
        if kind in REPLY_EVENTS:
            return {"text": payload.get("last_agent_message") or "", "outcome": "completed",
                    "turn": self.target_turn or turn}
        return {"text": "", "outcome": "failed", "turn": self.target_turn or turn,
                "error": f"Codex turn ended with {kind}: {json.dumps(payload, ensure_ascii=False)[:500]}"}


class RolloutCollector:
    """Non-blocking form of collect_reply for long-lived watchers.

    Each poll reads only bytes appended since the previous poll, so a watcher can
    follow many requests without rereading whole rollout files.
    """

    def __init__(self, thread, marker):
        if not marker:
            raise ValueError("A nonempty request marker is required")
        self.thread = thread
        self.marker = marker
        self.tailer = None
        self.tracker = ReplyTracker(marker)
        self.generation = 0

    def poll(self):
        if self.tailer is None:
            path = rollout_path(self.thread, 0)
            if path is None:
                return None
            self.tailer = BinaryRolloutTailer(path)
            self.generation = self.tailer.generation
        for record in self.tailer.read_records():
            if self.generation != self.tailer.generation:
                self.tracker = ReplyTracker(self.marker)
                self.generation = self.tailer.generation
            result = self.tracker.feed(record)
            if result is not None:
                return result
        return None


def collect_reply(thread, marker, timeout=0):
    if not marker:
        raise ValueError("A nonempty request marker is required")
    if timeout < 0:
        raise ValueError("timeout must be nonnegative")
    deadline = time.time() + timeout if timeout else float("inf")
    path = rollout_path(thread, deadline)
    if path is None:
        return {"text": "", "outcome": "timeout",
                "error": f"No rollout file for thread {thread} (use the thread ID, not its name, for waiting)"}
    collector = RolloutCollector(thread, marker)
    collector.tailer = BinaryRolloutTailer(path)
    while True:
        result = collector.poll()
        if result is not None:
            return result
        if time.time() >= deadline:
            return {"text": "", "outcome": "timeout", "turn": collector.tracker.target_turn,
                    "error": "Timed out waiting for the Codex reply; the message stays queued, do not re-send"}
        time.sleep(min(POLL_INTERVAL, max(0, deadline - time.time())))


def wait_reply(thread, marker, return_file, timeout):
    result = collect_reply(thread, marker, timeout)
    if result["outcome"] == "completed":
        if return_file:
            Path(return_file).write_text(result["text"], encoding="utf-8")
        print(result["text"])
        return 0
    print(result.get("error") or "Codex turn failed", file=sys.stderr)
    return 3 if result["outcome"] == "timeout" else 2


class CodexAdapter(BaseAdapter):
    name = "codex"

    def __init__(self, executable=None, model=None, reasoning_effort=None):
        self.executable = executable
        for label, value in (("model", model), ("reasoning_effort", reasoning_effort)):
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{label} must be a nonempty string or None")
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.execution_settings = {key: value for key, value in
                                   (("model", model), ("reasoning_effort", reasoning_effort))
                                   if value is not None}
        self._thread_settings = {}

    def _resolve_settings(self, server, cwd, existing=None):
        """Resolve installed Codex settings, including cwd-specific config layers.

        Native thread settings take precedence over today's defaults when sending
        to an existing conversation. Catalog values are dynamic; in particular,
        neither the supported efforts nor the model's default is hard-coded.
        """
        existing = existing or {}
        config = server.call("config/read", {"cwd": os.path.abspath(cwd), "includeLayers": False})["config"]
        model = self.model or existing.get("model") or config.get("model")
        effort = self.reasoning_effort or existing.get("reasoningEffort") or config.get("model_reasoning_effort")
        models = []
        cursor = None
        seen_cursors = set()
        while True:
            params = {"limit": 100, "includeHidden": True}
            if cursor is not None:
                params["cursor"] = cursor
            page = server.call("model/list", params)
            models.extend(page["data"])
            cursor = page.get("nextCursor")
            if not cursor:
                break
            if cursor in seen_cursors:
                raise AdapterError("Codex model/list repeated a pagination cursor")
            seen_cursors.add(cursor)
        selected = next((item for item in models if model in (item.get("id"), item.get("model"))), None) if model else \
            next((item for item in models if item.get("isDefault")), None)
        if selected is None:
            raise AdapterError(f"Codex model is not advertised by the installed client: {model or '(default)'}")
        model = model or selected["model"]
        effort = effort or selected.get("defaultReasoningEffort")
        supported = [item["reasoningEffort"] for item in selected.get("supportedReasoningEfforts", [])]
        if not effort or effort not in supported:
            raise AdapterError(f"Codex model {model} does not support reasoning effort {effort!r}; "
                               f"supported: {', '.join(supported) or '(none)'}")
        return {"model": model, "reasoning_effort": effort}

    def detect(self):
        info = {"agent": self.name, "installed": False, "executable": None, "version": None,
                "warnings": [], "capabilities": {"worker": True, "lead": True, "hooks_verified": False}}
        try:
            info["executable"] = resolve_codex(self.executable)
            info["version"] = _version(info["executable"])
            info["installed"] = True
        except AdapterUnavailable as exc:
            info["warnings"].append(str(exc))
        info["warnings"].append("App lifecycle hooks are unverified; replies use the rollout fallback")
        return info

    def new_thread(self, root, name, sandbox="workspace-write", open_app=True, writable_roots=None):
        params = {"cwd": os.path.abspath(root), "sandbox": sandbox, "approvalPolicy": "never", "threadSource": "user"}
        if writable_roots:
            if sandbox != "workspace-write":
                raise ValueError("--add-dir requires the workspace-write sandbox")
            # Same effect as `codex exec --add-dir`: extra writable roots for this thread only.
            params["config"] = {"sandbox_workspace_write": {
                "writable_roots": [os.path.abspath(path) for path in writable_roots]}}
        server = AppServer(resolve_codex(self.executable), str(root))
        try:
            server.initialize()
            settings = self._resolve_settings(server, root)
            params["model"] = settings["model"]
            params.setdefault("config", {})["model_reasoning_effort"] = settings["reasoning_effort"]
            started = server.call("thread/start", params)
            thread_id = started["thread"]["id"]
            # Prefer the server's effective settings (it may normalize a model alias).
            self.execution_settings = {"model": started.get("model") or settings["model"],
                                       "reasoning_effort": started.get("reasoningEffort") or settings["reasoning_effort"]}
            self._thread_settings[thread_id] = dict(self.execution_settings)
            server.call("thread/name/set", {"threadId": thread_id, "name": name})
            # thread/start reports the chosen settings, but naming a new thread
            # alone does not persist its model/effort in the native thread index.
            server.call("thread/settings/update", {"threadId": thread_id,
                        "model": self.execution_settings["model"], "effort": self.execution_settings["reasoning_effort"]})
        finally:
            clean_shutdown = server.close()
        if clean_shutdown is False:
            raise AdapterError(f"Codex app-server did not exit cleanly; settings persistence for thread {thread_id} is unconfirmed")
        if open_app:
            open_in_app(thread_id)
        return thread_id

    def deliver(self, thread, envelope):
        body = envelope.get("body")
        if not isinstance(body, str) or not body.strip():
            raise ValueError("envelope.body must contain the marked message text")
        thread = str(thread).removeprefix("codex:")
        try:
            executable = resolve_codex(self.executable)
            settings = self._thread_settings.get(thread)
            if settings is None:
                server = AppServer(executable, os.getcwd())
                try:
                    server.initialize()
                    existing = server.call("thread/read", {"threadId": thread, "includeTurns": False})["thread"]
                    settings = self._resolve_settings(server, existing.get("cwd") or os.getcwd(), existing)
                    if existing.get("model") != settings["model"] or existing.get("reasoningEffort") != settings["reasoning_effort"]:
                        # A metadata-only resume restores this conversation's
                        # saved permissions; no sandbox/approval override is sent.
                        server.call("thread/resume", {"threadId": thread, "excludeTurns": True})
                        server.call("thread/settings/update", {"threadId": thread,
                                    "model": settings["model"], "effort": settings["reasoning_effort"]})
                finally:
                    clean_shutdown = server.close()
                if clean_shutdown is False:
                    raise AdapterError("Codex app-server did not exit cleanly; settings persistence was not confirmed before queueing")
                self._thread_settings[thread] = dict(settings)
            self.execution_settings = dict(settings)
            # queue's shared --model/-c options are ignored by thread/queue/add.
            # Keep its usual desktop route after persisting native settings.
            result = subprocess.run([executable, "queue", "--thread", thread, "--message", body],
                                    capture_output=True, text=True, encoding="utf-8",
                                    errors="replace", timeout=30, **_process_options())
            return {"accepted": result.returncode == 0, "stdout": result.stdout, "stderr": result.stderr,
                    "returncode": result.returncode}
        except subprocess.TimeoutExpired:
            # Queue acceptance is ambiguous. The caller must keep ownership and must not retry.
            return {"accepted": False, "unknown": True, "stdout": "", "stderr": "codex queue timed out; delivery is unknown; do not re-send",
                    "returncode": None}
        except (AdapterError, OSError) as exc:
            return {"accepted": False, "stdout": "", "stderr": str(exc), "returncode": None}

    def deliver_to_lead(self, thread, text, state=None):
        from ..router import sandboxed
        if sandboxed():
            return {"accepted": False, "pending": True}
        return self.deliver(thread, {"body": text})

    def parse_hook(self, event, stdin_json):
        data = stdin_json
        event = event.lower().replace("_", "")
        text = data.get("last_assistant_message", "") if event == "stop" else data.get("prompt", "")
        return {"thread": data.get("session_id"), "turn": data.get("turn_id"), "text": text or "",
                "outcome": "completed" if event == "stop" else "submitted"}

    def fallback_collect(self, request, timeout=0):
        thread = request.get("thread") or request.get("thread_id") or request.get("to")
        if not thread:
            raise ValueError("request.thread is required")
        return collect_reply(str(thread).removeprefix("codex:"), request["marker"], timeout)

    def incremental_collector(self, request):
        thread = request.get("thread") or request.get("thread_id") or request.get("to")
        if not thread:
            raise ValueError("request.thread is required")
        return RolloutCollector(str(thread).removeprefix("codex:"), request["marker"])

    def selftest(self):
        return cmd_selftest(executable=self.executable)


def cmd_selftest(_args=None, executable=None):
    ok = True
    try:
        codex = resolve_codex(executable)
    except AdapterUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"codex: {codex} ({_version(codex)})")
    result = subprocess.run([codex, "queue", "--help"], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=30, **_process_options())
    has_queue = result.returncode == 0 and "--thread" in result.stdout
    print(f"codex queue: {'ok' if has_queue else 'MISSING'}")
    ok &= has_queue
    server = None
    try:
        server = AppServer(codex, os.getcwd())
        server.initialize()
        print("app-server initialize: ok")
    except (AdapterError, OSError) as exc:
        print(f"app-server initialize: FAILED ({exc})")
        ok = False
    finally:
        if server:
            server.close()
    files = sorted(codex_home().joinpath("sessions").rglob("rollout-*.jsonl"),
                   key=lambda path: path.stat().st_mtime)[-20:]
    kinds = set()
    for path in files:
        for record in BinaryRolloutTailer(path).read_records():
            payload = record.get("payload") or {}
            if not isinstance(payload, dict):
                continue
            if record.get("type") == "event_msg":
                kinds.add(payload.get("type"))
            elif record.get("type") == "response_item" and payload.get("role") == "user":
                kinds.add("user message item")
    for name in ("user message item", "task_complete"):
        found = name in kinds
        print(f"rollout {name}: {'seen' if found else 'NOT SEEN in recent sessions'}")
        ok &= found
    print("selftest: " + ("ok" if ok else "FAILED"))
    return 0 if ok else 1
