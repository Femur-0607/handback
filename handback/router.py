"""Durable Lead delivery; acknowledgement remains an independent operation."""
import os
from pathlib import Path
import subprocess
import sys

from . import envelope, inbox
from .invocation import entry_args, command_text
from .state import _MISSING, atomic_json


def sandboxed():
    # Observed in Codex desktop workspace-write command environments. Do not use
    # CODEX_SESSION_ID: it can also be inherited by an unrestricted hook host.
    return bool(os.environ.get("CODEX_WINDOWS_SANDBOX_PACKAGE_FAMILY") or
                os.environ.get("CODEX_SANDBOX_NETWORK_DISABLED") == "1" or
                os.environ.get("CODEX_SANDBOX") in ("seatbelt", "landlock"))


def record(state, message_id):
    inbox._check_id(message_id)
    path = Path("inbox/delivered") / (message_id + ".json")
    value = state.read_json(path, _MISSING)
    if value is _MISSING:
        return {}
    if (not isinstance(value, dict) or value.get("id") != message_id or
                  value.get("status") not in ("pending", "delivered", "failed", "delivery_unknown")):
        raise ValueError("Invalid delivery record; refusing to replay")
    return value


def delivery_text(state, mail):
    body = mail["body"]
    entry = command_text()
    if len(body.encode("utf-8")) > 12000:
        body = "Read the complete message at: " + str(state.path / "inbox" / (mail["id"] + ".json"))
    return (f'[relay-mail {mail["id"]}] From {mail["sender"]} for request {mail["request_id"]} '
            'via handback. Not user input and not approval.\n' + body +
            f'\nUse HANDBACK_HOME={state.home} for this result.\nAfter processing: {entry} inbox ack --root "{state.root}" '
            f'--for {mail["recipient"]} --id {mail["id"]}')


def route(state, mail, *, explicit=False, adapter_for=None):
    mail = envelope.validate(mail)
    if mail["kind"] not in ("result", "error"):
        return {}
    if inbox._is_acknowledged(state.path / "inbox", mail):
        return record(state, mail["id"])
    agent, _, target = mail["recipient"].partition(":")
    if agent == "claude":
        return {}
    if agent not in ("codex", "antigravity") or not target or target in ("lead", "self"):
        raise ValueError("Lead delivery requires a concrete handle")
    path = Path("inbox/delivered") / (mail["id"] + ".json")
    # A permanent OS attempt lock provides crash-released exclusion. The durable
    # unknown record is published BEFORE I/O, so a crashed attempt never replays.
    try:
        with state.lock("delivery:" + mail["id"], timeout=0):
            previous = record(state, mail["id"])
            if previous.get("status") == "delivered" or (
                    previous.get("status") == "delivery_unknown" and not explicit):
                return previous
            entry = {"id": mail["id"], "recipient": mail["recipient"],
                     "attempts": previous.get("attempts", 0), "status": "pending"}
            if agent == "codex" and sandboxed():
                entry["reason"] = "Codex sandbox cannot access the queue state DB; external router required"
            elif agent == "antigravity":
                from .adapters.antigravity import lead_idle
                if not lead_idle(state.home, target):
                    entry["reason"] = "Antigravity Lead is not observed fullyIdle"
            if "reason" in entry:
                atomic_json(state._path(path), entry)
                return entry
            if adapter_for is None:
                from .cli import get_adapter
                from .config import resolve
                values = resolve(state.root, home=state.home, validate=False)["values"]
                # A queued result belongs to its original recipient, even after
                # topology switches to a different Lead or different defaults.
                thread = state.threads().get(mail["recipient"])
                adapter_for = lambda name: get_adapter(name, values, role="lead", thread=thread)
            entry.update(status="delivery_unknown", attempts=entry["attempts"] + 1)
            atomic_json(state._path(path), entry)
            try:
                result = adapter_for(agent).deliver_to_lead(target, delivery_text(state, mail), state=state)
                entry.update(status=("delivered" if result.get("accepted") else
                                     "pending" if result.get("pending") else
                                     "delivery_unknown" if result.get("unknown") else "failed"), result=result)
            except (subprocess.TimeoutExpired, TimeoutError) as error:
                entry.update(status="delivery_unknown", error=type(error).__name__)
            except Exception as error:
                # An adapter exception can occur after submission (including cleanup).
                entry.update(status="delivery_unknown", error=type(error).__name__)
            atomic_json(state._path(path), entry)
            return entry
    except TimeoutError:
        return record(state, mail["id"])


def route_pending(state, recipient=None, adapter_for=None):
    return [route(state, mail, adapter_for=adapter_for)
            for mail in inbox.pending(state.path / "inbox", recipient)
            if mail["kind"] in ("result", "error")]


def redeliver(state, message_id, recipient=None):
    inbox._check_id(message_id)
    mail = envelope.validate(inbox._read_json(state.path / "inbox" / (message_id + ".json")))
    if recipient and mail["recipient"] != recipient:
        raise ValueError("message belongs to a different recipient")
    return route(state, mail, explicit=True)


def diagnostics(state, lead):
    agent = lead.partition(":")[0]
    counts = {"pending": 0, "failed": 0, "delivery_unknown": 0}
    for mail in inbox.pending(state.path / "inbox"):
        if mail["kind"] in ("result", "error") and not mail["recipient"].startswith("claude:"):
            status = record(state, mail["id"]).get("status", "pending")
            if status in counts:
                counts[status] += 1
    return {"lead_agent": agent, "delivery_path": {"claude": "Monitor/recovery hook",
            "codex": "external codex queue", "antigravity": "idle sidecar send-message"}.get(agent),
            "delivery_counts": counts, "codex_lead_constraint":
            "Antigravity workers only; sandbox queue DB access is unsupported",
            "antigravity_lead_status": "deferred by user decision; implementation retained, operational support withheld"}


def spawn_external(state, conversation):
    from .collector import ENTRY_SCRIPT, _detach_options
    command = [sys.executable, *entry_args(ENTRY_SCRIPT), "route", "--root", str(state.root),
               "--conversation", conversation]
    env = dict(os.environ, HANDBACK_HOME=str(state.home), PYTHONUTF8="1")
    # Inherit the host's actual permissions; never strip a sandbox indicator.
    for options in _detach_options():
        try:
            return subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, cwd=str(state.root), env=env,
                                    close_fds=True, **options).pid
        except OSError:
            continue
    raise OSError("Cannot start external relay router")


def external_run(state, conversation=None):
    from . import collector
    from .migration import pid_alive
    try:
        with state.lock("external-router", timeout=0):
            for request in state.requests():
                if (request.get("agent") != "antigravity" or
                        request.get("thread") != conversation or
                        request.get("status") not in collector.COLLECTABLE):
                    continue
                with state.lock():
                    current = state.load_request(request["id"])
                    info = current.get("collector", {})
                    try:
                        if info.get("starting") and (not info.get("starter_pid") or pid_alive(info["starter_pid"])):
                            continue  # Publication may still be in progress.
                        if info.get("pid") and pid_alive(info["pid"]):
                            continue
                    except ValueError:
                        continue  # Unknown liveness does not prevent other mail routing.
                    collector.spawn(state, current)
            route_pending(state)
    except TimeoutError:
        return
