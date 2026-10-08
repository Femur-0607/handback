"""Command interface and durable request lifecycle."""
import argparse
from contextlib import nullcontext, redirect_stdout, redirect_stderr
from datetime import datetime, timezone
import json
import io
import math
import os
from pathlib import Path
import sys
import time
import uuid

from . import collector, config, envelope, inbox
from .adapters.codex import CodexAdapter, cmd_selftest, resolve_codex, wait_reply
from .adapters.claude import ClaudeAdapter
from .adapters.antigravity import AntigravityAdapter
from .state import ProjectState, home_lock, state_home, state_warnings
from .state_transition import legacy_warnings, require_migrated_default


OPEN_STATES = collector.OPEN_STATES
finish_request = collector.finish_request


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def output(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def get_adapter(agent, values=None):
    settings = (values or {}).get("agents", {}).get(agent, {})
    executable = os.environ.get("HANDBACK_" + agent.upper()) or settings.get("executable")
    if agent == "codex":
        return CodexAdapter(executable=executable)
    if agent == "claude":
        return ClaudeAdapter(executable=executable)
    if agent == "antigravity":
        return AntigravityAdapter(executable=executable, model=settings.get("model"),
                                  delivery_timeout=settings.get("delivery_timeout", 60))
    raise ValueError("unknown agent: " + agent)


def handle(value, default="codex"):
    if ":" not in value:
        value = default + ":" + value
    agent, native_id = value.split(":", 1)
    if agent not in {"codex", "claude", "antigravity"} or not native_id.strip():
        raise ValueError("expected <agent>:<thread-id>")
    if any(char in value for char in ("\n", "\r", "\0")):
        raise ValueError("invalid thread handle")
    return value, agent, native_id


def lead_handle(values):
    lead = values["lead"]
    return lead if ":" in lead else lead + ":lead"


def resolve_for(args):
    return config.resolve(args.root)


def require_worker(resolved, agent):
    values = resolved["values"]
    # Validate policy independently of the caller's selected topology.
    config.validate_topology(resolved, values["lead"], [agent], values["fallback"])


def submit(state, resolved, target, body, label):
    target, agent, native_id = handle(target)
    # A provisional handle (Antigravity creates the conversation on first send)
    # resolves to its conversation once that first send has bound it.
    bound = state.threads().get(target, {}).get("bound_to")
    if bound:
        target, agent, native_id = handle(bound)
    require_worker(resolved, agent)
    adapter = get_adapter(agent, resolved["values"])
    request_id = uuid.uuid4().hex
    marker = "[relay " + request_id[:8] + "]"
    return_to = lead_handle(resolved["values"])
    if target == return_to:
        raise ValueError("Lead and worker must not have the same handle")
    text = f"{marker} From {label} through handback.\n{body}"
    outgoing = envelope.make(request_id, return_to, target, text, kind="request")
    request = {"schema": 1, "id": request_id, "marker": marker, "thread": native_id,
               "handle": target, "agent": agent, "return_to": return_to,
               "outgoing_id": outgoing["id"], "created_utc": utcnow(),
               "status": "prepared", "hop": 0, "root": str(state.root)}
    with state.lock():
        if any(item.get("handle") == target and item.get("status") in OPEN_STATES
               for item in state.requests()):
            raise ValueError("this worker already has an open request; use wait, do not resend")
        state.save_request(request)
        try:
            inbox.put(state.path / "inbox", outgoing)
        except Exception:
            request["status"] = "send_failed"
            request["error"] = "Outgoing envelope could not be stored; nothing was sent"
            state.save_request(request)
            raise
        request["status"] = "dispatching"
        state.save_request(request)
    # Disk state precedes I/O. A crash here retains an open request; never auto-replay.
    try:
        if getattr(adapter, "needs_context", False):
            title = state.threads().get(target, {}).get("name")
            delivery = adapter.deliver(native_id, outgoing, state=state, request=request, title=title)
        else:
            delivery = adapter.deliver(native_id, outgoing)
    except Exception as error:
        with state.lock():
            request = state.load_request(request_id)
            if request.get("status") in {"completed", "failed"}:
                return request, 0
            request["status"] = "delivery_unknown"
            request["error"] = str(error)
            state.save_request(request)
        return request, 4
    with state.lock():
        request = state.load_request(request_id)
        if request.get("status") in {"completed", "failed"}:
            return request, 0
        request["status"] = ("accepted" if delivery.get("accepted") else
                             "delivery_unknown" if delivery.get("unknown") else "send_failed")
        request["delivery"] = delivery
        if delivery.get("thread"):
            # The first send created the worker conversation: rebind to its real handle.
            real = agent + ":" + delivery["thread"]
            request.update(provisional_handle=request["handle"], handle=real, thread=delivery["thread"])
            previous = state.threads().get(target, {})
            state.register_thread({**previous, "handle": target, "bound_to": real})
            state.register_thread({**previous, "handle": real, "id": delivery["thread"],
                                   "provisional_handle": target, "created_utc": utcnow()})
        state.save_request(request)
    return request, 0 if delivery.get("accepted") else 4


def emit_result(result, return_file=None, summary=None):
    outcome = result["outcome"]
    if outcome == "completed":
        text = result.get("text") or ""
        if return_file:
            Path(return_file).write_text(text, encoding="utf-8")
        if summary is None:
            print(text)
        else:
            output({**summary, "status": "completed", "result": result})
        return 0
    if outcome == "timeout":
        if summary is not None:
            output({**summary, "status": "accepted", "result": result})
        print("Timed out; the request stays open. Use wait; do not resend.", file=sys.stderr)
        return 3
    if summary is not None:
        output({**summary, "status": "failed", "result": result})
    print(result.get("error") or result.get("text") or "Worker turn failed", file=sys.stderr)
    return 2


def collect(state, request, timeout, return_file, values, summary=None):
    with state.lock():
        request = state.load_request(request["id"])
        if request.get("status") == "prepared":
            # A live sender holds this same lock through the dispatching transition.
            # Seeing prepared after acquiring it means it stopped before delivery.
            request.update(status="send_failed", error="Sender stopped before dispatch")
            state.save_request(request)
    if request.get("status") in {"completed", "failed"}:
        request = finish_request(state, request, request["result"])
        summary = result_identity(request, summary)
        return emit_result(request["result"], return_file, summary)
    if request.get("status") == "send_failed":
        if summary is not None:
            output({**summary, "status": "send_failed"})
        print("Request was not accepted by the worker.", file=sys.stderr)
        return 4
    adapter = get_adapter(request["agent"], values)
    result = adapter.fallback_collect(request, timeout=timeout)
    completed = finish_request(state, request, result)
    summary = result_identity(completed, summary)
    return emit_result(completed.get("result", result), return_file, summary)


def result_identity(request, summary):
    message_id = request.get("reply_id")
    if message_id:
        if summary is not None:
            return {**summary, "message_id": message_id}
        # Preserve legacy plain-text stdout and --return-file contents.
        print("Result message id: " + message_id, file=sys.stderr)
    return summary


def warn_read_only():
    print("Warning: Antigravity cannot enforce read-only; the restriction is only "
          "the brief's instruction.", file=sys.stderr)


def cmd_new(args):
    is_lead = getattr(args, "role", "worker") == "lead"
    initial = args.text is not None or args.file is not None
    if initial:
        if is_lead:
            raise ValueError("new --text/--file is for workers; Lead instructions are user input, not relay delegation")
        message_body(args)  # Reject invalid briefs before creating an orphan thread.
    resolved = config.resolve(args.cwd, validate=not is_lead)
    worker = args.worker or "codex"
    if worker == "auto":
        worker = resolved["values"]["workers"][0]
    if is_lead:
        requested_workers = getattr(args, "workers", None)
        resolved["values"]["workers"] = ([x.strip() for x in requested_workers.split(",") if x.strip()]
                                          if requested_workers else
                                          ["antigravity"] if worker == "codex" else resolved["values"]["workers"])
        config.validate_topology(resolved, worker + ":new", resolved["values"]["workers"],
                                 resolved["values"]["fallback"])
    else:
        require_worker(resolved, worker)
    if worker == "antigravity" and args.sandbox == "read-only":
        warn_read_only()
    adapter = get_adapter(worker, resolved["values"])
    extra = {"writable_roots": args.add_dir} if args.add_dir else {}
    if is_lead and worker == "codex":
        if args.sandbox != "workspace-write":
            raise ValueError("Codex Lead requires workspace-write for relay state")
        roots = list(args.add_dir) + [str(state_home())]
        if "antigravity" in resolved["values"]["workers"]:
            from .adapters.antigravity import gemini_home
            roots.append(str(gemini_home() / "config"))
        extra = {"writable_roots": list(dict.fromkeys(roots))}
    if extra and worker != "codex":
        raise ValueError("--add-dir is only supported for Codex threads")
    native_id = adapter.new_thread(args.cwd, args.name, args.sandbox, open_app=not args.no_open, **extra)
    state = ProjectState(args.cwd)
    if is_lead and worker == "antigravity":
        initialization = "Agent-relay Lead conversation created. Wait for a task; this is not user approval."
        delivery = adapter.deliver(native_id, {"body": initialization}, state=state,
                                   request={"id": uuid.uuid4().hex, "root": str(state.root)}, title=args.name)
        if not delivery.get("accepted") or not delivery.get("thread"):
            raise ValueError("Antigravity Lead creation failed or is unknown; inspect sidecar evidence before retrying")
        native_id = delivery["thread"]
    state.register_thread({"handle": worker + ":" + native_id, "agent": worker,
                           "id": native_id, "name": args.name,
                           "role": "lead" if is_lead else "worker",
                           "root": str(Path(args.cwd).resolve()), "created_utc": utcnow()})
    if is_lead:
        config.use_topology(args.cwd, worker + ":" + native_id, resolved["values"]["workers"])
    if initial:
        send_args = argparse.Namespace(**vars(args))
        send_args.root = args.cwd
        send_args.to = worker + ":" + native_id
        send_args.thread = None
        send_args.no_collect = False
        send_args.collect_timeout = collector.DEFAULT_COLLECT_TIMEOUT
        return cmd_send(send_args, created={"created_handle": send_args.to})
    print(worker + ":" + native_id if args.worker else native_id)
    return 0


def message_body(args):
    if args.file:
        path = Path(args.file).resolve()
        if not path.is_file():
            raise ValueError("brief file does not exist: " + str(path))
        body = "Read " + str(path) + " completely and follow it."
    else:
        body = args.text
    if not body or not body.strip():
        raise ValueError("message must not be empty")
    return body


def cmd_send(args, created=None):
    resolved = resolve_for(args)
    body = message_body(args)
    target = args.to or args.thread
    if target.startswith("antigravity:"):
        try:
            brief = Path(args.file).read_text(encoding="utf-8-sig", errors="replace") if args.file else body
        except OSError:
            # Inspecting a brief for a warning must not change path-based delivery.
            brief = "read-only"
        if any(word in brief.lower() for word in
               ("read-only", "read only", "read_only", "readonly", "읽기 전용", "읽기전용",
                "do not modify files", "no file changes", "파일을 수정하지")):
            warn_read_only()
    state = ProjectState(args.root)
    request, code = submit(state, resolved, args.to or args.thread, body, args.label)
    if getattr(args, "on_request", None):
        args.on_request(request)
    summary = ({**created, "handle": request["handle"], "id": request["thread"],
                "request_id": request["id"], "marker": request["marker"], "to": request["handle"]}
               if created is not None else None)
    if code:
        if summary is not None:
            output({**summary, "status": request["status"], "delivery": request.get("delivery"),
                    "error": request.get("error")})
        print("Worker delivery failed: " + (request.get("error") or
              str(request.get("delivery", {}).get("stderr") or request.get("delivery"))), file=sys.stderr)
        return code
    if args.no_wait:
        spawned = None
        from .router import sandboxed
        external_collection = request["return_to"].startswith("codex:") and sandboxed()
        if not args.no_collect and not external_collection:
            try:
                spawned = collector.spawn(state, request, timeout=args.collect_timeout)
            except OSError as error:
                # The request is accepted; inbox watch or wait can still collect it.
                print("Collector not started: " + str(error), file=sys.stderr)
        if args.thread:
            print(request["marker"])
        else:
            output({**(summary or {}), "request_id": request["id"], "marker": request["marker"],
                    "to": request["handle"], "status": request["status"],
                    "collector_pid": spawned and spawned["pid"]})
        return 0
    return collect(state, request, args.timeout, args.return_file, resolved["values"], summary)


def cmd_wait(args):
    state = ProjectState(args.root)
    resolved = config.resolve(args.root, validate=False)
    if args.request:
        request = state.load_request(args.request)
        if request is None:
            raise ValueError("request not found for this project; check --root")
    else:
        if not args.thread or not args.marker:
            raise ValueError("wait requires --request or both --thread and --marker")
        target, agent, native_id = handle(args.thread)
        request = next((item for item in state.requests()
                        if item.get("handle") == target and item.get("marker") == args.marker), None)
        if request is None:
            if agent != "codex":
                raise ValueError("legacy --marker wait only supports Codex")
            return wait_reply(native_id, args.marker, args.return_file, args.timeout)
    return collect(state, request, args.timeout, args.return_file, resolved["values"])


def cmd_collect(args):
    """Detached collector body: publish the confirmed result to the inbox."""
    state = ProjectState(args.root)
    request = state.load_request(args.request)
    if request is None:
        raise ValueError("request not found for this project; check --root")
    resolved = config.resolve(args.root, validate=False)
    print(f"{collector.utcnow()} collector pid={os.getpid()} request={args.request}", flush=True)
    code = collect(state, request, args.timeout, None, resolved["values"])
    print(f"{collector.utcnow()} collector exit={code}", flush=True)
    return code


def cmd_try(args):
    """One synchronous smoke task; all transport stays in new/send/collect."""
    started = time.monotonic()
    root = str(Path(args.root).resolve())
    quote = lambda value: "'" + value.replace("'", "''") + "'"
    suggested_lead = args.lead if args.lead.startswith("claude:") else "claude:lead"
    use = f"handback use --root {quote(root)} --lead {quote(suggested_lead)} --workers codex"
    report = {"status": "configuration_error", "acknowledged": False}
    fix = use
    try:
        if not Path(root).is_dir():
            fix = f"handback try --root '<existing-project-directory>' --lead {quote(args.lead)}"
            raise ValueError("Project directory does not exist")
        state = ProjectState(root)
        saved = state.read_json("topology.json", {})
        resolved = config.resolve(root, validate=False)
        values = resolved["values"]
        if saved:
            if "codex" not in values["workers"]:
                raise ValueError("Existing topology has no Codex worker; it was not changed")
            if any(values[key] != saved.get(key, values[key]) for key in ("lead", "workers", "fallback")):
                fix = "Remove-Item Env:HANDBACK_LEAD, Env:HANDBACK_WORKERS -ErrorAction SilentlyContinue"
                raise ValueError("Environment overrides the saved topology; clear overrides before trying")
        else:
            if (os.environ.get("HANDBACK_LEAD", args.lead) != args.lead or
                    os.environ.get("HANDBACK_WORKERS", "codex") != "codex"):
                fix = "Remove-Item Env:HANDBACK_LEAD, Env:HANDBACK_WORKERS -ErrorAction SilentlyContinue"
                raise ValueError("Environment conflicts with the trial topology; no topology was changed")
            resolved = config.resolve(root, overrides={"lead": args.lead, "workers": ["codex"]}, validate=False)
            values = resolved["values"]
        if any(not resolved["policy"]["user_enabled"].get(a) for a in ("claude", "codex")):
            fix = f"notepad {quote(str(state.home / 'config.json'))}"
        elif any(a not in resolved["policy"]["allowed_agents"] or not resolved["policy"]["project_enabled"].get(a, True)
                 for a in ("claude", "codex")):
            fix = f"notepad {quote(str(state.checkout_root / '.handback.json'))}"
        config.validate_topology(resolved, values["lead"], values["workers"], values["fallback"])
        if not values["lead"].startswith("claude"):
            raise ValueError("try requires Claude Lead -> Codex worker. " + config.SUPPORTED_COMBINATIONS)
        from .router import sandboxed
        if sandboxed():
            fix = f"handback try --root {quote(root)} --lead {quote(args.lead)}"
            raise ValueError("Codex queue cannot run inside an agent sandbox; run the fix in a normal terminal")
        fix = "$env:HANDBACK_CODEX = '<absolute-path-to-installed-Codex-executable>'"
        detection = get_adapter("codex", values).detect()
        if not detection.get("installed"):
            raise ValueError("Codex executable was not found. Install/open Codex and set its executable path")
        if not saved:
            fix = use
            config.use_topology(root, args.lead, ["codex"])
            effective = config.resolve(root)["values"]
            if effective["lead"] != args.lead or effective["workers"] != ["codex"]:
                raise ValueError("Environment overrides the selected topology; clear HANDBACK_LEAD/WORKERS")
        child = build_parser().parse_args([
            "new", "--cwd", root, "--name", "relay first task", "--worker", "codex",
            "--sandbox", "read-only", "--text",
            "Do not modify files or delegate. Reply with RELAY_OK in this conversation.",
            "--timeout", str(args.timeout)])
        child.on_request = lambda request: report.update(
            request_id=request["id"], handle=request["handle"], status=request["status"])
        captured, diagnostics = io.StringIO(), io.StringIO()
        with redirect_stdout(captured), redirect_stderr(diagnostics):
            code = cmd_new(child)
        report = {**json.loads(captured.getvalue()), "acknowledged": False}
        request = state.load_request(report["request_id"])
        if code == 0:
            mail = envelope.validate(inbox._read_json(state.path / "inbox" / (request["reply_id"] + ".json")))
            if (mail["id"] != request["reply_id"] or mail["request_id"] != request["id"]
                    or mail["recipient"] != request["return_to"] or mail["sender"] != request["handle"]
                    or mail["kind"] != "result" or "RELAY_OK" not in mail["body"]):
                report.update(status="failed", error="Result identity or RELAY_OK verification failed")
                code = 2
            elif not args.keep:
                report["acks"] = inbox.acknowledge_request(state.path / "inbox", request["id"], request["return_to"])
                report["acknowledged"] = True
        if code in (3, 4):
            report["recovery"] = f"handback wait --root {quote(root)} --request {request['id']} --timeout {args.timeout:g}"
        if code == 3:
            report["status"] = "timeout"
        if diagnostics.getvalue() and code not in (0, 3, 4):
            report.setdefault("error", diagnostics.getvalue().strip())
    except (ValueError, OSError, RuntimeError) as error:
        report.update(error=str(error))
        if report.get("request_id"):
            code = 4
            report["recovery"] = f"handback wait --root {quote(root)} --request {report['request_id']} --timeout {args.timeout:g}"
        else:
            report["fix"] = fix
            code = 5
    report["elapsed_seconds"] = round(time.monotonic() - started, 2)
    if args.json:
        output(report)
    else:
        if report.get("request_id"):
            print(f"Worker: {report['handle']}\nRequest: {report['request_id']}")
        print(f"Elapsed: {report['elapsed_seconds']:.2f}s")
        print("Result: " + (report.get("error") or report.get("result", {}).get("text") or report["status"]))
        print("ACK: " + ("done" if report["acknowledged"] else "kept (--keep)" if code == 0 else "not done"))
        if report.get("fix"):
            print("Fix: " + report["fix"])
        if report.get("recovery"):
            print("Recover (do not resubmit): " + report["recovery"])
    return code


def build_parser():
    parser = argparse.ArgumentParser(prog="handback", description="handback: durable local agent relay")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("codex")
    trial = sub.add_parser("try", help="run and acknowledge one read-only Codex smoke task")
    trial.add_argument("--root", default=".")
    trial.add_argument("--lead", default="claude:lead")
    trial.add_argument("--timeout", type=float, default=300)
    trial.add_argument("--keep", action="store_true", help="leave the result unacknowledged")
    trial.add_argument("--json", action="store_true")
    new = sub.add_parser("new")
    new.add_argument("--cwd", required=True)
    new.add_argument("--name", required=True)
    new.add_argument("--role", choices=["lead", "worker"], default="worker")
    new.add_argument("--workers", help="worker agents for a new Lead; Codex Lead defaults to antigravity")
    new.add_argument("--worker", choices=["auto", "codex", "claude", "antigravity"])
    new.add_argument("--sandbox", default="workspace-write", choices=["read-only", "workspace-write"])
    new.add_argument("--no-open", action="store_true")
    new.add_argument("--add-dir", action="append", default=[],
                     help="extra writable directory for a workspace-write Codex thread (repeatable)")
    initial = new.add_mutually_exclusive_group()
    initial.add_argument("--text")
    initial.add_argument("--file")
    new.add_argument("--no-wait", action="store_true")
    new.add_argument("--timeout", type=float, default=0, help="seconds; 0 waits indefinitely")
    new.add_argument("--return-file")
    new.add_argument("--label", default=os.environ.get("HANDBACK_LABEL", "the Lead"))
    send = sub.add_parser("send")
    target = send.add_mutually_exclusive_group(required=True)
    target.add_argument("--thread")
    target.add_argument("--to")
    source = send.add_mutually_exclusive_group(required=True)
    source.add_argument("--text")
    source.add_argument("--file")
    send.add_argument("--label", default=os.environ.get("HANDBACK_LABEL", "the Lead"))
    mode = send.add_mutually_exclusive_group()
    mode.add_argument("--wait", action="store_true")
    mode.add_argument("--no-wait", action="store_true")
    send.add_argument("--no-collect", action="store_true",
                      help="with --no-wait, do not start the detached collector")
    send.add_argument("--collect-timeout", type=float, default=collector.DEFAULT_COLLECT_TIMEOUT,
                      help="seconds the detached collector waits before leaving the request open")
    gather = sub.add_parser("collect", help="internal: detached collector started by send --no-wait")
    gather.add_argument("--request", required=True)
    wait = sub.add_parser("wait")
    wait.add_argument("--request")
    wait.add_argument("--thread")
    wait.add_argument("--marker")
    for command in (send, wait, gather):
        command.add_argument("--return-file")
        command.add_argument("--timeout", type=float, default=0, help="seconds; 0 waits indefinitely")
        command.add_argument("--root", default=".")
    dashboard = sub.add_parser("dashboard", help="status strip above the taskbar")
    dashboard.add_argument("--once", action="store_true", help="print the snapshot as JSON instead")
    dashboard.add_argument("--autostart", choices=["on", "off"], help="add or remove the Windows Startup shortcut")
    for name in ("status", "doctor", "use"):
        command = sub.add_parser(name)
        command.add_argument("--root", default=".")
        if name == "doctor":
            modes = command.add_mutually_exclusive_group()
            modes.add_argument("--json", action="store_true", help="legacy diagnostic JSON")
            modes.add_argument("--report", action="store_true", help="redacted bug-report block")
        if name == "status":
            command.add_argument("--stats", action="store_true")
            command.add_argument("--days", type=int, help="requests created within N days")
        if name == "use":
            command.add_argument("--lead", required=True)
            command.add_argument("--workers", required=True)
            command.add_argument("--fallback", choices=["ask", "next"], default="ask")
    explain = sub.add_parser("explain", help="read-only request lifecycle and next action")
    explain.add_argument("--root", default=".")
    explain.add_argument("--request", required=True)
    explain.add_argument("--json", action="store_true")
    mail = sub.add_parser("inbox")
    mail_sub = mail.add_subparsers(dest="inbox_command", required=True)
    route = sub.add_parser("route", help="internal: external router and collector recovery")
    route.add_argument("--root", required=True)
    route.add_argument("--conversation")
    for name in ("list", "watch", "ack", "redeliver"):
        command = mail_sub.add_parser(name)
        command.add_argument("--root", default=".")
        command.add_argument("--for", dest="recipient", required=name != "redeliver")
        if name == "ack":
            selector = command.add_mutually_exclusive_group(required=True)
            selector.add_argument("--id")
            selector.add_argument("--request")
        if name == "list":
            command.add_argument("--request")
        if name == "redeliver":
            command.add_argument("--id", required=True)
        if name == "watch":
            command.add_argument("--interval", type=float, default=1)
            command.add_argument("--timeout", type=float, default=0)
            command.add_argument("--once", action="store_true")
            command.add_argument("--idle-exit", type=float, default=0, metavar="SECONDS",
                                 help="exit after this long with no open requests and no new mail "
                                      "(the Claude Lead skill uses 1200)")
            command.add_argument("--no-collect", action="store_true",
                                 help="only print mail; do not collect this recipient's open requests")
    skills = sub.add_parser("install-skills", help="install packaged agent skills")
    skills.add_argument("--dry-run", action="store_true")
    skills.add_argument("--target-home", help="isolated home; never inspect host PATH or CODEX_HOME")
    test = sub.add_parser("selftest")
    test.add_argument("--agent", choices=["codex", "claude", "antigravity"], default="codex")
    for name in ("install-hooks", "uninstall-hooks"):
        command = sub.add_parser(name)
        command.add_argument("--agents", default="codex,claude")
        command.add_argument("--dry-run", action="store_true")
        command.add_argument("--state-home")
    cleanup = sub.add_parser("cleanup-sidecars")
    cleanup.add_argument("--dry-run", action="store_true")
    hook = sub.add_parser("hook")
    hook.add_argument("--agent", required=True, choices=["codex", "claude", "antigravity"])
    sidecar = sub.add_parser("antigravity-sidecar", help="internal: body of a relay-owned Antigravity sidecar")
    sidecar.add_argument("--job", required=True)
    hook.add_argument("--event", required=True)
    hook.add_argument("--state-home")
    migrate = sub.add_parser("migrate-state", help="copy idle state to the current state home without deleting the source")
    migrate.add_argument("--from", dest="source", required=True)
    migrate.add_argument("--dry-run", action="store_true")
    return parser


def main(argv=None):
    # Windows pipes must not decode UTF-8 CLI output using the local ANSI page.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "install-skills":
            from .skill_install import install
            for message in install(args.target_home, args.dry_run):
                print(message)
            return 0
        for name in ("timeout", "collect_timeout"):
            value = getattr(args, name, 0)
            if not math.isfinite(value) or value < 0:
                parser.error("--" + name.replace("_", "-") + " must be nonnegative and finite")
        home = getattr(args, "state_home", None) or state_home()
        warnings = state_warnings(home) + legacy_warnings()
        if args.command != "hook" and not (args.command == "doctor" and args.report):
            for warning in warnings:
                print("Warning: " + warning, file=sys.stderr)
        mutates = (args.command in {"try", "new", "send", "wait", "collect", "use", "route"}
                   or args.command == "inbox" and args.inbox_command != "list"
                   or args.command in {"install-hooks", "uninstall-hooks", "cleanup-sidecars"} and not args.dry_run)
        if mutates:
            require_migrated_default(explicit_home=getattr(args, "state_home", None))
        lock = home_lock(home, timeout=0.2 if args.command == "hook" else 5) if mutates else nullcontext()
        with lock:
            return dispatch(args, parser)
    except (ValueError, OSError, RuntimeError, TimeoutError) as error:
        if args.command == "hook":
            return 0
        if args.command == "doctor" and args.report:
            # Exception text can contain local paths, IDs, or configuration content.
            output({"check": "diagnostic collection", "level": "FAIL",
                    "error_type": type(error).__name__,
                    "next_action": "Run doctor locally to inspect the detailed error."})
            return 5
        if args.command == "doctor" and not args.json:
            print("FAIL Diagnostic collection: " + str(error).replace("\n", " ").replace("\r", " ") +
                  " | Next: Inspect local configuration and state files with doctor --json.")
            return 5
        if args.command == "try":
            result = {"status": "configuration_error", "error": str(error),
                      "fix": "Run handback try from a normal terminal with the correct HANDBACK_HOME"}
            if args.json:
                output(result)
            else:
                print(result["error"] + "\nFix: " + result["fix"], file=sys.stderr)
            return 5
        print(str(error), file=sys.stderr)
        return 4 if args.command == "send" else 5


def dispatch(args, parser):
    if args.command == "try":
        return cmd_try(args)
    if args.command == "route":
        from .router import external_run
        external_run(ProjectState(args.root), args.conversation)
        return 0
    if args.command == "cleanup-sidecars":
        from .adapters.antigravity import cleanup_sidecars
        output(cleanup_sidecars(state_home(), dry_run=args.dry_run))
        return 0
    if args.command == "migrate-state":
        from .migration import migrate_state
        output(migrate_state(args.source, dry_run=args.dry_run))
        return 0
    if args.command == "hook":
        # Hooks never block the host's normal completion, including malformed input.
        try:
            from .hooks import process
            source = getattr(sys.stdin, "buffer", sys.stdin)
            raw = source.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                return 0
            result = process(args.agent, args.event, json.loads(raw), home=args.state_home)
            if result is not None:
                output(result)
        except Exception:
            pass
        return 0
    if args.command in {"install-hooks", "uninstall-hooks"}:
        from . import hook_install
        from .adapters import antigravity
        agents = [name.strip() for name in args.agents.split(",") if name.strip()]
        result = {}
        if "antigravity" in agents:
            agents.remove("antigravity")
            home = Path(args.state_home).resolve() if args.state_home else state_home()
            action = antigravity.install_hooks if args.command == "install-hooks" else antigravity.uninstall_hooks
            result["antigravity"] = action(home, dry_run=args.dry_run)
        if agents:
            kwargs = {"agents": agents, "home": args.state_home, "dry_run": args.dry_run}
            if args.command == "install-hooks":
                result.update(hook_install.install(**kwargs))
            else:
                result.update(hook_install.uninstall(**kwargs))
        output(result)
        return 0
    if args.command == "antigravity-sidecar":
        from .adapters.antigravity import run_sidecar_job
        return run_sidecar_job(args.job)
    if args.command == "codex":
        values = config.resolve(".", validate=False)["values"]
        print(resolve_codex(get_adapter("codex", values).executable))
        return 0
    if args.command == "new":
        return cmd_new(args)
    if args.command == "send":
        return cmd_send(args)
    if args.command == "wait":
        return cmd_wait(args)
    if args.command == "collect":
        return cmd_collect(args)
    if args.command == "use":
        output(config.use_topology(args.root, args.lead,
                                   [x.strip() for x in args.workers.split(",") if x.strip()],
                                   args.fallback))
        return 0
    if args.command == "dashboard":
        from . import dashboard
        if args.autostart:
            output(dashboard.set_autostart(args.autostart == "on"))
        elif args.once:
            output(dashboard.snapshot())
        else:
            dashboard.main()
        return 0
    if args.command == "status":
        state = ProjectState(args.root)
        if args.days is not None and (not args.stats or args.days <= 0):
            parser.error("--days requires --stats and a positive integer")
        if args.stats:
            from .diagnostics import usage_stats
            output(usage_stats(state, args.days))
            return 0
        from .router import diagnostics
        resolved = config.resolve(args.root, validate=False)
        output({**config.resolve(args.root, validate=False), "project_key": state.key, "state_path": str(state.path),
                **diagnostics(state, resolved["values"]["lead"]),
                "warnings": state_warnings(state.home) + legacy_warnings(),
                "open_requests": [r for r in state.requests() if r.get("status") in OPEN_STATES],
                "collection": "Codex rollout task_complete; send --no-wait starts a detached collector "
                              "and inbox watch also collects for its recipient"})
        return 0
    if args.command == "doctor":
        if not args.json:
            from .diagnostics import doctor
            return doctor(args, get_adapter)
        from .adapters.antigravity import cleanup_sidecars
        resolved = config.resolve(args.root, validate=False)
        from .router import diagnostics
        output({"state_home": str(state_home()), "configuration": resolved,
                **diagnostics(ProjectState(args.root), resolved["values"]["lead"]),
                "warnings": state_warnings() + legacy_warnings(),
                "agents": [get_adapter(a, resolved["values"]).detect()
                           for a in ("codex", "claude", "antigravity")],
                "sidecar_cleanup": cleanup_sidecars(state_home(), dry_run=True),
                "limits": ["Codex hooks and Claude Desktop Monitor verified; Desktop global recovery hooks not fully verified",
                           "Claude worker unsupported; Codex Lead supports Antigravity workers only",
                           "Antigravity Lead deferred by user decision; implementation retained",
                           "Unknown Lead delivery requires explicit redeliver; no quota fallback"]})
        return 0
    if args.command == "explain":
        from .diagnostics import explain_request, format_timeline
        result = explain_request(ProjectState(args.root), args.request)
        if args.json:
            output(result)
        else:
            print(format_timeline(result))
        return 0
    if args.command == "inbox":
        state = ProjectState(args.root)
        path = state.path / "inbox"
        if args.inbox_command == "list":
            from .router import record
            output([{**m, "delivery": record(state, m["id"])}
                    for m in inbox.pending(path, args.recipient, args.request)])
        elif args.inbox_command == "ack":
            receipts = (inbox.acknowledge_request(path, args.request, args.recipient) if args.request
                        else [inbox.acknowledge(path, args.id, args.recipient)])
            output({"acknowledged": receipts})
        elif args.inbox_command == "redeliver":
            from .router import redeliver
            output(redeliver(state, args.id, args.recipient))
        else:
            if args.interval <= 0:
                parser.error("--interval must be positive")
            if args.idle_exit < 0:
                parser.error("--idle-exit must be nonnegative")
            from .watcher import Lease
            before_scan = None
            if not args.no_collect:
                values = config.resolve(args.root, validate=False)["values"]
                watch_collector = collector.WatchCollector(
                    state, args.recipient, lambda agent: get_adapter(agent, values),
                    log=state.path / "log" / "watch-collector.log")
                before_scan = watch_collector.poll
                is_active = lambda: watch_collector.active
            else:
                def is_active():
                    try:
                        return any(r.get("return_to") == args.recipient and r.get("status") in OPEN_STATES
                                   for r in state.requests())
                    except (OSError, ValueError):
                        return True
            lease = Lease(state.path, args.recipient)
            try:
                reason = inbox.watch(path, args.recipient, args.interval, args.timeout, args.once,
                                     before_scan=before_scan, idle_exit=args.idle_exit,
                                     is_active=is_active, heartbeat=None if args.once else lease.beat)
            finally:
                lease.release()
            if reason == "idle":
                print(f"inbox watch: no open requests or new mail for {args.idle_exit:g}s; stopped. "
                      "Unread mail stays in the inbox.", file=sys.stderr)
            elif reason == "superseded":
                print("inbox watch: a newer watcher for this recipient took over; stopped.", file=sys.stderr)
        return 0
    if args.agent == "codex":
        values = config.resolve(".", validate=False)["values"]
        return cmd_selftest(args, executable=get_adapter("codex", values).executable)
    output(get_adapter(args.agent).detect())
    print("Runtime transport is unverified for this adapter.", file=sys.stderr)
    return 5


if __name__ == "__main__":
    raise SystemExit(main())
