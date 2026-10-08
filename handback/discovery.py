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
