"""Install packaged skills without changing shared rules or app registration."""
from datetime import datetime
from importlib import resources
import json
import os
from pathlib import Path
import re
import shutil

from .invocation import command_text
from .discovery import codex_bundled_candidates


def render(agent):
    templates = resources.files("handback").joinpath("skills")
    common = templates.joinpath("SKILL.md").read_text(encoding="utf-8").rstrip()
    content = common if agent == "claude" else templates.joinpath(agent, "SKILL.md.in").read_text(encoding="utf-8").replace("{{COMMON_SKILL}}", common)
    content = content.replace("{{HANDBACK}}", command_text())
    if re.search(r"\{\{[^}]+\}\}", content):
        raise ValueError(f"Unresolved template in {agent} skill")
    return content


def _home(target_home):
    isolated = target_home is not None
    return isolated, (Path(target_home).expanduser().resolve() if isolated else Path.home().resolve())


def _codex_root(home, isolated):
    if not isolated and os.environ.get("CODEX_HOME"):
        return Path(os.environ["CODEX_HOME"]).expanduser().resolve()
    return home / ".codex"


def _registered_skill_root(home, isolated, messages):
    """First absolute skill root registered in Antigravity's skills.json, or None."""
    registration = home / ".gemini/config/skills.json"
    if not registration.is_file() or (isolated and not registration.resolve().is_relative_to(home)):
        return None
    settings = json.loads(registration.read_text(encoding="utf-8-sig"))
    for entry in settings.get("entries", []):
        value = str(entry.get("path", ""))
        path = home / value[2:].replace("\\", "/") if value.startswith(("~/", "~\\")) else Path(value)
        if not path.is_absolute():
            continue
        path = path.resolve()
        if isolated and not path.is_relative_to(home):
            messages.append(f"skip: registered skill path outside target home: {path}")
            continue
        return path
    return None


def install(target_home=None, dry_run=False):
    isolated, home = _home(target_home)
    messages = []

    def contained(path):
        return path.resolve().is_relative_to(home)

    def find(name, relative):
        candidate = home / "AppData/Local/Programs" / relative
        if candidate.is_file() and (not isolated or contained(candidate)):
            return str(candidate)
        return None if isolated else shutil.which(name)

    def write(agent, target):
        if isolated and not contained(target):
            messages.append(f"skip: skill path outside target home: {target}")
            return
        content = render(agent)
        legacy_backup = None
        legacy = target.parent.parent / "agent-relay" / "SKILL.md"
        if legacy.is_file() and (not isolated or contained(legacy)):
            messages.append(f"legacy skill detected: {legacy}")
            if not dry_run:
                stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
                backup = legacy.with_name(f"SKILL.md.{stamp}.bak")
                with backup.open("xb") as stream:
                    stream.write(legacy.read_bytes())
                legacy_backup = backup
                messages.append(f"backup: {backup}; replaced by {target}")
        if dry_run:
            messages.append(f"dry-run: {agent} -> {target} (UTF-8 skill; existing file gets timestamp backup)")
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
            backup = target.with_name(target.name + f".{stamp}.bak")
            with backup.open("xb") as stream:
                stream.write(target.read_bytes())
            messages.append(f"backup: {backup}")
        target.write_text(content, encoding="utf-8")
        if legacy_backup is not None:
            legacy.unlink()  # remove the obsolete skill only after replacement succeeds
        messages.append(f"installed: {target}")

    write("claude", home / ".claude/skills/handback/SKILL.md")
    candidates = codex_bundled_candidates(home if isolated else None)
    codex = candidates[0] if candidates else (None if isolated else shutil.which("codex"))
    if codex:
        root = _codex_root(home, isolated)
        messages.append(f"detected: codex {codex}; skill root {root / 'skills'}")
        write("codex", root / "skills/handback/SKILL.md")
    else:
        messages.append("skip: codex app not detected")
    antigravity = find("antigravity", "antigravity/Antigravity.exe")
    if antigravity:
        registration = home / ".gemini/config/skills.json"
        root = _registered_skill_root(home, isolated, messages)
        if root:
            messages.append(f"detected: antigravity {antigravity}; registered skill root {root}")
            write("antigravity", root / "handback/SKILL.md")
        else:
            messages.append(f"skip: antigravity global skill location unverified; inspect {registration}; template retained in handback/skills/antigravity")
    else:
        messages.append("skip: antigravity app not detected")
    return messages


def _references_this_installation(text):
    marker = command_text()
    if os.name == "nt":  # Windows paths compare case-insensitively
        return marker.casefold() in text.casefold()
    return marker in text


def uninstall(target_home=None, dry_run=False):
    """Remove handback skill files written by install() that point at this installation.

    Files that do not reference this installation's command (user edits, another checkout
    or exe) and timestamp backups are left alone. Locations are checked even when the
    agent app is no longer installed, because the content check is what authorizes removal.
    """
    isolated, home = _home(target_home)
    messages = []

    def remove(agent, target):
        if isolated and not target.resolve().is_relative_to(home):
            messages.append(f"skip: skill path outside target home: {target}")
            return
        if not target.is_file():
            messages.append(f"skip: no {agent} skill at {target}")
            return
        text = target.read_text(encoding="utf-8", errors="replace")
        if not _references_this_installation(text):
            messages.append(f"skip: {target} does not reference this installation; kept")
            return
        if dry_run:
            messages.append(f"dry-run: would remove {target}")
            return
        target.unlink()
        messages.append(f"removed: {target}")
        try:
            target.parent.rmdir()  # only succeeds when no backups or other files remain
        except OSError:
            pass

    remove("claude", home / ".claude/skills/handback/SKILL.md")
    remove("codex", _codex_root(home, isolated) / "skills/handback/SKILL.md")
    try:
        root = _registered_skill_root(home, isolated, messages)
    except (OSError, ValueError) as error:
        root = None
        messages.append(f"skip: antigravity skill registration unreadable: {error}")
    if root:
        remove("antigravity", root / "handback/SKILL.md")
    else:
        messages.append("skip: antigravity skill location not registered")
    return messages
