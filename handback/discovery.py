"""Read-only app bundle discovery shared by adapters and skill installation."""
import glob
import os
import re
from pathlib import Path
import sys


def codex_bundled_candidates(target_home=None):
    """Newest bundle first; an explicit target home never consults host paths."""
    isolated = target_home is not None
    home = Path(target_home).resolve() if isolated else Path.home()
    roots = [home / "AppData/Local"]
    if not isolated:
        if os.environ.get("LOCALAPPDATA"):
            roots.append(Path(os.environ["LOCALAPPDATA"]))
        # App-container shells can redirect LOCALAPPDATA away from the installation.
        if os.environ.get("USERPROFILE"):
            roots.append(Path(os.environ["USERPROFILE"]) / "AppData/Local")
    candidates = []
    for root in roots:
        for base in (root / "OpenAI/Codex/bin", root / "Programs/OpenAI/Codex/bin"):
            candidates.extend(glob.glob(str(base / "*" / "codex.exe")))
            candidates.append(str(base / "codex.exe"))
    if sys.platform == "darwin":
        apps = [home / "Applications/Codex.app"]
        if not isolated:
            apps.append(Path("/Applications/Codex.app"))
        for app in apps:
            candidates.extend(str(app / "Contents/Resources" / name) for name in ("codex", "codex.exe"))
    unique = {}
    for path in candidates:
        if os.path.isfile(path) and (not isolated or Path(path).resolve().is_relative_to(home)):
            unique[os.path.normcase(os.path.abspath(path))] = path
    return sorted(unique.values(), key=os.path.getmtime, reverse=True)


def claude_desktop_app():
    """Path of an installed Claude desktop app, or None. Never runs the app.

    The desktop app's Code tab can be the Lead without a `claude` CLI on PATH.
    """
    roots = []
    for value in (os.environ.get("LOCALAPPDATA"),
                  os.environ.get("USERPROFILE") and os.path.join(os.environ["USERPROFILE"], "AppData", "Local")):
        if value and value not in roots:
            roots.append(value)
    candidates = []
    for root in roots:
        # Microsoft Store / MSIX install keeps a per-user package folder.
        candidates.extend(glob.glob(os.path.join(root, "Packages", "Claude_*")))
        # Older Squirrel installer.
        candidates.append(os.path.join(root, "AnthropicClaude", "claude.exe"))
    if sys.platform == "darwin":
        candidates += [str(Path.home() / "Applications/Claude.app"), "/Applications/Claude.app"]
    return next((path for path in candidates if os.path.exists(path)), None)


def _local_roots():
    roots = []
    for value in (os.environ.get("LOCALAPPDATA"),
                  os.environ.get("USERPROFILE") and os.path.join(os.environ["USERPROFILE"], "AppData", "Local")):
        if value and value not in roots:
            roots.append(value)
    return roots


def file_version(path):
    """X.Y.Z from a Windows executable's fixed version resource, or None. Never runs the file."""
    if os.name != "nt" or not os.path.isfile(path):
        return None
    import ctypes
    from ctypes import wintypes
    try:
        api = ctypes.WinDLL("version")  # private handle: argtypes stay local
        api.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
        api.GetFileVersionInfoSizeW.restype = wintypes.DWORD
        api.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
        api.GetFileVersionInfoW.restype = wintypes.BOOL
        api.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p),
                                       ctypes.POINTER(wintypes.UINT)]
        api.VerQueryValueW.restype = wintypes.BOOL
        size = api.GetFileVersionInfoSizeW(str(path), None)
        if not size:
            return None
        buffer = ctypes.create_string_buffer(size)
        if not api.GetFileVersionInfoW(str(path), 0, size, buffer):
            return None
        pointer, length = ctypes.c_void_p(), wintypes.UINT()
        if not api.VerQueryValueW(buffer, "\\", ctypes.byref(pointer), ctypes.byref(length)) or length.value < 16:
            return None
        # VS_FIXEDFILEINFO: signature, struct version, FileVersionMS, FileVersionLS, ...
        fixed = ctypes.cast(pointer, ctypes.POINTER(wintypes.DWORD * 4)).contents
        if fixed[0] != 0xFEEF04BD:
            return None
        return f"{fixed[2] >> 16}.{fixed[2] & 0xFFFF}.{fixed[3] >> 16}"
    except (AttributeError, OSError, ValueError):
        return None


def antigravity_logs():
    """Folder holding Antigravity's main.log and language_server.log."""
    base = os.environ.get("APPDATA") or os.path.join(str(Path.home()), "AppData", "Roaming")
    return Path(base) / "Antigravity" / "logs"


def _tail(path, size=512 * 1024):
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - size))
            return handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


# main.log names the version each launch: "Starting app (v2.23.1)" since 2.23,
# "--override_ide_version 2.15.1" on the language server command line before it.
_MAIN_LOG_VERSION = re.compile(r"Starting app \(v(\d+\.\d+\.\d+)\)|override_ide_version[ =](\d+\.\d+\.\d+)")
_SERVER_LOG_VERSION = re.compile(r"Language server version:\s*(\d+\.\d+\.\d+)")


def antigravity_log_version(logs=None):
    """Version of the most recent Antigravity launch, read from its logs; None when absent."""
    logs = Path(logs) if logs else antigravity_logs()
    found = [m.group(1) or m.group(2) for m in _MAIN_LOG_VERSION.finditer(_tail(logs / "main.log"))]
    if found:
        return found[-1]
    found = _SERVER_LOG_VERSION.findall(_tail(logs / "language_server.log"))
    return found[-1] if found else None


def antigravity_executable():
    for root in _local_roots():
        path = os.path.join(root, "Programs", "antigravity", "Antigravity.exe")
        if os.path.isfile(path):
            return path
    return None


def antigravity_version(logs=None):
    """Installed Antigravity version: the app's file version, else its last launch in the logs."""
    executable = antigravity_executable()
    return (executable and file_version(executable)) or antigravity_log_version(logs)
