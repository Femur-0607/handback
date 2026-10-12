"""Read-only app bundle discovery shared by adapters and skill installation."""
import glob
import os
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
