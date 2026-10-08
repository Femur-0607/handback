"""Install packaged skills without changing shared rules or app registration."""
from datetime import datetime
from importlib import resources
import json
import os
from pathlib import Path
import re
import shutil

from .invocation import command_text


def render(agent):
    templates = resources.files("handback").joinpath("skills")
    common = templates.joinpath("SKILL.md").read_text(encoding="utf-8").rstrip()
    content = common if agent == "claude" else templates.joinpath(agent, "SKILL.md.in").read_text(encoding="utf-8").replace("{{COMMON_SKILL}}", common)
    content = content.replace("{{HANDBACK}}", command_text())
    if re.search(r"\{\{[^}]+\}\}", content):
        raise ValueError(f"Unresolved template in {agent} skill")
    return content


def install(target_home=None, dry_run=False):
    isolated = target_home is not None
    home = Path(target_home).expanduser().resolve() if isolated else Path.home().resolve()
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
    codex = find("codex", "OpenAI/Codex/bin/codex.exe")
    if codex:
        root = home / ".codex"
        if not isolated and os.environ.get("CODEX_HOME"):
            root = Path(os.environ["CODEX_HOME"]).expanduser().resolve()
        messages.append(f"detected: codex {codex}; skill root {root / 'skills'}")
        write("codex", root / "skills/handback/SKILL.md")
    else:
        messages.append("skip: codex app not detected")
    antigravity = find("antigravity", "antigravity/Antigravity.exe")
    if antigravity:
        registration = home / ".gemini/config/skills.json"
        root = None
        if registration.is_file() and (not isolated or contained(registration)):
            settings = json.loads(registration.read_text(encoding="utf-8-sig"))
            for entry in settings.get("entries", []):
                value = str(entry.get("path", ""))
                path = home / value[2:].replace("\\", "/") if value.startswith(("~/", "~\\")) else Path(value)
                if not path.is_absolute():
                    continue
                path = path.resolve()
                if isolated and not contained(path):
                    messages.append(f"skip: registered skill path outside target home: {path}")
                    continue
                root = path
                break
        if root:
            messages.append(f"detected: antigravity {antigravity}; registered skill root {root}")
            write("antigravity", root / "handback/SKILL.md")
        else:
            messages.append(f"skip: antigravity global skill location unverified; inspect {registration}; template retained in handback/skills/antigravity")
    else:
        messages.append("skip: antigravity app not detected")
    return messages
