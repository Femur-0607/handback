"""Copy quiescent relay state, verify it, and retain the original directory.

No agent is resumed, no collector is killed, and no conflicting destination is
overwritten. Old processes may not know the activity lock, so snapshots and PID
checks are repeated before publishing and before recording the move.
"""

from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import uuid

from . import watcher
from .collector import OPEN_STATES
from .state import ID_PATTERN, _valid_migration_record, atomic_json, home_lock, state_home


RECEIPT = ".migration-receipt.json"
_META = {"MOVED.json", RECEIPT}
_TERMINAL = frozenset({"completed", "failed", "send_failed"})


def _canonical(path):
    return os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(path))))


def _linked(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _safe_root(path, must_exist=False):
    path = Path(os.path.abspath(Path(path).expanduser()))
    for item in [path, *path.parents]:
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if _linked(info):
            raise ValueError(f"Migration refuses a symlink/junction/reparse-point path: {item}")
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"Migration directory path is not a directory: {item}")
    if must_exist and not path.is_dir():
        raise ValueError(f"Source state directory does not exist: {path}")
    if path == Path(path.anchor) or _canonical(path) == _canonical(Path.home()):
        raise ValueError(f"Refusing to migrate a filesystem or user-profile root: {path}")
    return path.resolve()


def _stat_identity(info):
    # Windows Python versions disagree between stat and fstat about whether
    # ctime denotes creation or metadata-change time, notably after os.replace.
    # File identity, length, write time and content hash remain comparable.
    identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    return identity if os.name == "nt" else (*identity, info.st_ctime_ns)


def _file_info(path):
    info = path.lstat()
    if _linked(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError(f"Migration refuses linked or non-regular files: {path}")
    return info


def _hash_file(path):
    before = _file_info(path)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        if _stat_identity(os.fstat(stream.fileno())) != _stat_identity(before):
            raise ValueError(f"Source changed while opening: {path}")
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
        if _stat_identity(os.fstat(stream.fileno())) != _stat_identity(before):
            raise ValueError(f"Source changed while hashing: {path}")
    if _stat_identity(_file_info(path)) != _stat_identity(before):
        raise ValueError(f"Source changed while hashing: {path}")
    return {"size": before.st_size, "sha256": digest.hexdigest(), "identity": _stat_identity(before)}


def _snapshot(root):
    files, directories = {}, []
    def visit(directory):
        with os.scandir(directory) as entries:
            items = sorted(entries, key=lambda entry: entry.name)
        for entry in items:
            path = Path(entry.path)
            relative = path.relative_to(root).as_posix()
            info = entry.stat(follow_symlinks=False)
            if _linked(info):
                raise ValueError(f"Migration refuses links and junctions in state: {path}")
            if stat.S_ISDIR(info.st_mode):
                if entry.name == ".locks":
                    continue  # operational lock inodes must never be migrated
                directories.append(relative)
                visit(path)
            elif stat.S_ISREG(info.st_mode):
                if path.parent == root and entry.name in _META:
                    continue
                if entry.name.endswith(".tmp"):
                    raise ValueError(f"Unresolved temporary state file; migration is ambiguous: {path}")
                files[relative] = _hash_file(path)
            else:
                raise ValueError(f"Migration refuses non-regular state entry: {path}")
    visit(root)
    return {"files": files, "directories": sorted(directories)}


def _content(snapshot):
    return {"directories": snapshot["directories"],
            "files": {name: {"size": value["size"], "sha256": value["sha256"]}
                      for name, value in snapshot["files"].items()}}


def _fingerprint(snapshot):
    raw = json.dumps(_content(snapshot), sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _json(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"Invalid JSON prevents safe state migration: {path}") from error
    if not isinstance(value, dict):
        raise ValueError(f"Non-object JSON prevents safe state migration: {path}")
    return value


def pid_alive(pid):
    """Return a definite process state; permission/inspection failures raise."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0 or pid > 0xFFFFFFFF:
        raise ValueError(f"Invalid recorded collector PID: {pid!r}")
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except OSError as error:
            raise ValueError(f"Cannot determine whether collector PID {pid} is alive") from error
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:  # ERROR_INVALID_PARAMETER: PID does not exist
            return False
        raise ValueError(f"Cannot inspect collector PID {pid}: Windows error {error}")
    try:
        code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
            raise ValueError(f"Cannot read collector PID {pid} status: Windows error {ctypes.get_last_error()}")
        return code.value == 259  # STILL_ACTIVE
    finally:
        kernel.CloseHandle(handle)


def _quiescent(root, snapshot):
    requests, checked_pids = 0, set()
    for name in snapshot["files"]:
        parts = Path(name).parts
        if len(parts) == 4 and parts[0] == "projects" and parts[2] == "requests":
            if not name.endswith(".json") or not ID_PATTERN.fullmatch(Path(name).stem):
                raise ValueError(f"Unrecognized request file prevents safe migration: {name}")
            request = _json(root / name)
            if request.get("id") != Path(name).stem:
                raise ValueError(f"Request ID mismatch prevents migration: {name}")
            status = request.get("status")
            if not isinstance(status, str):
                raise ValueError(f"Unknown request status prevents migration: {request['id']} ({status!r})")
            if status in OPEN_STATES:
                raise ValueError(f"Open request prevents migration: {request['id']} ({status})")
            if status not in _TERMINAL:
                raise ValueError(f"Unknown request status prevents migration: {request['id']} ({status!r})")
            requests += 1
            if "collector" in request:
                info = request["collector"]
                if not isinstance(info, dict) or "pid" not in info:
                    raise ValueError(f"Ambiguous collector metadata prevents migration: {name}")
                pid = info["pid"]
                if pid_alive(pid):
                    raise ValueError(f"Live collector PID {pid} prevents migration: {request['id']}")
                checked_pids.add(pid)
        elif len(parts) == 5 and parts[0] == "projects" and parts[2:4] == ("inbox", "delivered"):
            delivery = _json(root / name)
            if delivery.get("id") != Path(name).stem or delivery.get("status") not in (
                    "pending", "delivered", "failed", "delivery_unknown"):
                raise ValueError(f"Invalid Lead delivery record prevents migration: {name}")
            if delivery["status"] == "delivery_unknown":
                raise ValueError(f"Unknown Lead delivery prevents migration: {delivery['id']}")
        elif len(parts) == 4 and parts[0] == "projects" and parts[2] == watcher.LEASE_DIR:
            lease = _json(root / name)
            if watcher.is_fresh(lease, lease.get("recipient")):
                raise ValueError(f"Live inbox watcher prevents migration: {lease.get('recipient')}")
        elif name.endswith(".json"):
            # Parse state JSON to fail closed on damaged configuration/inbox data.
            value = _json(root / name)
            if name == "hook-install/manifest.json" and value.get("transaction_pending"):
                raise ValueError("Interrupted hook installation must be recovered before migration")
    return {"closed_requests": requests, "dead_collector_pids": sorted(checked_pids)}


def _validate_receipt(receipt, source, destination):
    if (not _valid_migration_record(receipt) or receipt.get("phase") not in ("pending", "complete")
            or receipt.get("source") != str(source) or receipt.get("destination") != str(destination)
            or not isinstance(receipt.get("fingerprint"), str)):
        raise ValueError(f"Conflicting or invalid migration receipt: {destination / RECEIPT}")


def _copy_file(source, destination, expected):
    before = _file_info(source)
    if _stat_identity(before) != expected["identity"]:
        raise ValueError(f"Source changed before copy: {source}")
    digest = hashlib.sha256()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as incoming, destination.open("xb") as outgoing:
        if _stat_identity(os.fstat(incoming.fileno())) != expected["identity"]:
            raise ValueError(f"Source changed while opening copy: {source}")
        for block in iter(lambda: incoming.read(1024 * 1024), b""):
            outgoing.write(block)
            digest.update(block)
        outgoing.flush()
        os.fsync(outgoing.fileno())
        if _stat_identity(os.fstat(incoming.fileno())) != expected["identity"]:
            raise ValueError(f"Source changed during copy: {source}")
    if digest.hexdigest() != expected["sha256"]:
        raise ValueError(f"Source hash changed during copy: {source}")
    if os.name != "nt":
        os.chmod(destination, stat.S_IMODE(before.st_mode) & 0o700)


def _write_marker(source, receipt):
    marker = {key: receipt[key] for key in ("schema", "id", "source", "destination", "fingerprint", "created_utc")}
    path = source / "MOVED.json"
    # Publish complete bytes without replacing an existing marker. Windows
    # rename is no-replace; POSIX link provides that guarantee on other hosts.
    temporary = source / (".MOVED." + uuid.uuid4().hex + ".tmp")
    try:
        atomic_json(temporary, marker)
        if os.name == "nt":
            os.rename(temporary, path)
        else:
            os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _finish(source, destination, receipt, snapshot):
    if _snapshot(source) != snapshot:
        raise ValueError("Source changed after copying; destination remains pending and cannot be used")
    _quiescent(source, snapshot)
    if not (source / "MOVED.json").exists():
        _write_marker(source, receipt)
    receipt = {**receipt, "phase": "complete"}
    atomic_json(destination / RECEIPT, receipt)


def migrate_state(source, destination=None, dry_run=False, timeout=5):
    """Copy a stopped state home to the default home; never delete the source.

    ``destination`` is injectable for temporary tests. The CLI only exposes
    ``--from`` and takes its destination from ``state_home()``.
    """
    source = _safe_root(source, must_exist=True)
    destination = _safe_root(destination if destination is not None else state_home())
    if (_canonical(source) == _canonical(destination) or source in destination.parents
            or destination in source.parents):
        raise ValueError("Source and destination must be distinct, non-overlapping directories")
    if source.name.casefold() in {".codex", ".claude", ".gemini", ".aws", ".ssh"} or not any(
            (source / name).exists() for name in ("config.json", "projects", "hook-install")):
        raise ValueError(f"Source does not identify an handback state home: {source}")

    def inspect():
        snapshot = _snapshot(source)
        audit = _quiescent(source, snapshot)
        marker_path, receipt_path = source / "MOVED.json", destination / RECEIPT
        marker = _json(marker_path) if marker_path.exists() else None
        receipt = _json(receipt_path) if receipt_path.exists() else None
        if marker is not None:
            if (not _valid_migration_record(marker) or marker.get("destination") != str(destination)
                    or marker.get("source") != str(source) or not receipt):
                raise ValueError("Source MOVED.json conflicts with the requested destination or receipt is missing")
            _validate_receipt(receipt, source, destination)
            if (any(marker[key] != receipt[key] for key in ("id", "source", "destination", "fingerprint"))
                    or marker["fingerprint"] != _fingerprint(snapshot)):
                raise ValueError("Moved source changed or migration IDs do not match; refusing migration")
            if receipt["phase"] == "complete":
                return snapshot, audit, receipt, "already_migrated"
        if receipt is not None:
            _validate_receipt(receipt, source, destination)
            copied = _content(_snapshot(destination))
            expected = _content(snapshot)
            compatible = (set(copied["directories"]).issubset(expected["directories"])
                          and all(expected["files"].get(name) == value for name, value in copied["files"].items()))
            if receipt["fingerprint"] != _fingerprint(snapshot) or not compatible or (
                    receipt["phase"] == "complete" and copied != expected):
                raise ValueError("Existing destination conflicts with source snapshot; no overwrite")
            return snapshot, audit, receipt, "resume"
        if destination.exists() and any(p.name != ".locks" for p in destination.iterdir()):
            raise ValueError(f"Destination is not empty; no files will be overwritten: {destination}")
        return snapshot, audit, None, "copy"

    snapshot, audit, receipt, mode = inspect()
    def result(status):
        return {"action": "migrate-state", "source": str(source), "destination": str(destination),
                "dry_run": bool(dry_run), "status": status, "file_count": len(snapshot["files"]),
                "bytes": sum(info["size"] for info in snapshot["files"].values()),
                "fingerprint": _fingerprint(snapshot), "source_preserved": True, **audit}
    if dry_run:
        return result("already_migrated" if mode == "already_migrated" else "ready_" + mode)
    with ExitStack() as stack:
        for path in sorted((source, destination), key=_canonical):
            stack.enter_context(home_lock(path, exclusive=True, timeout=timeout, allow_moved=True))
        _safe_root(source, must_exist=True)
        _safe_root(destination)
        snapshot, audit, receipt, mode = inspect()
        if mode == "already_migrated":
            return result("already_migrated")
        if mode == "resume":
            # A crash during entry publication can leave a verified subset under
            # a pending receipt. Rebuild missing entries without overwriting any.
            for name in snapshot["directories"]:
                (destination / name).mkdir(parents=True, exist_ok=True)
            for name, expected in snapshot["files"].items():
                target = destination / name
                if target.exists():
                    continue
                temporary = target.with_name(target.name + "." + uuid.uuid4().hex + ".tmp")
                try:
                    _copy_file(source / name, temporary, expected)
                    os.rename(temporary, target)
                finally:
                    temporary.unlink(missing_ok=True)
            if _content(_snapshot(destination)) != _content(snapshot):
                raise ValueError("Resumed migration verification failed")
            _finish(source, destination, receipt, snapshot)
            return result("migrated")
        receipt = {"schema": 1, "id": uuid.uuid4().hex, "source": str(source), "destination": str(destination),
                   "phase": "pending", "fingerprint": _fingerprint(snapshot),
                   "created_utc": datetime.now(timezone.utc).isoformat()}
        stage = destination.parent / ("." + destination.name + ".migrating-" + uuid.uuid4().hex)
        stage.mkdir(mode=0o700)
        published = False
        try:
            for name in snapshot["directories"]:
                (stage / name).mkdir(parents=True, exist_ok=True)
            for name, expected in snapshot["files"].items():
                _copy_file(source / name, stage / name, expected)
            if _content(_snapshot(stage)) != _content(snapshot):
                raise ValueError("Copied state hash verification failed; destination was not published")
            if _snapshot(source) != snapshot:
                raise ValueError("Source changed during migration; destination was not published")
            _quiescent(source, snapshot)
            atomic_json(stage / RECEIPT, receipt)
            _safe_root(destination)
            # Keep destination/.locks and its held inode in place. Publish only
            # payload entries while holding both homes' migration ownership.
            destination.mkdir(exist_ok=True)
            # Receipt precedes payload, so interrupted publication is recoverable.
            os.rename(stage / RECEIPT, destination / RECEIPT)
            for entry in stage.iterdir():
                os.rename(entry, destination / entry.name)
            stage.rmdir()
            published = True
            _finish(source, destination, receipt, snapshot)
        finally:
            if not published and stage.exists():
                # Only remove the exact private staging directory created above.
                if stage.parent.resolve() != destination.parent.resolve() or not stage.name.startswith("." + destination.name + ".migrating-"):
                    raise ValueError("Unsafe migration staging cleanup path")
                _snapshot(stage)  # reject links before recursive cleanup
                shutil.rmtree(stage)
        return result("migrated")
