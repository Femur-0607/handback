"""Durable per-repository state and process-safe standard-library file locks."""

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import uuid


ID_PATTERN = re.compile(r"[0-9a-f]{32}\Z")
_LOCKS = {}
_LOCKS_GUARD = threading.Lock()
_LOCAL_LOCKS = threading.local()
_MISSING = object()


def state_home():
    """Return the state directory without creating it or reading credentials."""
    override = os.environ.get("AGENT_RELAY_HOME")
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "win32":
        # Profile-root dot directories are shared with processes outside MSIX.
        return Path(os.environ.get("USERPROFILE") or Path.home()) / ".agent-relay"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "agent-relay"
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "agent-relay"


def _normal_path(path):
    return os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(path))))


def project_identity(root, timeout=5):
    """Git worktrees share a key; non-Git roots use their canonical directory."""
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Project root is not a directory: {root}")
    common = None
    checkout_root = str(root)
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--path-format=absolute", "--git-common-dir", "--show-toplevel"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
        if result.returncode == 0 and result.stdout.strip():
            lines = result.stdout.strip().splitlines()
            reported = Path(lines[0])
            common = _normal_path(reported if reported.is_absolute() else root / reported)
            if len(lines) > 1:
                checkout_root = str(Path(lines[1]).resolve())
    except (OSError, subprocess.TimeoutExpired):
        pass
    normalized = common or _normal_path(root)
    return {"key": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
            "root": str(root), "common_dir": common, "checkout_root": checkout_root}


def atomic_json(path, data):
    """Publish a complete UTF-8 JSON file using a same-directory atomic replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, ensure_ascii=False, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_json(path, default):
    try:
        with Path(path).open(encoding="utf-8-sig") as stream:
            return json.load(stream)
    except FileNotFoundError:
        return _MISSING if default is _MISSING else deepcopy(default)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"Invalid JSON state in {path}: {error}") from error


def _request_id(value):
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise ValueError("Request id must be 32 lowercase hexadecimal characters")
    return value


def _os_try_lock(stream):
    stream.seek(0)
    if sys.platform == "win32":
        import msvcrt
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _os_unlock(stream):
    stream.seek(0)
    if sys.platform == "win32":
        import msvcrt
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _valid_migration_record(value):
    return (isinstance(value, dict) and value.get("schema") == 1
            and isinstance(value.get("id"), str) and bool(ID_PATTERN.fullmatch(value["id"]))
            and isinstance(value.get("fingerprint"), str)
            and bool(re.fullmatch(r"[0-9a-f]{64}", value["fingerprint"]))
            and isinstance(value.get("created_utc"), str) and bool(value["created_utc"])
            and all(isinstance(value.get(key), str) and Path(value[key]).is_absolute()
                    for key in ("source", "destination")))


def moved_destination(home=None):
    """Read migration guidance without creating state or following it silently."""
    home = Path(home).expanduser().resolve() if home is not None else state_home()
    marker = _read_json(home / "MOVED.json", None)
    if marker is None and not (home / "MOVED.json").exists():
        return None
    if not _valid_migration_record(marker) or _normal_path(marker["source"]) != _normal_path(home):
        raise ValueError(f"Invalid MOVED.json migration marker in {home}; mutations are disabled")
    destination = Path(marker["destination"]).resolve()
    if _normal_path(destination) == _normal_path(home):
        raise ValueError(f"MOVED.json points to its own source: {home}")
    receipt = _read_json(destination / ".migration-receipt.json", None)
    if (not _valid_migration_record(receipt) or receipt.get("phase") not in ("pending", "complete")
            or any(receipt[key] != marker[key] for key in ("id", "source", "destination", "fingerprint"))):
        raise ValueError(f"MOVED.json destination receipt is missing or inconsistent: {destination}")
    return destination


def state_warnings(home=None):
    """Read-only MSIX and migration diagnostics for the selected state home."""
    home = Path(home).expanduser() if home is not None else state_home()
    real = os.path.realpath(home)
    parts = re.split(r"[\\/]", real.casefold())
    warnings = []
    if "packages" in parts and "localcache" in parts[parts.index("packages") + 1:]:
        warnings.append(f"MSIX-virtualized state path may differ across applications: {real}")
    try:
        destination = moved_destination(home)
        if destination is not None:
            warnings.append(f"State was migrated from {home}; use the new state home: {destination}")
        receipt_path = Path(real) / ".migration-receipt.json"
        receipt = _read_json(receipt_path, None)
        if receipt_path.exists() and (not _valid_migration_record(receipt) or receipt.get("phase") != "complete"):
            warnings.append(f"State migration is incomplete at {real}; resume migrate-state before writing")
    except (ValueError, OSError) as error:
        warnings.append(str(error))
    return warnings


class _ReaderWriterGate:
    def __init__(self):
        self.condition = threading.Condition()
        self.readers = 0
        self.writer = False

    def acquire(self, exclusive, deadline):
        with self.condition:
            while self.writer or (exclusive and self.readers):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("State home is in use; migration/activity lock is busy")
                self.condition.wait(remaining)
            if exclusive:
                self.writer = True
            else:
                self.readers += 1

    def release(self, exclusive):
        with self.condition:
            if exclusive:
                self.writer = False
            else:
                self.readers -= 1
            self.condition.notify_all()


_HOME_GATES = {}


def _home_os_lock(stream, exclusive):
    if os.name != "nt":
        import fcntl
        fcntl.flock(stream.fileno(), (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        return None
    import ctypes
    from ctypes import wintypes
    import msvcrt
    class Overlapped(ctypes.Structure):
        _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                    ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD), ("hEvent", wintypes.HANDLE)]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                 wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(Overlapped)]
    kernel.LockFileEx.restype = wintypes.BOOL
    overlap = Overlapped()
    if not kernel.LockFileEx(msvcrt.get_osfhandle(stream.fileno()), 1 | (2 if exclusive else 0), 0, 1, 0,
                             ctypes.byref(overlap)):
        code = ctypes.get_last_error()
        if code in (32, 33):
            raise BlockingIOError("State home lock is busy")
        raise ctypes.WinError(code)
    return overlap


def _home_os_unlock(stream, overlap):
    if os.name != "nt":
        import fcntl
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        return
    import ctypes
    from ctypes import wintypes
    import msvcrt
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.UnlockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                   wintypes.DWORD, ctypes.c_void_p]
    kernel.UnlockFileEx.restype = wintypes.BOOL
    if not kernel.UnlockFileEx(msvcrt.get_osfhandle(stream.fileno()), 0, 1, 0, ctypes.byref(overlap)):
        raise ctypes.WinError(ctypes.get_last_error())


@contextmanager
def home_lock(home=None, *, exclusive=False, timeout=5, allow_moved=False):
    """Hold shared activity or exclusive migration ownership across processes.

    Activity ownership lives inside .locks. A permanent legacy parent lock is
    also acquired (read-only handle is sufficient for OS byte-range locks), so
    older processes cannot split ownership. Never replace either lock inode.
    """
    if timeout < 0:
        raise ValueError("Lock timeout must be nonnegative")
    home = Path(home).expanduser().resolve() if home is not None else state_home().resolve()
    key = _normal_path(home)
    held = getattr(_LOCAL_LOCKS, "homes", None)
    if held is None:
        held = _LOCAL_LOCKS.homes = {}
    if key in held:
        if exclusive and not held[key]:
            raise ValueError("Cannot upgrade a shared state-home lock to migration ownership")
        yield home
        return
    deadline = time.monotonic() + timeout
    with _LOCKS_GUARD:
        gate = _HOME_GATES.setdefault(key, _ReaderWriterGate())
    gate.acquire(exclusive, deadline)
    streams = []
    try:
        name = ".agent-relay-lock-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:24] + ".lock"
        legacy = home.parent / name
        if not legacy.exists():
            try:
                home.parent.mkdir(parents=True, exist_ok=True)
                with legacy.open("xb") as initial:
                    initial.write(b"\0")
            except FileExistsError:
                pass
            except PermissionError as error:
                raise ValueError("Legacy compatibility lock is absent; initialize this state home outside "
                                 "the sandbox first. Internal-only locking would split old-process ownership") from error
        (home / ".locks").mkdir(parents=True, exist_ok=True)
        for path, mode in ((legacy, "rb"), (home / ".locks" / name, "a+b")):
            stream = path.open(mode)
            streams.append([stream, False, None])
            while True:
                try:
                    overlap = _home_os_lock(stream, exclusive)
                    streams[-1][1:] = [True, overlap]
                    break
                except BlockingIOError as error:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(f"State home is in use: {home}") from error
                    time.sleep(min(0.025, remaining))
        if not allow_moved:
            destination = moved_destination(home)
            if destination is not None:
                raise ValueError(f"State was migrated; use the new state home: {destination}")
            receipt_path = home / ".migration-receipt.json"
            receipt = _read_json(receipt_path, None)
            if receipt_path.exists() and (not _valid_migration_record(receipt) or receipt.get("phase") != "complete"
                                          or _normal_path(receipt["destination"]) != key):
                raise ValueError(f"State migration is incomplete at {home}; resume migrate-state before writing")
        held[key] = bool(exclusive)
        try:
            yield home
        finally:
            del held[key]
    finally:
        try:
            for stream, acquired, overlap in reversed(streams):
                try:
                    if acquired:
                        _home_os_unlock(stream, overlap)
                finally:
                    stream.close()
        finally:
            gate.release(exclusive)


class ProjectState:
    def __init__(self, root, home=None, identity_timeout=5):
        identity = project_identity(root, timeout=identity_timeout)
        self.root = Path(identity["root"])
        self.home = Path(home).expanduser().resolve() if home is not None else state_home()
        self.key = identity["key"]
        self.common_dir = identity["common_dir"]
        self.checkout_root = Path(identity["checkout_root"])
        self.path = self.home / "projects" / self.key

    def _path(self, name):
        name = Path(name)
        if name.is_absolute() or not name.parts or any(part == ".." for part in name.parts):
            raise ValueError("State name must be a relative path inside project state")
        path = self.path / name
        try:
            path.resolve().relative_to(self.path.resolve())
        except ValueError as error:
            raise ValueError("State path escapes project state") from error
        return path

    @contextmanager
    def lock(self, name="project", timeout=5):
        with home_lock(self.home, timeout=timeout):
            with self._project_lock(name, timeout):
                yield self

    @contextmanager
    def _project_lock(self, name="project", timeout=5):
        """Reentrant process/thread lock; the OS releases ownership after a crash.

        Keep lock files permanently: deleting them would let processes lock different
        inodes. Callers may lock the whole read/check/write transaction explicitly.
        """
        if not isinstance(name, str) or not name or timeout < 0:
            raise ValueError("Lock name must be nonempty and timeout nonnegative")
        digest = hashlib.sha256(name.encode("utf-8")).hexdigest()
        path = self._path(Path(".locks") / (digest + ".lock"))
        lock_key = _normal_path(path)
        with _LOCKS_GUARD:
            local_lock = _LOCKS.setdefault(lock_key, threading.RLock())
        deadline = time.monotonic() + timeout
        if not local_lock.acquire(timeout=timeout):
            raise TimeoutError(f"Timed out acquiring relay lock: {name}")
        depths = getattr(_LOCAL_LOCKS, "depths", None)
        if depths is None:
            depths = _LOCAL_LOCKS.depths = {}
        stream = None
        acquired = False
        try:
            if not depths.get(lock_key):
                path.parent.mkdir(parents=True, exist_ok=True)
                stream = path.open("a+b")
                stream.seek(0, os.SEEK_END)
                if stream.tell() == 0:
                    stream.write(b"\0")
                    stream.flush()
                while True:
                    try:
                        _os_try_lock(stream)
                        acquired = True
                        break
                    except OSError as error:
                        import errno
                        if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                            raise
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError(f"Timed out acquiring relay lock: {name}") from error
                        time.sleep(min(0.025, remaining))
            depths[lock_key] = depths.get(lock_key, 0) + 1
            try:
                yield self
            finally:
                depths[lock_key] -= 1
                if not depths[lock_key]:
                    del depths[lock_key]
        finally:
            if stream is not None:
                try:
                    if acquired:
                        _os_unlock(stream)
                finally:
                    stream.close()
            local_lock.release()

    def read_json(self, name, default=None):
        return _read_json(self._path(name), default)

    def write_json(self, name, data):
        with self.lock():
            atomic_json(self._path(name), data)

    def requests(self):
        requests = []
        for path in sorted(self._path("requests").glob("*.json")):
            if not ID_PATTERN.fullmatch(path.stem):
                continue
            # A damaged active request must block another send rather than vanish
            # from the ownership check. Only unrelated filenames are ignored.
            request = self.load_request(path.stem)
            if request is not None:
                requests.append(request)
        return requests

    def load_request(self, request_id):
        request_id = _request_id(request_id)
        request = self.read_json(Path("requests") / (request_id + ".json"), _MISSING)
        if request is _MISSING:
            return None
        if not isinstance(request, dict) or request.get("id") != request_id:
            raise ValueError(f"Invalid request record: {request_id}")
        return request

    def save_request(self, request):
        if not isinstance(request, dict):
            raise ValueError("Request must be an object")
        request_id = _request_id(request.get("id"))
        self.write_json(Path("requests") / (request_id + ".json"), request)

    def threads(self):
        threads = self.read_json("threads.json", {})
        if not isinstance(threads, dict) or any(
                not isinstance(key, str) or not isinstance(value, dict) or value.get("handle") != key
                for key, value in threads.items()):
            raise ValueError("Invalid threads.json: expected handle-to-thread mapping")
        return threads

    def register_thread(self, thread):
        if not isinstance(thread, dict):
            raise ValueError("Thread must be an object")
        handle = thread.get("handle")
        if not isinstance(handle, str) or not re.fullmatch(r"[a-z][a-z0-9_-]*:[^\s:]+", handle):
            raise ValueError("Thread handle must have the form agent:id")
        with self.lock():
            threads = self.threads()
            threads[handle] = {**threads.get(handle, {}), **thread}
            self.write_json("threads.json", threads)
        return threads[handle]
