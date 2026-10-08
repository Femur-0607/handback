"""Agent adapters; unverified capabilities fail explicitly."""

from .base import AdapterError, AdapterUnavailable, BaseAdapter
from .codex import CodexAdapter
from .claude import ClaudeAdapter
from .antigravity import AntigravityAdapter


def get_adapter(name, executable=None):
    adapters = {"codex": CodexAdapter, "claude": ClaudeAdapter, "antigravity": AntigravityAdapter}
    try:
        return adapters[name](executable=executable)
    except KeyError as exc:
        raise AdapterUnavailable(f"Unknown agent: {name}") from exc


__all__ = ["AdapterError", "AdapterUnavailable", "BaseAdapter", "CodexAdapter", "ClaudeAdapter",
           "AntigravityAdapter", "get_adapter"]
