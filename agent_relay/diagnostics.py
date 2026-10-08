"""Read-only, actionable diagnostics over existing state (no telemetry)."""
from datetime import datetime, timedelta, timezone
from contextlib import nullcontext, redirect_stderr
import io
import json
import math
import os
from pathlib import Path
import platform
import re
import shlex
import statistics
import subprocess
import sys

from . import __version__, collector, config, inbox, router
from .migration import pid_alive
from .state import ProjectState, state_warnings
from .state_transition import legacy_warnings


# October 7 preflight versions in docs/verification/README.md. These are
# historical observations, not a claim that every later live run used them.
VERIFIED_VERSIONS = {"codex": "0.153.4", "claude": "2.1.292", "antigravity": None}


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (AttributeError, TypeError, ValueError):
        return None


def quote(value):
    value = str(value)
    return "'" + value.replace("'", "''") + "'" if os.name == "nt" else shlex.quote(value)


def command(state, action):
    return f"agent-relay {action} --root {quote(state.root)}"


def collector_info(request, now=None):
    info = dict(request.get("collector") or {})
    alive = None
    if info.get("pid"):
        try:
            alive = pid_alive(info["pid"])
        except (OSError, ValueError):
            pass
    started = timestamp(info.get("started_utc"))
    limit = info.get("timeout", 0)
    expired = bool(started and isinstance(limit, (int, float)) and limit > 0 and
                   ((now or datetime.now(timezone.utc)) - started).total_seconds() > limit)
    return {"pid": info.get("pid"), "alive": alive, "expired": expired,
            "started_utc": info.get("started_utc"), "log": info.get("log"),
            "recorded": bool(info)}


def explain_request(state, request_id):
    request = state.load_request(request_id)
    if request is None:
        raise ValueError("request not found for this project; check --root")
    status = request.get("status")
    delivery = request.get("delivery") or {}
    accepted = ("accepted" if delivery.get("accepted") else
                "failed" if status == "send_failed" else
                "unknown" if status in {"dispatching", "delivery_unknown"} or delivery.get("unknown") else
                "accepted" if status in {"accepted", "completed", "failed"} else "not sent")
    info = collector_info(request)
    messages = [m for m in inbox.pending(state.path / "inbox", request=request_id,
                                        include_acknowledged=True) if m["kind"] in {"result", "error"}
                and m["recipient"] == request.get("return_to")]
    mail = next((m for m in messages if m["id"] == request.get("reply_id")), None)
    mail = mail or (messages[0] if messages else None)
    lead = router.record(state, mail["id"]) if mail else {}
    ack = (state.read_json(Path("inbox/acks") / (mail["id"] + ".json"), {})
           if mail and inbox._is_acknowledged(state.path / "inbox", mail) else {})
    wait = command(state, f"wait --request {quote(request_id)} --timeout 300")
    if ack:
        reason, action = "acknowledged", "No action required."
    elif mail and lead.get("status") == "delivery_unknown":
        reason = "lead delivery unknown"
        action = "Check whether the Lead already received this result before explicitly redelivering it."
    elif mail and lead.get("status") in {"failed", "pending"}:
        reason = "lead delivery " + lead["status"]
        action = command(state, f"inbox redeliver --id {quote(mail['id'])}")
    elif mail:
        reason = "result ready (review before ACK)"
        action = command(state, f"inbox ack --request {quote(request_id)} --for {quote(mail['recipient'])}")
    elif status == "send_failed":
        reason, action = "worker delivery failed", "Inspect the sender error before submitting a new task."
    elif status in {"completed", "failed"}:
        reason, action = "terminal result missing from inbox", "Inspect the request and inbox files for missing or damaged result data."
    elif info["expired"]:
        reason, action = "collector timeout elapsed (request remains open)", wait
    elif info["alive"] is False:
        reason, action = "collector dead: recover", wait
    elif not info["recorded"]:
        reason, action = "collector not recorded: recover", wait
    elif info["alive"] is None:
        reason, action = "collector liveness unknown: recover", wait
    else:
        reason, action = "still running", wait
    timeline = [
        {"stage": "created", "at": request.get("created_utc"), "status": "recorded"},
        {"stage": "worker delivery", "at": None, "status": accepted,
         "error": request.get("error") or delivery.get("stderr")},
        {"stage": "collector", "at": info["started_utc"], **info},
        {"stage": "result in inbox", "at": request.get("completed_utc"),
         "status": "present" if mail else "not recorded", "message_id": mail and mail["id"]},
        {"stage": "delivered to Lead", "at": None,
         "status": lead.get("status", "Monitor/recovery hook (receipt not recorded)" if mail and
                            mail["recipient"].startswith("claude:") else "not recorded")},
        {"stage": "ACKed", "at": ack.get("acknowledged_utc"), "status": "yes" if ack else "no"},
    ]
    return {"request_id": request_id, "status": status, "timeline": timeline,
            "next_reason": reason, "next_action": action}


def format_timeline(result):
    lines = [f"Request {result['request_id']} ({result['status']})"]
    for row in result["timeline"]:
        detail = row.get("status", "")
        if row["stage"] == "collector":
            detail = f"pid={row['pid']} alive={row['alive']} expired={row['expired']} log={row['log']}"
        if row.get("message_id"):
            detail += " message=" + row["message_id"]
        if row.get("error"):
            detail += " error=" + str(row["error"]).replace("\n", " ").replace("\r", " ")
        lines.append(f"  {row.get('at') or '(time not recorded)'} -> {row['stage']}: {detail}")
    lines.append(f"Next ({result['next_reason']}): {result['next_action']}")
    return "\n".join(lines)


def usage_stats(state, days=None, now=None):
    if days is not None and days <= 0:
        raise ValueError("days must be positive")
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days) if days else None
    requests = [r for r in state.requests() if cutoff is None or
                (timestamp(r.get("created_utc")) and timestamp(r["created_utc"]) >= cutoff)]
    counts = {"completed": 0, "failed": 0, "unknown": 0, "open": 0}
    durations, retries, unacked = [], 0, 0
    mail_by_request = {}
    for mail in inbox.pending(state.path / "inbox", include_acknowledged=True):
        if mail["kind"] in {"result", "error"}:
            mail_by_request.setdefault(mail["request_id"], []).append(mail)
    for request in requests:
        status = request.get("status")
        bucket = ("completed" if status == "completed" else "failed" if status in {"failed", "send_failed"}
                  else "unknown" if status in {"delivery_unknown", "dispatching"} else "open")
        counts[bucket] += 1
        start, end = timestamp(request.get("created_utc")), timestamp(request.get("completed_utc"))
        if status in {"completed", "failed"} and start and end and end >= start:
            durations.append((end - start).total_seconds())
        for mail in mail_by_request.get(request["id"], []):
            unacked += not inbox._is_acknowledged(state.path / "inbox", mail)
            retries += max(0, router.record(state, mail["id"]).get("attempts", 0) - 1)
    durations.sort()
    return {"days": days, "requests": len(requests), **counts,
            "latency_samples": len(durations),
            "median_seconds": statistics.median(durations) if durations else None,
            "p90_seconds": durations[math.ceil(len(durations) * .9) - 1] if durations else None,
            "additional_delivery_attempts": retries, "results_unacknowledged": unacked,
            "notes": ["Latency uses created_utc to completed_utc (confirmed result); p90 is nearest-rank.",
                      "Timed-out collections and recovery via wait are not persisted; counts unavailable.",
                      "Additional delivery attempts include automatic retries; explicit redeliveries cannot be distinguished.",
                      "Local state only; no telemetry. --days filters request creation time."]}


def queue_supported(executable):
    if not executable:
        return False
    try:
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        result = subprocess.run([executable, "queue", "--help"], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=15, **options)
        return result.returncode == 0 and "--thread" in result.stdout
    except (OSError, subprocess.TimeoutExpired):
        return False


def skill_status(agent):
    from .invocation import command_text, entry_args
    root = (Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") if agent == "codex"
            else Path.home() / ".claude")
    path = root / "skills/agent-relay/SKILL.md"
    try:
        content = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return "missing"
    current = command_text() in content or (len(entry_args()) == 1 and
              entry_args()[0].replace("\\", "/").lower() in content.replace("\\", "/").lower())
    return "points at this installation" if current else "installation reference differs or is unverified"


def hooks_present(agent, home):
    from . import hook_install as h
    path = h._config_path(agent, None)
    data = h._object(h._bytes(path), path)
    # Compare actual config, not merely an installer receipt. No installation or trust writes.
    return all(h._spec(agent, event, None, home)[0] in h._hook_groups(data, event, path)
               for event in h.EVENTS[agent])


def checklist(state, resolved, agents):
    rows = []

    def add(name, level, detail, action=None):
        rows.append({"check": name, "level": level, "detail": detail,
                     **({"next_action": action} if level != "OK" else {})})

    values = resolved["values"]
    add("Python", "OK" if sys.version_info >= (3, 10) else "FAIL", platform.python_version(),
        "Install Python 3.10 or newer.")
    parent = state.home
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    writable = parent.is_dir() and os.access(parent, os.W_OK)
    add("State home", "OK" if writable else "FAIL", str(state.home) +
        " (permission estimate; no write probe)", "Set AGENT_RELAY_HOME to a writable state directory.")
    by_agent = {a["agent"]: a for a in agents}
    codex = by_agent["codex"]
    add("Codex executable", "OK" if codex.get("installed") else "FAIL",
        codex.get("executable") or "not detected", "Set AGENT_RELAY_CODEX to the installed Codex executable.")
    add("Codex queue --thread", "OK" if queue_supported(codex.get("executable")) else "FAIL",
        "queue help capability probe", "Install a Codex build with queue --thread support.")
    enabled = values.get("agents", {}).get("antigravity", {}).get("enabled", False)
    if enabled:
        agy = by_agent["antigravity"]
        add("Antigravity", "OK" if agy.get("installed") else "FAIL", agy.get("executable") or "not detected",
            "Set AGENT_RELAY_ANTIGRAVITY to the installed Antigravity executable.")
        add("Antigravity hooks", "OK" if agy.get("capabilities", {}).get("hooks_installed") else "WARN",
            "relay hook registration", "agent-relay install-hooks --agents antigravity")
    for agent in ("claude", "codex"):
        try:
            detail = skill_status(agent)
        except (OSError, ValueError):
            detail = "cannot read skill"
        add(agent + " skill", "OK" if detail == "points at this installation" else "WARN", detail,
            "agent-relay install-skills")
    error = resolved.get("validation_error")
    configured = resolved.get("sources", {}).get("lead") != "builtin"
    add("Topology", "FAIL" if error else "OK" if configured else "WARN",
        error or (values["lead"].split(":")[0] + " -> " + ",".join(values["workers"]) +
                  ("" if configured else " (builtin default; not explicitly set)")),
        "Configure a supported Lead/worker combination with agent-relay use --root <checkout> --lead <agent:id> --workers <agents>.")
    lead = values["lead"].split(":")[0]
    if lead in {"claude", "codex"}:
        try:
            present = hooks_present(lead, state.home)
        except (OSError, ValueError):
            present = False
        add(lead + " recovery hooks", "OK" if present else "WARN", "registration for this installation/state home; execution/trust not verified",
            f"agent-relay install-hooks --agents {lead} --state-home {quote(state.home)}")
    for agent in agents:
        name = agent["agent"]
        if name == "antigravity" and not enabled:
            continue
        version = agent.get("version") or "unknown"
        match = re.search(r"\d+\.\d+\.\d+(?:[-+][\w.-]+)?", version)
        verified = VERIFIED_VERSIONS[name]
        tested = bool(verified and match and match[0] == verified)
        add(name + " version", "OK" if tested else "WARN", version +
            (" (historical preflight match)" if tested else " (untested version; recorded=" + str(verified or "unknown") + ")"),
            "agent-relay selftest" if name == "codex" else "Verify a small authorized live round trip for this app version.")
    for request in state.requests():
        if request.get("status") not in collector.OPEN_STATES:
            continue
        info = collector_info(request)
        healthy = info["alive"] is True and not info["expired"]
        add("Collector " + request["id"], "OK" if healthy else "WARN",
            f"pid={info['pid']} alive={info['alive']} timeout_elapsed={info['expired']}",
            command(state, f"wait --request {quote(request['id'])} --timeout 300"))
    counts = router.diagnostics(state, values["lead"])["delivery_counts"]
    add("Lead delivery", "WARN" if any(counts.values()) else "OK", json.dumps(counts),
        "Inspect unACKed results for each original Lead with " +
        command(state, "inbox list --for '<original-lead>'"))
    if lead == "claude":
        add("Claude Monitor", "WARN", "watch expires after 30 minutes; active Monitor cannot be observed",
            "Re-arm inbox watch in Claude Monitor after each expiry.")
    for index, warning in enumerate(state_warnings(state.home) + legacy_warnings(), 1):
        add(f"State warning {index}", "WARN", warning, "Review the selected AGENT_RELAY_HOME before recovery.")
    return rows


def bug_report(state, resolved, agents, rows):
    # Allowlist avoids copying free-form errors, commands, app stdout, IDs,
    # executable paths or project paths from diagnostic data into public reports.
    versions = {name: "unknown" for name in config.AGENTS}
    for agent in agents:
        match = re.search(r"\d+\.\d+\.\d+(?:-(?:alpha|beta|rc)\.\d+)?", agent.get("version") or "")
        versions[agent["agent"]] = match[0] if match else "unknown"
    # Preserve home-relative location only for the normal, non-project state home.
    normal = Path.home() / ".agent-relay"
    state_label = "~/.agent-relay" if state.home == normal else "<custom-state-home>"
    agent_name = lambda name: name if name in config.AGENTS else "unknown"
    return {"os": platform.system() + " " + platform.release(), "python": platform.python_version(),
            "agent_relay": __version__, "state_home": state_label,
            "app_versions": versions,
            "topology": {"lead": agent_name(resolved["values"]["lead"].split(":")[0]),
                         "workers": [agent_name(w) for w in resolved["values"]["workers"]]},
            "checks": [{"check": "Collector" if r["check"].startswith("Collector ") else r["check"],
                        "level": r["level"]} for r in rows]}


def doctor(args, adapter_for):
    state = ProjectState(args.root)
    resolved = config.resolve(args.root, validate=False)
    errors = io.StringIO()
    with redirect_stderr(errors) if args.report else nullcontext():
        agents = [adapter_for(a, resolved["values"]).detect() for a in ("codex", "claude", "antigravity")
                  if a != "antigravity" or resolved["values"]["agents"][a]["enabled"]]
        rows = checklist(state, resolved, agents)
    if args.report and errors.getvalue():
        rows.append({"check": "Diagnostic reader warnings", "level": "WARN"})
    if args.report:
        print("```json\n" + json.dumps(bug_report(state, resolved, agents, rows), indent=2) + "\n```")
    else:
        for row in rows:
            line = f"{row['level']} {row['check']}: {row['detail']}"
            if row.get("next_action"):
                line += " | Next: " + row["next_action"]
            print(line.replace("\n", " ").replace("\r", " "))
    return 5 if any(row["level"] == "FAIL" for row in rows) else 0
