"""Read-only discovery of the previous Windows default before opting into a new one."""
import os
from pathlib import Path
import sys

from .state import moved_destination, state_home


def legacy_homes():
    """Find state, never credentials, at the previous normal and MSIX defaults."""
    if sys.platform != "win32":
        return []
    profile = Path(os.environ.get("USERPROFILE") or Path.home())
    local = profile / "AppData" / "Local"
    candidates = [local / "agent-relay"]
    packages = local / "Packages"
    if packages.is_dir():
        candidates.extend(packages.glob("Claude_*/LocalCache/Local/agent-relay"))
    selected = state_home().resolve()
    result = []
    seen = set()
    for candidate in candidates:
        canonical = candidate.resolve()
        if canonical == selected or canonical in seen or not candidate.is_dir():
            continue
        seen.add(canonical)
        # A lock file alone is not meaningful state. A MOVED source is retained
        # intentionally and must not block use of its destination.
        try:
            if moved_destination(candidate) == selected:
                continue
        except (ValueError, OSError):
            pass  # A corrupt marker is not evidence that this state was migrated.
        if any((candidate / name).exists() for name in ("config.json", "projects", "hook-install")):
            result.append(canonical)
    return result


def legacy_warnings():
    if os.environ.get("AGENT_RELAY_HOME"):
        return []
    return [f"Unmigrated previous state: {path}. Use migrate-state --from \"{path}\" "
            "only after all requests and collectors finish. To continue existing work, "
            "set AGENT_RELAY_HOME to that path explicitly." for path in legacy_homes()]


def require_migrated_default(explicit_home=None):
    """Do not silently start a second store while an existing Lead still uses the old one."""
    if explicit_home is not None or os.environ.get("AGENT_RELAY_HOME"):
        return
    warnings = legacy_warnings()
    if warnings:
        raise ValueError("Default state has changed; refusing to split existing state. " + " ".join(warnings))
