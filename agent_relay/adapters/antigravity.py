"""Antigravity app worker: one-shot sidecar delivery and transcript collection.

Facts this adapter relies on were measured in Phase 3.1
(docs/verification/2026-10-07-phase3-3.1.md); none has a published stability
guarantee except agentapi, sidecars and hooks themselves:

- agentapi runs only inside a sidecar. A relay-owned sidecar runs this package's
  ``antigravity-sidecar`` command once, which calls agentapi and records the result.
- ``new-conversation`` input is a USER_INPUT step; ``send-message`` input is a
  SYSTEM_MESSAGE whose body starts with ``[Message]``. A Stop hook that continues
  the loop adds a SYSTEM_MESSAGE starting with ``Stop hook blocked termination:``.
- Stop hook input has no reply text. The reply is the last tool-free
  PLANNER_RESPONSE in transcript_full.jsonl.
- A fully idle Stop is final only when no other Stop hook can continue the loop,
  or after the longest other Stop hook timeout passes without a continuation
  (completion contract approved 2026-10-07).
"""
from datetime import datetime, timezone
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid
from urllib.parse import unquote, urlparse

from .base import AdapterError, AdapterUnavailable, BaseAdapter
from .codex import BinaryRolloutTailer
from ..invocation import entry_args

SIDECAR_PREFIX = "agent-relay-"
HOOK_GROUP = "agent-relay"
HOOK_EVENTS = ("Stop", "PreInvocation")
DEFAULT_HOOK_TIMEOUT = 30
CONFIRM_MARGIN = 2.0
BLOCKED_PREFIX = "Stop hook blocked termination:"
MESSAGE_PREFIX = "[Message]"
ENTRY_SCRIPT = Path(__file__).resolve().parents[2] / "agent_relay.py"


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def gemini_home():
    return Path(os.environ.get("GEMINI_HOME") or Path.home() / ".gemini")


def _options():
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def _read_json(path, default=None):
    try:
        raw = Path(path).read_bytes()
    except FileNotFoundError:
        return default
    text = raw.decode("utf-8-sig").strip()
    return json.loads(text) if text else default


def _write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _encode_like(original, value):
    """Serialize JSON keeping the original file's newline style and trailing newline."""
    data = json.dumps(value, ensure_ascii=False, indent=2)
    crlf = original is not None and b"\r\n" in original
    if crlf:
        data = data.replace("\n", "\r\n")
    if original is None or original.endswith(b"\n"):
        data += "\r\n" if crlf else "\n"
    return data.encode("utf-8")


def update_user_json(path, change, backup_dir):
    """Back up, change and atomically replace a user JSON file; refuse concurrent edits."""
    path = Path(path)
    before = path.read_bytes() if path.exists() else None
    value = json.loads(before.decode("utf-8-sig") or "{}") if before is not None else {}
    if not isinstance(value, dict):
        raise AdapterError(f"Expected a JSON object in {path}")
    changed = change(value)
    if changed is False:
        return None
    backup = None
    if before is not None:
        backup = Path(backup_dir) / f"{path.name}.{datetime.now().strftime('%Y%m%dT%H%M%S%f')}.bak"
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_bytes(before)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_bytes(_encode_like(before, value))
    try:
        current = path.read_bytes() if path.exists() else None
        if current != before:
            raise AdapterError(f"{path} changed concurrently; nothing written")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return str(backup) if backup else None


def resolve_project(root, home=None):
    """Return the Antigravity project ID whose git folder is this checkout."""
    root = os.path.normcase(os.path.realpath(root))
    matches = []
    for path in sorted((home or gemini_home()).joinpath("config", "projects").glob("*.json")):
        try:
            project = _read_json(path, {})
        except (OSError, ValueError):
            continue
        for resource in (project.get("projectResources") or {}).get("resources") or []:
            uri = ((resource or {}).get("gitFolder") or {}).get("folderUri") or ""
            if uri and os.path.normcase(os.path.realpath(unquote(urlparse(uri).path).lstrip("/"))) == root:
                matches.append(path.stem)
    if len(matches) != 1:
        raise AdapterUnavailable(f"Expected one Antigravity project registered for {root}, found {len(matches)}; "
                                 "register the checkout as a project in the Antigravity app")
    return matches[0]


def _hook_entries(value):
    """Yield (group_name, event, hook) from either documented or flat layouts."""
    if not isinstance(value, dict):
        return
    for name, group in value.items():
        if not isinstance(group, dict) or group.get("enabled", True) is False:
            continue
        for event, items in group.items():
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, dict) and isinstance(item.get("hooks"), list):
                    for hook in item["hooks"]:
                        yield name, event, hook
                elif isinstance(item, dict):
                    yield name, event, item


def other_stop_hooks(root=None, home=None):
    """Stop hooks other than relay's that could continue a finished turn.

    Returns (count, longest_timeout_seconds). An unreadable file counts as one
    hook with the default timeout, because the relay cannot prove it is inert.
    """
    home = home or gemini_home()
    files = [home / "config" / "hooks.json"]
    if root:
        files.append(Path(root) / ".agents" / "hooks.json")
    plugins = home / "config" / "plugins"
    if plugins.is_dir():
        files.extend(plugins.rglob("hooks.json"))
    count, longest = 0, 0
    for path in files:
        try:
            value = _read_json(path, {})
        except (OSError, ValueError):
            count, longest = count + 1, max(longest, DEFAULT_HOOK_TIMEOUT)
            continue
        for name, event, hook in _hook_entries(value):
            if event != "Stop" or (name == HOOK_GROUP and path == home / "config" / "hooks.json"):
                continue
            count += 1
            timeout = hook.get("timeout", DEFAULT_HOOK_TIMEOUT) if isinstance(hook, dict) else DEFAULT_HOOK_TIMEOUT
            longest = max(longest, timeout if isinstance(timeout, (int, float)) and timeout > 0 else DEFAULT_HOOK_TIMEOUT)
    return count, longest


def observation_dir(home):
    return Path(home) / "antigravity" / "observations"


def record_observation(home, payload, event):
    """Hook side: append a Stop/PreInvocation observation for one conversation."""
    conversation = payload.get("conversationId")
    if not isinstance(conversation, str) or not conversation or any(c in conversation for c in "/\\:.\0"):
        return
    keep = {"executionNum", "terminationReason", "error", "fullyIdle", "invocationNum", "initialNumSteps",
            "deliveryReservation"}
    record = {"at": time.time(), "event": event, **{k: payload.get(k) for k in keep if k in payload}}
    directory = observation_dir(home)
    path = directory / (conversation + ".jsonl")
    if not path.exists():
        # Only conversations the relay opened get a spool file; others are ignored.
        return
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")


def _observations(home, conversation):
    path = observation_dir(home) / (conversation + ".jsonl")
    result = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                result.append(json.loads(line))
            except ValueError:
                continue
    except FileNotFoundError:
        pass
    return result


def lead_idle(home, conversation):
    observations = _observations(home, conversation)
    return bool(observations and observations[-1].get("event") == "Stop"
                and observations[-1].get("fullyIdle") is True)


def observe_lead(home, conversation):
    if not isinstance(conversation, str) or not conversation or any(c in conversation for c in "/\\:.\0"):
        raise ValueError("invalid Antigravity conversation id")
    path = observation_dir(home) / (conversation + ".jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)


def _epoch(value):
    """Transcript created_at (second resolution, UTC) as epoch seconds, or None."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def transcript_path(conversation, home=None):
    return (home or gemini_home()) / "antigravity" / "brain" / conversation / ".system_generated" / "logs" / "transcript_full.jsonl"


def _input_kind(record):
    """'input' for a new user/agentapi input, 'continue' for a hook continuation, else None."""
    kind = record.get("type")
    content = record.get("content") or ""
    if kind == "USER_INPUT":
        return "input"
    if kind == "SYSTEM_MESSAGE":
        body = content.rsplit("<SYSTEM_MESSAGE>", 1)[-1].lstrip()
        if body.startswith(BLOCKED_PREFIX):
            return "continue"
        if body.startswith(MESSAGE_PREFIX):
            # agentapi send-message arrives as sender=system. Background task
            # notifications use sender=<conversation>/task-N and belong to the
            # running turn (Phase 4.3: one ended a relay request early).
            sender = re.search(r"\bsender=(\S+)", body.partition("\n")[0])
            return "input" if sender and sender.group(1) == "system" else None
    return None


class TranscriptCollector:
    """Incremental completion check for one marked Antigravity turn."""

    def __init__(self, conversation, marker, relay_home, root=None, gemini=None, clock=time.time):
        self.conversation = conversation
        self.marker = marker
        self.relay_home = Path(relay_home)
        self.root = root
        self.gemini = gemini or gemini_home()
        self.clock = clock
        self.tailer = None
        self.seen = False
        self.marker_at = None
        self.candidate = None
        self.steps_after_candidate = 0
        self.last_error = None

    def _feed(self, record):
        kind = _input_kind(record)
        content = record.get("content") or ""
        if not self.seen:
            if kind == "input" and self.marker in content:
                self.seen = True
                self.marker_at = _epoch(record.get("created_at")) or self.clock()
            return None
        if kind == "input":
            settled = self._settled_before(_epoch(record.get("created_at")))
            if settled is not None:
                return settled
            return {"text": "", "outcome": "failed", "turn": None,
                    "error": "A later input arrived before the relay reply was confirmed; refusing to collect its answer"}
        if (record.get("type") == "PLANNER_RESPONSE" and not record.get("tool_calls")
                and content and record.get("status") == "DONE"):
            self.candidate = content
            self.steps_after_candidate = 0
        else:
            # Any later step (tool call, tool output, continuation) supersedes the candidate.
            self.steps_after_candidate += 1
            if record.get("status") == "ERROR":
                self.last_error = content[:500] or "Antigravity step ended with ERROR"
        return None

    def poll(self):
        if self.tailer is None:
            path = transcript_path(self.conversation, self.gemini)
            if not path.exists():
                return None
            self.tailer = BinaryRolloutTailer(path)
        for record in self.tailer.read_records():
            result = self._feed(record)
            if result is not None:
                return result
        if not self.seen or self.marker_at is None:
            return None
        stops = [o for o in _observations(self.relay_home, self.conversation)
                 if o.get("event") == "Stop" and o.get("fullyIdle") is True and o.get("at", 0) >= self.marker_at - 1]
        if not stops:
            return None
        stop = stops[-1]
        later = [o for o in _observations(self.relay_home, self.conversation)
                 if o.get("event") == "PreInvocation" and o.get("at", 0) > stop["at"]]
        if later:
            return None  # the loop resumed; wait for the next Stop
        if self.candidate is None or self.steps_after_candidate:
            reason = stop.get("terminationReason")
            if stop.get("error") or (reason and reason != "NO_TOOL_CALL"):
                outcome = {"text": "", "outcome": "failed", "turn": None,
                           "error": f"Antigravity stopped: {reason} {stop.get('error') or self.last_error or ''}".strip()}
                return self._confirmed(stop, outcome)
            return None
        return self._confirmed(stop, {"text": self.candidate, "outcome": "completed", "turn": None})

    def _settled_before(self, input_at):
        """The marked turn ended before a later input if a fully idle Stop came first.

        Lets a collector that starts late (the next input is already in the
        transcript) still collect. Input that arrives while the agent is busy is
        injected into the running turn, so without an earlier idle Stop it fails.
        A continuation would have added a transcript step after the candidate.
        """
        if input_at is None or self.candidate is None or self.steps_after_candidate:
            return None
        # created_at has second resolution; observations are precise.
        stops = [o for o in _observations(self.relay_home, self.conversation)
                 if o.get("event") == "Stop" and o.get("fullyIdle") is True
                 and self.marker_at - 1 <= o.get("at", 0) <= input_at + 1]
        if not stops:
            return None
        return {"text": self.candidate, "outcome": "completed", "turn": None,
                "confirmation": {"settled_before_next_input": True}}

    def _confirmed(self, stop, outcome):
        others, longest = other_stop_hooks(self.root, self.gemini)
        if others and self.clock() < stop["at"] + longest + CONFIRM_MARGIN:
            return None  # another Stop hook may still continue this turn
        outcome["confirmation"] = {"other_stop_hooks": others, "waited_for_timeout": longest if others else 0}
        return outcome


def _short_path(path):
    """Windows 8.3 path when the long one has spaces; hook commands cannot be quoted."""
    path = str(path)
    if os.name != "nt" or " " not in path:
        return path
    import ctypes
    buffer = ctypes.create_unicode_buffer(32768)
    if ctypes.windll.kernel32.GetShortPathNameW(path, buffer, len(buffer)):
        return buffer.value
    return path


def hook_command(event, relay_home):
    entry = [_short_path(p) if p not in ("-m", "agent_relay") else p for p in entry_args(ENTRY_SCRIPT)]
    parts = [_short_path(sys.executable), "-X", "utf8", *entry, "hook", "--agent", "antigravity",
             "--event", event, "--state-home", _short_path(relay_home)]
    if any(" " in part or '"' in part for part in parts):
        raise AdapterUnavailable("Antigravity hook commands cannot be quoted; a path still contains a space: "
                                 + " ".join(parts))
    return " ".join(parts)


def install_hooks(relay_home, dry_run=False, home=None):
    path = (home or gemini_home()) / "config" / "hooks.json"
    group = {event: [{"type": "command", "command": hook_command(event, relay_home), "timeout": 10}]
             for event in HOOK_EVENTS}

    def change(value):
        if value.get(HOOK_GROUP) == group:
            return False
        value[HOOK_GROUP] = group
        return True

    if dry_run:
        return {"path": str(path), "group": HOOK_GROUP, "add": group, "dry_run": True}
    backup = update_user_json(path, change, Path(relay_home) / "antigravity" / "backups")
    return {"path": str(path), "group": HOOK_GROUP, "add": group, "backup": backup, "changed": backup is not None}


def uninstall_hooks(relay_home, dry_run=False, home=None):
    path = (home or gemini_home()) / "config" / "hooks.json"
    if dry_run:
        return {"path": str(path), "remove": HOOK_GROUP, "dry_run": True}

    def change(value):
        if HOOK_GROUP not in value:
            return False
        value.pop(HOOK_GROUP)
        return True

    backup = update_user_json(path, change, Path(relay_home) / "antigravity" / "backups")
    return {"path": str(path), "removed": HOOK_GROUP, "backup": backup, "changed": backup is not None}


def run_sidecar_job(job_path):
    """Body of the relay sidecar: one agentapi call, recorded, never repeated."""
    job_path = Path(job_path)
    job = _read_json(job_path)
    folder = job_path.parent
    try:
        with (folder / "attempted").open("x", encoding="utf-8") as stream:
            stream.write(utcnow())
    except FileExistsError:
        return 0
    result = {"started": utcnow()}
    try:
        if os.environ.get("ANTIGRAVITY_PROJECT_ID") and os.environ["ANTIGRAVITY_PROJECT_ID"] != job["project_id"]:
            raise AdapterError("sidecar runs in a different Antigravity project")
        executable = os.environ.get("ANTIGRAVITY_AGENTAPI_EXE") or shutil.which("agentapi")
        if not executable:
            raise AdapterUnavailable("agentapi is not available inside the sidecar")
        # Prefer the executable over agentapi.BAT: cmd would mangle quotes and newlines.
        command = [executable] + ([] if Path(executable).stem.lower() == "agentapi" else ["agentapi"]) + job["args"]
        completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                   timeout=120, **_options())
        result.update(exit=completed.returncode, stdout=completed.stdout, stderr=completed.stderr)
    except Exception as error:
        result.update(exit=None, error=f"{type(error).__name__}: {error}")
    result["finished"] = utcnow()
    _write_json(folder / "result.json", result)
    return 0


def cleanup_sidecars(relay_home, dry_run=False, home=None):
    """Remove only provably owned, inactive sidecars with no open request."""
    from ..collector import OPEN_STATES
    home = home or gemini_home()
    relay_home = Path(relay_home).resolve()
    path = home / "config" / "config.json"
    before = path.read_bytes() if path.exists() else None
    value = _read_json(path, {})
    candidates = []
    manifests = {}
    for name, entry in value.get("sidecars", {}).items():
        if (not name.startswith(SIDECAR_PREFIX) or Path(name).name != name or
                any(c in name for c in "/\\:") or not isinstance(entry, dict) or
                entry.get("enabled") is not False):
            continue
        folder = home / "config" / "sidecars" / name
        if folder.is_symlink() or (hasattr(folder, "is_junction") and folder.is_junction()):
            continue
        manifest = folder / "sidecar.json"
        try:
            raw = manifest.read_bytes()
            spec = json.loads(raw)
            args = spec.get("args", [])
            if not spec.get("description", "").startswith("agent-relay"):
                continue
            legacy = (len(args) == 4 and Path(args[0]).resolve() == ENTRY_SCRIPT and
                      args[1:3] == ["antigravity-sidecar", "--job"])
            module = len(args) == 5 and args[:4] == ["-m", "agent_relay", "antigravity-sidecar", "--job"]
            if legacy or module:
                job_path = Path(args[-1]).resolve()
                job_path.relative_to(relay_home / "projects")
                job = _read_json(job_path, {})
                request_id = job.get("request_id")
                from ..state import ID_PATTERN
                if not isinstance(request_id, str) or not ID_PATTERN.fullmatch(request_id):
                    continue
                if name != SIDECAR_PREFIX + request_id[:12]:
                    continue
                request = _read_json(job_path.parents[2] / "requests" / (request_id + ".json"))
                if request is not None and (not isinstance(request, dict) or
                        request.get("id") != request_id or request.get("status") in OPEN_STATES):
                    continue
            else:
                continue
        except (OSError, ValueError, TypeError, AttributeError):
            continue
        candidates.append(name)
        manifests[name] = raw
    result = {"candidates": candidates, "count": len(candidates), "dry_run": dry_run}
    if dry_run or not candidates:
        return result
    backups = relay_home / "antigravity" / "backups"
    stamp = datetime.now().strftime('%Y%m%dT%H%M%S%f')
    folder_backup = backups / ("sidecars." + stamp)
    for name in candidates:
        shutil.copytree(home / "config" / "sidecars" / name, folder_backup / name)

    def change(current):
        if (cleanup_sidecars(relay_home, dry_run=True, home=home)["candidates"] != candidates or
                path.read_bytes() != before or current != value or any(
                (home / "config" / "sidecars" / name / "sidecar.json").read_bytes() != manifests[name]
                for name in candidates)):
            raise AdapterError("Sidecar configuration changed concurrently; nothing written")
        for name in candidates:
            current["sidecars"].pop(name)

    result["backup"] = update_user_json(path, change, backups)
    result["folder_backup"] = str(folder_backup)
    for name in candidates:
        folder = (home / "config" / "sidecars" / name).resolve()
        folder.relative_to((home / "config" / "sidecars").resolve())
        shutil.rmtree(folder)
    result["removed"] = candidates
    return result


class AntigravityAdapter(BaseAdapter):
    name = "antigravity"
    needs_context = True

    def __init__(self, executable=None, model=None, delivery_timeout=60, gemini=None):
        self.executable = executable
        self.model = model or "flash"
        self.delivery_timeout = delivery_timeout
        self.gemini = gemini or gemini_home()

    def _agentapi_help(self):
        override = self.executable or os.environ.get("AGENT_RELAY_ANTIGRAVITY") or os.environ.get("ANTIGRAVITY_AGENTAPI")
        candidates = [override] if override else []
        if not override:
            roots = [os.environ.get("LOCALAPPDATA")]
            roots.extend(str(Path(profile) / "AppData/Local") for profile in
                         (os.environ.get("USERPROFILE"), str(Path.home())) if profile)
            candidates.extend(str(Path(root) / "Programs/antigravity/resources/bin/language_server.exe")
                              for root in roots if root)
        for candidate in candidates:
            if not candidate:
                continue
            command = [str(candidate)] + ([] if Path(candidate).stem.lower() == "agentapi" else ["agentapi"])
            try:
                result = subprocess.run(command + ["--help"], capture_output=True, text=True, encoding="utf-8",
                                        errors="replace", timeout=15, **_options())
            except (OSError, subprocess.TimeoutExpired):
                continue
            if result.returncode == 0 and "new-conversation" in result.stdout:
                return str(candidate)
        return None

    def detect(self, root=None):
        executable = self._agentapi_help()
        sidecars = (_read_json(self.gemini / "config" / "config.json", {}) or {}).get("sidecars") or {}
        own = [k for k in sidecars if k.startswith(SIDECAR_PREFIX)]
        others, longest = other_stop_hooks(root, self.gemini)
        hooks = _read_json(self.gemini / "config" / "hooks.json", {}) or {}
        info = {"agent": self.name, "installed": executable is not None, "executable": executable, "version": None,
                "warnings": [], "capabilities": {"worker": executable is not None, "lead": False,
                                                 "lead_implemented": True, "lead_deferred": True,
                                                 "hooks_installed": HOOK_GROUP in hooks},
                "sidecars": {"relay_owned": len(own), "other": len(sidecars) - len(own),
                             "enabled": sum(1 for v in sidecars.values() if isinstance(v, dict) and v.get("enabled"))},
                "other_stop_hooks": {"count": others, "longest_timeout": longest}}
        if HOOK_GROUP not in hooks:
            info["warnings"].append("relay Antigravity hooks are not installed; replies cannot be confirmed "
                                    "(install-hooks --agents antigravity)")
        if root:
            try:
                info["project_id"] = resolve_project(root, self.gemini)
            except AdapterUnavailable as error:
                info["warnings"].append(str(error))
        return info

    def new_thread(self, root, name, sandbox="workspace-write", open_app=True):
        if sandbox == "read-only":
            raise AdapterUnavailable("Antigravity cannot enforce a read-only sandbox; state read-only limits in the brief")
        resolve_project(root, self.gemini)
        # agentapi cannot create an empty conversation; the first send creates it.
        return "pending-" + uuid.uuid4().hex

    def deliver(self, thread, envelope, state=None, request=None, title=None):
        if state is None or request is None:
            raise AdapterError("Antigravity delivery needs the request state")
        project_id = resolve_project(request.get("root") or state.root, self.gemini)
        folder = state.path / "antigravity" / request["id"]
        if thread.startswith("pending-"):
            args = ["new-conversation", f"--title={title or 'agent-relay'}", f"--model={self.model}", envelope["body"]]
        else:
            args = ["send-message", thread, envelope["body"]]
        job = {"request_id": request["id"], "project_id": project_id, "args": args, "created": utcnow()}
        _write_json(folder / "job.json", job)
        sidecar = SIDECAR_PREFIX + request["id"][:12]
        sidecar_dir = self.gemini / "config" / "sidecars" / sidecar
        _write_json(sidecar_dir / "sidecar.json", {
            "command": sys.executable, "args": [*entry_args(ENTRY_SCRIPT), "antigravity-sidecar", "--job", str(folder / "job.json")],
            "restart_policy": "never", "display_name": "agent-relay " + request["id"][:8],
            "description": "agent-relay: one agentapi call for one request, then exits."})
        backups = state.home / "antigravity" / "backups"
        config = self.gemini / "config" / "config.json"

        def enable(value):
            value.setdefault("sidecars", {})[sidecar] = {"enabled": True, "projectId": project_id}

        settled = False
        try:
            update_user_json(config, enable, backups)
            deadline = time.monotonic() + self.delivery_timeout
            while time.monotonic() < deadline and not (folder / "result.json").exists():
                time.sleep(0.25)
            result = _read_json(folder / "result.json")
            if result is None:
                return {"accepted": False, "unknown": True,
                        "stderr": "Antigravity did not run the relay sidecar in time (is the app running?); "
                                  "delivery is unknown; do not re-send"}
            settled = True
            if result.get("exit") is None:
                return {"accepted": False, "unknown": True,
                        "stderr": result.get("error") or "sidecar outcome is unknown"}
            if result.get("exit") != 0:
                return {"accepted": False, "returncode": result.get("exit"),
                        "stderr": result.get("stderr") or result.get("error") or "agentapi failed",
                        "classification": self.classify_error(result.get("stderr") or result.get("error") or "")}
            delivery = {"accepted": True, "returncode": 0}
            if thread.startswith("pending-"):
                try:
                    conversation = json.loads(result["stdout"])["response"]["newConversation"]["conversationId"]
                except (KeyError, TypeError, ValueError) as error:
                    raise AdapterError("agentapi did not return a conversation id") from error
                delivery["thread"] = conversation
            # Open the spool so the relay hook records observations for this conversation.
            spool = observation_dir(state.home) / ((delivery.get("thread") or thread) + ".jsonl")
            spool.parent.mkdir(parents=True, exist_ok=True)
            spool.touch()
            return delivery
        finally:
            def settle(value):
                entry = value.get("sidecars", {}).get(sidecar)
                if entry is None:
                    return False
                if settled:
                    value["sidecars"].pop(sidecar)  # relay-owned; nothing else references it
                else:
                    entry["enabled"] = False  # keep for diagnosis when the outcome is unknown
            update_user_json(config, settle, backups)
            if settled:
                shutil.rmtree(sidecar_dir, ignore_errors=True)

    def deliver_to_lead(self, thread, text, state=None):
        if state is None:
            return {"accepted": False, "pending": True}
        with state.lock("lead:" + thread):
            if not lead_idle(state.home, thread):
                return {"accepted": False, "pending": True}
            # Reserve idle BEFORE submission, so fast real Stop hooks win over
            # this reservation and concurrent mail cannot reuse the same Stop.
            reservation = uuid.uuid4().hex
            record_observation(state.home, {"conversationId": thread, "deliveryReservation": reservation}, "PreInvocation")
            request = {"id": uuid.uuid4().hex, "root": str(state.root)}
            result = self.deliver(thread, {"body": text}, state=state, request=request)
            observations = _observations(state.home, thread)
            if (not result.get("accepted") and not result.get("unknown") and observations
                    and observations[-1].get("deliveryReservation") == reservation):
                record_observation(state.home, {"conversationId": thread, "fullyIdle": True}, "Stop")
            return result

    def incremental_collector(self, request, relay_home=None):
        from ..state import state_home
        thread = request.get("thread")
        if not thread or thread.startswith("pending-"):
            raise ValueError("request has no Antigravity conversation yet")
        return TranscriptCollector(thread, request["marker"], relay_home or state_home(),
                                   root=request.get("root"), gemini=self.gemini)

    def fallback_collect(self, request, timeout=0):
        collector = self.incremental_collector(request)
        deadline = time.time() + timeout if timeout else float("inf")
        while True:
            result = collector.poll()
            if result is not None:
                return result
            if time.time() >= deadline:
                return {"text": "", "outcome": "timeout",
                        "error": "Timed out waiting for the Antigravity reply; do not re-send"}
            time.sleep(1)

    def classify_error(self, text):
        lowered = str(text).lower()
        if "connect" in lowered and ("language server" in lowered or "ls_address" in lowered):
            return "not_running"
        return super().classify_error(text)
