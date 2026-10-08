"""Merge optional relay hooks without replacing unrelated user settings.

Only exact matcher groups recorded by this installer are removable. Backups contain
the original bytes. Optimistic checks prevent overwriting edits observed after a
configuration snapshot; unrelated applications do not participate in the relay lock.
No code in this module reads or writes Codex hook trust.
"""

import base64
from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shlex
import stat
import sys
import uuid

from .state import ProjectState, state_home
from .invocation import entry_args


EVENTS = {"codex": ("UserPromptSubmit", "Stop", "Interrupt"),
          "claude": ("SessionStart", "UserPromptSubmit")}
_UNCHECKED = object()


def _agents(agents):
    if isinstance(agents, str):
        agents = [name.strip() for name in agents.split(",") if name.strip()]
    agents = list(dict.fromkeys(agents))
    if not agents or any(agent not in EVENTS for agent in agents):
        raise ValueError("Hook agents must be codex and/or claude; other adapters are unverified")
    return agents


def _home(home):
    return Path(home).expanduser().resolve() if home is not None else state_home().resolve()


def _config_path(agent, config_paths):
    if config_paths is not None and agent in config_paths:
        return Path(config_paths[agent]).expanduser().resolve()
    if agent == "codex":
        return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser().resolve() / "hooks.json"
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser().resolve() / "settings.json"


def _bytes(path):
    try:
        return Path(path).read_bytes()
    except FileNotFoundError:
        return None


def _hash(value):
    return hashlib.sha256(value).hexdigest() if value is not None else None


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _object(raw, path):
    if raw is None:
        return {}
    try:
        value = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, ValueError) as error:
        raise ValueError(f"Invalid JSON in {path}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return value


def _atomic_bytes(path, data, expected=_UNCHECKED):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != "nt":
            # Settings may contain private env values; never widen their mode,
            # and keep new backups/manifests private to this OS user.
            mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
            os.chmod(temporary, mode)
        if expected is not _UNCHECKED and _bytes(path) != expected:
            raise ValueError(f"Configuration changed concurrently; no overwrite: {path}")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _lock(home, dry_run):
    if dry_run:
        return nullcontext()
    # Reuse the OS-backed lock; this is global installation state, not a project.
    state = ProjectState(Path(__file__).parent, home=home)
    state.path = home / "hook-install"
    return state.lock("installation", timeout=5)


def _manifest(home):
    path = home / "hook-install" / "manifest.json"
    raw = _bytes(path)
    value = _object(raw, path) if raw is not None else {"schema": 1, "installations": {}}
    if value.get("schema") != 1 or not isinstance(value.get("installations"), dict):
        raise ValueError(f"Invalid hook ownership manifest: {path}")
    for record in value["installations"].values():
        if not isinstance(record, dict) or record.get("agent") not in EVENTS or not isinstance(record.get("path"), str):
            raise ValueError(f"Invalid hook ownership record: {path}")
        if not isinstance(record.get("groups"), list):
            raise ValueError(f"Invalid hook ownership groups: {path}")
        for owned in record["groups"]:
            if not isinstance(owned, dict) or owned.get("event") not in EVENTS[record["agent"]] or not isinstance(owned.get("group"), dict):
                raise ValueError(f"Invalid owned hook group: {path}")
    return path, raw, value


def _record_key(agent, path):
    return hashlib.sha256((agent + ":" + os.path.normcase(str(path))).encode("utf-8")).hexdigest()


def _hook_groups(config, event, path):
    hooks = config.get("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError(f"hooks must be an object: {path}")
    groups = hooks.get(event, [])
    if not isinstance(groups, list) or any(not isinstance(group, dict) for group in groups):
        raise ValueError(f"hooks.{event} must be a list of matcher groups: {path}")
    return groups


def _powershell_string(value):
    return "'" + value.replace("'", "''") + "'"


def _spec(agent, event, script_path, home):
    # Resolving a POSIX venv's python symlink would select the base interpreter
    # and lose the installed package. Preserve the environment's executable.
    executable = os.path.abspath(sys.executable)
    arguments = ["-X", "utf8", *entry_args(script_path), "hook", "--agent", agent,
                 "--event", event, "--state-home", str(home)]
    timeout = 3 if event == "Interrupt" else 5
    if agent == "claude":
        # Official exec form: no Bash/PowerShell interpolation on any platform.
        handler = {"type": "command", "command": executable, "args": arguments, "timeout": timeout}
        invocation = [executable, *arguments]
    else:
        handler = {"type": "command", "command": shlex.join([executable, *arguments]), "timeout": timeout}
        invocation = [executable, *arguments]
        if sys.platform == "win32":
            decoded = "& " + " ".join(_powershell_string(value) for value in invocation) + "; exit $LASTEXITCODE"
            encoded = base64.b64encode(decoded.encode("utf-16le")).decode("ascii")
            handler["commandWindows"] = (
                "powershell.exe -NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -EncodedCommand " + encoded)
    return {"hooks": [handler]}, invocation


def _contains_handler(groups, owned_group):
    handlers = owned_group.get("hooks", [])
    return any(isinstance(group.get("hooks"), list) and any(handler in group["hooks"] for handler in handlers)
               for group in groups)


def legacy_handler(handler, agent, event):
    """Recognize only the old installer's explicit Python hook invocation."""
    if not isinstance(handler, dict) or handler.get("type") != "command":
        return False
    try:
        args = handler.get("args")
        if args is None:
            tokens = shlex.split(handler.get("command", ""), posix=False)
            args = [token.strip("\"'") for token in tokens[1:]]
        else:
            args = list(args)
        if args[:2] == ["-X", "utf8"]:
            args = args[2:]
        if args[:2] == ["-m", "agent_relay"]:
            args = args[2:]
        elif args and args[0].replace("\\", "/").rsplit("/", 1)[-1] == "agent_relay.py":
            args = args[1:]
        else:
            return False
        return (len(args) == 7 and args[:5] == ["hook", "--agent", agent, "--event", event]
                and args[5] == "--state-home" and bool(args[6]))
    except (TypeError, ValueError, AttributeError):
        return False


def _prepare(action, agents, script_path, home, config_paths, manifest):
    next_manifest = deepcopy(manifest)
    next_manifest.pop("transaction_pending", None)
    plans, changes, warnings = [], [], []
    for agent in agents:
        path = _config_path(agent, config_paths)
        raw = _bytes(path)
        before = _object(raw, path)
        after = deepcopy(before)
        key = _record_key(agent, path)
        old_record = manifest["installations"].get(key)
        if old_record and (old_record["agent"] != agent or old_record["path"] != str(path)):
            raise ValueError(f"Hook ownership path mismatch: {path}")
        previous = deepcopy(old_record.get("groups", [])) if old_record else []
        owned, removed, added, invocations = [], [], [], []
        hooks_existed = old_record.get("hooks_existed", "hooks" in before) if old_record else "hooks" in before
        for event in EVENTS[agent]:
            groups = _hook_groups(after, event, path)
            originally_present = event in before.get("hooks", {})
            event_owned = [entry for entry in previous if entry["event"] == event]
            # Adopt intact legacy installer groups even when using a fresh state home.
            # Mixed/edited matcher groups remain user-owned.
            for group in groups:
                handlers = group.get("hooks", [])
                if (set(group) == {"hooks"} and len(handlers) == 1
                        and legacy_handler(handlers[0], agent, event)
                        and not any(entry["group"] == group for entry in event_owned)):
                    event_owned.append({"event": event, "group": deepcopy(group), "event_existed": True})
            wanted, invocation = _spec(agent, event, script_path, home) if action == "install" else (None, None)
            for entry in event_owned:
                originally_present = entry.get("event_existed", True)
                group = entry["group"]
                if action == "install" and group == wanted and group in groups:
                    owned.append(entry)
                    continue
                if group in groups:
                    groups.remove(group)
                    removed.append({"event": event, "group": group})
                elif _contains_handler(groups, group):
                    warnings.append(f"Preserved an edited relay matcher group in {path}: {event}")
                    owned.append(entry)
                    if action == "install":
                        raise ValueError(f"Relay matcher group was edited; preserve it and resolve manually before reinstalling: {path}: {event}")
            if action == "install":
                if wanted not in groups:
                    groups.append(deepcopy(wanted))
                    entry = {"event": event, "group": wanted, "event_existed": originally_present}
                    owned.append(entry)
                    added.append({"event": event, "group": wanted})
                    invocations.append({"event": event, "argv": invocation})
                # An identical, pre-existing group is deliberately not adopted.
                if groups:
                    after.setdefault("hooks", {})[event] = groups
            elif event in after.get("hooks", {}):
                if not groups and not originally_present:
                    del after["hooks"][event]
                else:
                    after["hooks"][event] = groups
        if not after.get("hooks") and not hooks_existed:
            after.pop("hooks", None)
        changed = after != before
        if owned:
            next_manifest["installations"][key] = {
                "agent": agent, "path": str(path), "groups": owned, "hooks_existed": hooks_existed,
                "last_written_sha256": _hash(_json_bytes(after)) if changed else _hash(raw),
                "backups": deepcopy(old_record.get("backups", [])) if old_record else [],
            }
        else:
            next_manifest["installations"].pop(key, None)
        public = {"agent": agent, "path": str(path), "changed": changed, "added": added,
                  "removed": removed, "backup": None, "invocations": invocations}
        changes.append(public)
        if changed:
            plans.append({"agent": agent, "path": path, "before": raw, "after": _json_bytes(after),
                          "key": key, "public": public})
    return next_manifest, plans, changes, warnings


def _commit(home, manifest_path, original_manifest_bytes, next_manifest, plans):
    """Journal ownership before publishing, then roll back a failed local write.

    If an unrelated writer changes a published file during rollback, preserve that
    file and the ownership journal so uninstall can still remove exact relay groups.
    """
    for plan in plans:
        if _bytes(plan["path"]) != plan["before"]:
            raise ValueError(f"Configuration changed concurrently; no overwrite: {plan['path']}")
        if plan["before"] is not None:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
            backup = home / "hook-install" / "backups" / f"{plan['agent']}-{stamp}-{uuid.uuid4().hex[:8]}.bak"
            _atomic_bytes(backup, plan["before"], expected=None)
            plan["public"]["backup"] = str(backup)
            if plan["key"] in next_manifest["installations"]:
                next_manifest["installations"][plan["key"]]["backups"].append(str(backup))
    # Keep both old and proposed ownership during the multi-file transaction. A
    # crash at any point leaves enough information for a removal-only uninstall.
    pending_manifest = deepcopy(next_manifest)
    previous_manifest = _object(original_manifest_bytes, manifest_path)
    for key, previous in previous_manifest.get("installations", {}).items():
        if key not in pending_manifest["installations"]:
            pending_manifest["installations"][key] = deepcopy(previous)
        else:
            groups = pending_manifest["installations"][key]["groups"]
            for group in previous["groups"]:
                if group not in groups:
                    groups.append(deepcopy(group))
    pending_manifest["transaction_pending"] = True
    journal = _json_bytes(pending_manifest)
    _atomic_bytes(manifest_path, journal, expected=original_manifest_bytes)
    published = []
    try:
        for plan in plans:
            _atomic_bytes(plan["path"], plan["after"], expected=plan["before"])
            published.append(plan)
        _atomic_bytes(manifest_path, _json_bytes(next_manifest), expected=journal)
    except Exception as error:
        conflicts = []
        for plan in reversed(published):
            try:
                if _bytes(plan["path"]) != plan["after"]:
                    raise ValueError("modified by another writer")
                if plan["before"] is None:
                    plan["path"].unlink()
                else:
                    _atomic_bytes(plan["path"], plan["before"], expected=plan["after"])
            except (OSError, ValueError):
                conflicts.append(str(plan["path"]))
        if not conflicts:
            try:
                if original_manifest_bytes is None:
                    if _bytes(manifest_path) == journal:
                        manifest_path.unlink()
                else:
                    _atomic_bytes(manifest_path, original_manifest_bytes, expected=journal)
            except (OSError, ValueError):
                # The journal is conservative: an absent group is never removed.
                pass
        if conflicts:
            raise ValueError("Installation failed; newer edits were preserved. Ownership journal retained for uninstall: "
                             + ", ".join(conflicts)) from error
        raise


def _run(action, agents, script_path=None, home=None, dry_run=False, config_paths=None):
    agents = _agents(agents)
    home = _home(home)
    if action == "install" and script_path is not None:
        script_path = Path(script_path).expanduser().resolve()
        if not script_path.is_file():
            raise ValueError(f"Relay entry point does not exist: {script_path}")
    with _lock(home, dry_run):
        manifest_path, manifest_raw, manifest = _manifest(home)
        next_manifest, plans, changes, warnings = _prepare(action, agents, script_path, home, config_paths, manifest)
        if manifest.get("transaction_pending"):
            warnings.append("Recovered hook ownership from an interrupted installation transaction.")
        if not dry_run and (plans or next_manifest != manifest):
            _commit(home, manifest_path, manifest_raw, next_manifest, plans)
        if action == "install" and "codex" in agents:
            warnings.append("Codex skips new or changed hooks until the user reviews and trusts them with /hooks; trust is not modified.")
        return {"action": action, "dry_run": bool(dry_run), "manifest": str(manifest_path),
                "changes": changes, "warnings": warnings}


def install(agents, script_path=None, home=None, dry_run=False, config_paths=None):
    return _run("install", agents, script_path=script_path, home=home, dry_run=dry_run, config_paths=config_paths)


def uninstall(agents, home=None, dry_run=False, config_paths=None):
    return _run("uninstall", agents, home=home, dry_run=dry_run, config_paths=config_paths)
