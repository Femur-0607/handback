"""Validated, vendor-independent messages stored by handback."""

from datetime import datetime, timedelta, timezone
import json
import re
import uuid


MAX_BYTES = 64 * 1024
DEFAULT_MAX_HOPS = 4
ID_PATTERN = re.compile(r"[0-9a-f]{32}")
KINDS = frozenset({"request", "result", "question", "progress", "error"})
FIELDS = frozenset({
    "schema", "id", "request_id", "sender", "recipient", "kind", "body",
    "hop", "created_utc",
})


def encode(value):
    """Return the canonical UTF-8 representation used for size and deduplication."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def validate(value, max_hops=DEFAULT_MAX_HOPS):
    """Return *value*, or raise ValueError for a malformed or oversized envelope."""
    if type(max_hops) is not int or max_hops < 0:
        raise ValueError("max_hops must be a nonnegative integer")
    if not isinstance(value, dict) or set(value) != FIELDS:
        raise ValueError("envelope must contain exactly the schema 1 fields")
    if type(value["schema"]) is not int or value["schema"] != 1:
        raise ValueError("unsupported envelope schema")
    if not isinstance(value["id"], str) or not ID_PATTERN.fullmatch(value["id"]):
        raise ValueError("message id must be 32 lowercase hexadecimal characters")
    for field in ("request_id", "sender", "recipient", "kind", "body", "created_utc"):
        if not isinstance(value[field], str) or not value[field].strip():
            raise ValueError(field + " must be a nonempty string")
    if value["kind"] not in KINDS:
        raise ValueError("unsupported message kind")
    if type(value["hop"]) is not int or not 0 <= value["hop"] <= max_hops:
        raise ValueError("message hop limit exceeded or invalid hop")
    try:
        timestamp = datetime.fromisoformat(value["created_utc"].replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("created_utc must be an ISO 8601 UTC timestamp") from error
    if timestamp.tzinfo is None or timestamp.utcoffset() != timedelta(0):
        raise ValueError("created_utc must include the UTC timezone")
    try:
        encoded = encode(value)
    except (TypeError, ValueError, UnicodeError) as error:
        raise ValueError("envelope cannot be encoded as UTF-8 JSON") from error
    if len(encoded) > MAX_BYTES:
        raise ValueError("message exceeds 64 KiB; send a report path instead")
    return value


def make(request_id, sender, recipient, body, kind="result", hop=0, message_id=None):
    """Build a new schema 1 message without changing caller-provided content."""
    return validate({
        "schema": 1,
        "id": uuid.uuid4().hex if message_id is None else message_id,
        "request_id": request_id,
        "sender": sender,
        "recipient": recipient,
        "kind": kind,
        "body": body,
        "hop": hop,
        "created_utc": datetime.now(timezone.utc).isoformat(),
    })
