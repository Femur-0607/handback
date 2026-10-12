"""In-app update for the Windows installer build: check, download, verify, run.

Only GitHub Releases of this repository are trusted. The installer is accepted
only when its SHA-256 matches the release's SHA256SUMS.txt, and it runs with
Inno Setup's silent flags plus /RELAUNCH=1 so the dashboard comes back.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.request

from . import __version__

REPO = "Femur-0607/handback"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
DOWNLOAD_PREFIX = f"https://github.com/{REPO}/releases/download/"
CHECK_INTERVAL = 24 * 60 * 60
TIMEOUT = 20
INSTALLER_FLAGS = ("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS", "/RELAUNCH=1")


class UpdateError(RuntimeError):
    pass


def parse_version(text):
    """'v0.3.4' -> (0, 3, 4); None for anything that is not a plain release."""
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", str(text or "").strip())
    return tuple(int(part) for part in match.groups()) if match else None


def is_newer(latest, current=__version__):
    new, old = parse_version(latest), parse_version(current)
    return bool(new and old and new > old)


def supported():
    """Only the frozen Windows build was installed by setup.exe and can replace itself."""
    from .invocation import frozen
    return sys.platform == "win32" and frozen()


def _get(url, timeout=TIMEOUT):
    request = urllib.request.Request(url, headers={
        "User-Agent": f"handback/{__version__}", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def latest_release(fetch=_get):
    """{"version", "installer", "sums"} for the latest release, or None if it has no installer."""
    data = json.loads(fetch(API_LATEST).decode("utf-8"))
    version = str(data.get("tag_name") or "").lstrip("v")
    if not parse_version(version):
        return None
    assets = {asset.get("name"): asset.get("browser_download_url") for asset in data.get("assets", [])
              if isinstance(asset, dict)}
    installer = assets.get(f"handback-{version}-setup.exe")
    sums = assets.get("SHA256SUMS.txt")
    if not (installer and sums and all(str(url).startswith(DOWNLOAD_PREFIX) for url in (installer, sums))):
        return None
    return {"version": version, "installer": installer, "sums": sums}


def check(fetch=_get, current=__version__):
    """The latest release when it is newer than this build, else None."""
    release = latest_release(fetch)
    return release if release and is_newer(release["version"], current) else None


def expected_hash(sums_text, name):
    for line in sums_text.splitlines():
        parts = line.strip().split()
        if len(parts) == 2 and parts[1].lstrip("*") == name and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            return parts[0].lower()
    raise UpdateError(f"SHA256SUMS.txt has no entry for {name}")


def download(release, folder, fetch=_get):
    """Save the installer into folder and verify it; returns its path."""
    name = f"handback-{release['version']}-setup.exe"
    want = expected_hash(fetch(release["sums"]).decode("utf-8", "replace"), name)
    data = fetch(release["installer"], timeout=300)
    got = hashlib.sha256(data).hexdigest()
    if got != want:
        raise UpdateError("downloaded installer does not match SHA256SUMS.txt")
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    partial = path.with_suffix(".part")
    partial.write_bytes(data)
    os.replace(partial, path)
    return path


def run_installer(path, popen=subprocess.Popen):
    """Start the installer detached; the caller exits so its files can be replaced."""
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    return popen([str(path), *INSTALLER_FLAGS], creationflags=flags, close_fds=True,
                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
