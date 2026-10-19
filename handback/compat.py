"""Notice app updates (Codex, Claude, Antigravity) and run a side-effect-free compatibility check.

handback depends on app internals with no stability guarantee (agentapi, sidecar
and project files, hook settings). An app update can break delivery silently, so
the dashboard compares each app's installed version with the last one that passed
these checks. A new version runs the checks once; a pass records the version as
verified, a failure leaves a copyable diagnostic summary for a GitHub issue.

Every check only reads files or runs ``--help``/``--version``. Nothing is sent to
any agent and no user file is written.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess

from . import __version__
from .state import _read_json, atomic_json, state_home

RECORD = "app-compat.json"
AGENTS = ("codex", "claude", "antigravity")
NAMES = {"codex": "Codex", "claude": "Claude", "antigravity": "Antigravity"}
# Same shape doctor matches (diagnostics.VERSION_PATTERN): a prerelease bump is an update too.
NUMBER = re.compile(r"\d+\.\d+\.\d+(?:[-+][\w.-]+)?")
ISSUE_URL = "https://github.com/Femur-0607/handback/issues/new"
LOG_LINES = 8
LOG_KEYWORDS = re.compile(r"sidecar|agentapi|project|hook|\[error\]|\bE\d{4} ", re.I)
# Errors every launch logs regardless of handback (no WSL installed, ...).
NOISE = re.compile(r"wsl", re.I)
# Values that must never reach a public issue even after the home path is shortened.
SECRETS = re.compile(r"((?:csrf_token|token|api[_-]?key|authorization)[\s=:\"']+)[^\s\"',]+", re.I)


def _options():
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def _run(command, timeout=15):
    """(returncode, stdout+stderr); (None, error text) when it cannot start."""
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=timeout, **_options())
    except (OSError, subprocess.TimeoutExpired) as error:
        return None, str(error)
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def number(text):
    match = NUMBER.search(str(text or ""))
    return match[0] if match else None


# ---- installed versions ---------------------------------------------------------------
def codex_version():
    from .adapters.base import AdapterUnavailable
    from .adapters.codex import _version, resolve_codex
    try:
        return number(_version(resolve_codex()))
    except AdapterUnavailable:
        return None


def claude_version():
    executable = os.environ.get("HANDBACK_CLAUDE") or shutil.which("claude")
    if not executable:
        return None
    code, output = _run([executable, "--version"])
    return number(output) if code == 0 else None


def antigravity_version():
    from .discovery import antigravity_version as read
    return number(read())


VERSION_READERS = {"codex": codex_version, "claude": claude_version, "antigravity": antigravity_version}


def current_versions(readers=None):
    """{agent: "X.Y.Z" or None}; an app that is not installed reads as None."""
    versions = {}
    for agent, read in (readers or VERSION_READERS).items():
        try:
            versions[agent] = read()
        except Exception:  # one unreadable app must not hide the others
            versions[agent] = None
    return versions


# ---- side-effect-free checks ----------------------------------------------------------
def _row(name, ok, detail=""):
    return {"check": name, "ok": bool(ok), "detail": str(detail)}


def _hook_config(agent):
    """Parse the agent's hook settings the way install-hooks would, without writing."""
    from . import hook_install as h
    path = h._config_path(agent, None)
    try:
        data = h._object(h._bytes(path), path)
        for event in h.EVENTS[agent]:
            h._hook_groups(data, event, path)
    except (OSError, ValueError) as error:
        return _row("hook settings format", False, error)
    return _row("hook settings format", True, "readable" if path.exists() else "no settings file yet")


def check_codex():
    from .adapters.base import AdapterUnavailable
    from .adapters.codex import resolve_codex
    try:
        executable = resolve_codex()
    except AdapterUnavailable as error:
        return [_row("codex executable", False, error)]
    rows = [_row("codex executable", True, "found")]
    code, output = _run([executable, "queue", "--help"])
    rows.append(_row("queue --thread", code == 0 and "--thread" in output,
                     "supported" if code == 0 and "--thread" in output else "queue --help lacks --thread"))
    code, output = _run([executable, "app-server", "--help"])
    rows.append(_row("app-server", code == 0, "available" if code == 0 else "app-server --help failed"))
    rows.append(_hook_config("codex"))
    return rows


def check_claude():
    executable = os.environ.get("HANDBACK_CLAUDE") or shutil.which("claude")
    if not executable:
        return [_row("claude executable", False, "claude CLI not on PATH")]
    code, output = _run([executable, "--version"])
    rows = [_row("claude --version", code == 0 and number(output), number(output) or "no version printed")]
    rows.append(_hook_config("claude"))
    return rows


def check_antigravity(gemini=None):
    from .adapters import antigravity as a
    home = Path(gemini) if gemini else a.gemini_home()
    adapter = a.AntigravityAdapter(gemini=home)
    executable = adapter._agentapi_help()
    rows = []
    if executable is None:
        rows.append(_row("agentapi --help", False, "agentapi did not list new-conversation"))
    else:
        command = [executable] + ([] if Path(executable).stem.lower() == "agentapi" else ["agentapi"])
        code, output = _run(command + ["--help"])
        missing = [name for name in ("new-conversation", "send-message") if name not in output]
        rows.append(_row("agentapi --help", code == 0 and not missing,
                         "lists new-conversation, send-message" if not missing else "missing " + ", ".join(missing)))
    # config.json: {"sidecars": {name: {"enabled": bool, "projectId": str}}}
    try:
        value = a._read_json(home / "config" / "config.json", {}) or {}
        sidecars = value.get("sidecars", {}) if isinstance(value, dict) else None
        bad = (["config.json is not an object"] if sidecars is None else
               ["sidecars is not an object"] if not isinstance(sidecars, dict) else
               [name for name, entry in sidecars.items() if not isinstance(entry, dict) or
                not isinstance(entry.get("enabled", False), bool) or
                not isinstance(entry.get("projectId", ""), str)])
        rows.append(_row("sidecar registry schema", not bad,
                         f"{len(sidecars)} entries" if not bad else "unexpected: " + ", ".join(bad[:3])))
    except (OSError, ValueError) as error:
        rows.append(_row("sidecar registry schema", False, error))
    # Each sidecar folder: sidecar.json {"command": str, "args": [str]}
    bad = []
    for path in sorted((home / "config" / "sidecars").glob("*/sidecar.json")):
        try:
            manifest = a._read_json(path, {})
        except (OSError, ValueError):
            manifest = None
        if not (isinstance(manifest, dict) and isinstance(manifest.get("command"), str)
                and isinstance(manifest.get("args"), list)):
            bad.append(path.parent.name)
    rows.append(_row("sidecar manifest format", not bad, "ok" if not bad else "unexpected: " + ", ".join(bad[:3])))
    # Project files: projectResources.resources[].gitFolder.folderUri, which resolve_project reads.
    files = sorted((home / "config" / "projects").glob("*.json"))
    readable = [project for _, project in a._projects(home)]
    with_folder = [project for project in readable if a._project_roots(project)]
    ok = not files or bool(with_folder)
    rows.append(_row("project file format", ok,
                     f"{len(with_folder)}/{len(files)} projects name a git folder" if files else "no projects yet"))
    try:
        hooks = a._read_json(home / "config" / "hooks.json", {})
        rows.append(_row("hook settings format", hooks is None or isinstance(hooks, dict),
                         "readable" if isinstance(hooks, dict) else "hooks.json is not an object"))
    except (OSError, ValueError) as error:
        rows.append(_row("hook settings format", False, error))
    return rows


CHECKS = {"codex": check_codex, "claude": check_claude, "antigravity": check_antigravity}


def run_checks(agent, checks=None):
    try:
        return (checks or CHECKS)[agent]()
    except Exception as error:  # a crashing check is itself a compatibility failure
        return [_row("check crashed", False, f"{type(error).__name__}: {error}")]


# ---- record and update detection ------------------------------------------------------
def load_record(home=None):
    data = _read_json(Path(home or state_home()) / RECORD, {})
    return data if isinstance(data, dict) else {}


def scan(home=None, versions=None, checks=None, force=False, now=None):
    """Check every app whose version differs from the last checked one.

    Returns one result per app that was checked. ``notify`` is False only for a
    passing first sighting: there is no earlier version, so nothing changed.
    A failed version is not re-checked (and re-announced) on every poll; ``force``
    re-checks all installed apps, as the menu's manual check does.
    """
    versions = current_versions() if versions is None else versions
    record = load_record(home)
    stamp = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    results, changed = [], False
    for agent in AGENTS:
        version = versions.get(agent)
        entry = record.get(agent) if isinstance(record.get(agent), dict) else {}
        if version and not force and version == entry.get("verified") and entry.get("checked") != version:
            # Back on the verified version (a rollback): an older failure no longer applies.
            record[agent] = {key: value for key, value in entry.items() if key != "last"}
            record[agent]["checked"] = version
            changed = True
            continue
        if not version or (not force and version in (entry.get("verified"), entry.get("checked"))):
            continue
        rows = run_checks(agent, checks)
        ok = all(row["ok"] for row in rows)
        result = {"agent": agent, "version": version, "previous": entry.get("verified"), "ok": ok,
                  "checks": rows, "utc": stamp, "notify": bool(entry) or not ok}
        if not ok:
            result["log_lines"] = relevant_log_lines(agent)
        entry = {**entry, "checked": version, "last": result}
        if ok:
            entry["verified"] = version
            entry["verified_utc"] = stamp
        record[agent] = entry
        results.append(result)
    if results or changed:
        atomic_json(Path(home or state_home()) / RECORD, record)
    return results


def failures(home=None):
    """Last failed result per app, for the app's current checked version."""
    return [entry["last"] for agent in AGENTS
            for entry in [load_record(home).get(agent)]
            if isinstance(entry, dict) and isinstance(entry.get("last"), dict) and not entry["last"].get("ok")]


# ---- user-facing text -----------------------------------------------------------------
def headline(result):
    """'Antigravity 2.23.1 감지 — 호환성 점검 통과' (tray notification body)."""
    verdict = "통과" if result["ok"] else "실패"
    return f"{NAMES[result['agent']]} {result['version']} 감지 — 호환성 점검 {verdict}"


def _redact(line):
    line = SECRETS.sub(lambda m: m.group(1) + "<redacted>", line)
    home = str(Path.home())
    for variant in {home, home.replace("\\", "/"), home.replace("\\", "\\\\")}:
        line = re.sub(re.escape(variant), "~", line, flags=re.I)
    return line.strip()[:240]


def relevant_log_lines(agent, logs=None, limit=LOG_LINES):
    """Recent app log lines about sidecars, projects, hooks or errors (Antigravity only)."""
    if agent != "antigravity":
        return []
    from .discovery import _tail, antigravity_logs
    folder = Path(logs) if logs else antigravity_logs()
    lines = []
    for name in ("main.log", "language_server.log"):
        found = [line for line in _tail(folder / name, 128 * 1024).splitlines()
                 if LOG_KEYWORDS.search(line) and not NOISE.search(line)]
        lines.extend(f"{name}: {_redact(line)}" for line in found[-limit:])
    return lines[-limit * 2:]


def summary(result):
    """Markdown diagnostic summary for a GitHub issue; paths shortened, secrets removed."""
    name = NAMES[result["agent"]]
    lines = [f"### {name} {result['version']} 호환성 점검 실패", "",
             f"- handback: {__version__}",
             f"- OS: {platform.system()} {platform.release()}",
             f"- {name}: {result['version']} (마지막 검증 버전: {result.get('previous') or '없음'})",
             f"- 점검 시각(UTC): {result.get('utc', '')}", "", "#### 점검 결과"]
    for row in result["checks"]:
        lines.append(f"- {'OK  ' if row['ok'] else 'FAIL'} {row['check']}: {_redact(row['detail'])}")
    logs = result.get("log_lines") or []
    if logs:
        lines += ["", "#### 관련 로그", "```", *logs, "```"]
    lines += ["", "_handback 현황판이 자동으로 만든 요약입니다. 올리기 전에 내용을 확인하세요._"]
    return "\n".join(lines)


def issue_title(result):
    return f"{NAMES[result['agent']]} {result['version']}: 호환성 점검 실패"
