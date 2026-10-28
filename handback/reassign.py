"""Move one recipient's open requests and unacknowledged results to another.

Open requests get the new ``return_to``; a collector rereads it under the
project lock before publishing, so a late result goes to the new address.
An unacknowledged result/error is copied to the new recipient, its request
is pointed at the copy, and only then is the original acknowledged for the
old one, so a crash can leave both readable but never neither. The copy has
a deterministic ID and the original timestamp, so a retry republishes
identical mail and finishes the move. Nothing is sent to a worker.
"""
from datetime import datetime, timezone
import uuid

from . import inbox
from .collector import OPEN_STATES
from .handoff import RESULT_KINDS


def copy_id(message_id, recipient):
    return uuid.uuid5(uuid.NAMESPACE_URL, f"handback-reassign:{message_id}:{recipient}").hex


def _note(request, source, target, now, **extra):
    history = request.get("reassigned")
    request["reassigned"] = [*(history if isinstance(history, list) else []),
                             {"from": source, "to": target, "utc": now, **extra}]


def reassign(state, source, target, request_id=None, dry_run=False):
    """Return what moved (or would move with ``dry_run``) from ``source`` to ``target``."""
    if source == target:
        raise ValueError("--from and --to are the same recipient")
    path = state.path / "inbox"
    now = datetime.now(timezone.utc).isoformat()
    moved = {"from": source, "to": target, "dry_run": dry_run, "requests": [], "mail": []}
    with state.lock():
        for request in state.requests():
            if request.get("return_to") != source or request.get("status") not in OPEN_STATES:
                continue
            if request_id is not None and request["id"] != request_id:
                continue
            moved["requests"].append({"id": request["id"], "status": request["status"]})
            if not dry_run:
                request["return_to"] = target
                _note(request, source, target, now)
                state.save_request(request)
        for mail in inbox.pending(path, source, request_id):
            if mail["kind"] not in RESULT_KINDS:
                continue
            copy = {**mail, "id": copy_id(mail["id"], target), "recipient": target}
            moved["mail"].append({"id": mail["id"], "request_id": mail["request_id"], "copy_id": copy["id"]})
            if dry_run:
                continue
            inbox.put(path, copy)
            try:
                request = state.load_request(mail["request_id"])
            except ValueError:
                request = None  # legacy task id or damaged record: move the mail alone
            if request is not None and request.get("reply_id") == mail["id"]:
                # Keep explain/try consistent: the live result is now the copy.
                request.update(return_to=target, reply_id=copy["id"])
                _note(request, source, target, now, reply_id=mail["id"])
                state.save_request(request)
            inbox.acknowledge(path, mail["id"], source)
    return moved
