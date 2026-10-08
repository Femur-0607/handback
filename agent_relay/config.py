"""Layered settings with user availability and project policy kept immutable."""

from copy import deepcopy
import json
import os
from pathlib import Path

from .state import ProjectState


AGENTS = ("claude", "codex", "antigravity")
SUPPORTED_COMBINATIONS = ("Supported: Claude Lead -> Codex and/or Antigravity; "
                          "Codex Lead -> Antigravity only.")
DEFAULTS = {
    "lead": "claude", "workers": ["codex"], "fallback": "ask",
    "agents": {"claude": {"enabled": True}, "codex": {"enabled": True},
               "antigravity": {"enabled": False}},
}


def _read_object(path):
    try:
        with Path(path).open(encoding="utf-8-sig") as stream:
            value = json.load(stream)
    except FileNotFoundError:
        return {}
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"Invalid configuration {path}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"Configuration must be an object: {path}")
    agents = value.get("agents", {})
    if not isinstance(agents, dict):
        raise ValueError(f"agents must be an object: {path}")
    for agent, settings in agents.items():
        if agent not in AGENTS or not isinstance(settings, dict):
            raise ValueError(f"Invalid agent settings for {agent}: {path}")
        if "enabled" in settings and not isinstance(settings["enabled"], bool):
            raise ValueError(f"agents.{agent}.enabled must be a boolean: {path}")
    return value


def _merge(target, layer, sources, source, prefix=""):
    for key, value in layer.items():
        dotted = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            if not isinstance(target.get(key), dict):
                target[key] = {}
            _merge(target[key], value, sources, source, dotted)
        else:
            target[key] = deepcopy(value)
            sources[dotted] = source


def _agent_name(lead):
    if not isinstance(lead, str) or not lead or any(char.isspace() for char in lead):
        raise ValueError("Lead must be an agent name or agent:id handle")
    name, separator, identifier = lead.partition(":")
    if name not in AGENTS or (separator and (not identifier or ":" in identifier)):
        raise ValueError("Lead must be claude, codex, antigravity, or an agent:id handle")
    return name


def validate_topology(config, lead, workers, fallback):
    """Validate every entry point against policy, even after env/CLI overrides."""
    lead_agent = _agent_name(lead)
    if not isinstance(workers, list) or not workers or any(
            not isinstance(worker, str) or worker not in AGENTS for worker in workers):
        raise ValueError("Workers must be a nonempty list of agent names")
    if len(set(workers)) != len(workers):
        raise ValueError("Worker agent names must be unique")
    if fallback not in ("ask", "next"):
        raise ValueError("Fallback must be ask or next")
    if fallback == "next":
        raise ValueError("Fallback next is not implemented; use ask")
    policy = config["policy"]
    for agent in [lead_agent, *workers]:
        if not policy["user_enabled"].get(agent, False):
            raise ValueError(f"Agent {agent} is disabled in user configuration")
        if agent not in policy["allowed_agents"]:
            raise ValueError(f"Agent {agent} is not allowed by project configuration")
        if not policy["project_enabled"].get(agent, True):
            raise ValueError(f"Agent {agent} is disabled in project configuration")
    if lead_agent in ("codex", "antigravity"):
        identifier = lead.partition(":")[2]
        if not identifier or identifier in ("lead", "self") or identifier.startswith("pending-"):
            raise ValueError("Lead requires a concrete agent:id handle; aliases are unavailable. " + SUPPORTED_COMBINATIONS)
    if lead_agent == "codex" and "codex" in workers:
        raise ValueError("Codex Lead supports Antigravity workers only: sandbox queue DB access is unavailable. " + SUPPORTED_COMBINATIONS)
    for worker in workers:
        if worker not in ("codex", "antigravity"):
            raise ValueError(f"{worker} worker is unavailable: application routing is unverified (Phase 0). " + SUPPORTED_COMBINATIONS)
    return {"lead": lead, "workers": list(workers), "fallback": fallback}


def resolve(root, home=None, overrides=None, validate=True):
    """Resolve defaults < user < project < topology < environment < CLI."""
    state = ProjectState(root, home=home)
    user = _read_object(state.home / "config.json")
    project = _read_object(state.checkout_root / ".agent-relay.json")
    topology = state.read_json("topology.json", {})
    if not isinstance(topology, dict):
        raise ValueError("topology.json must be an object")
    allowed = project.get("allowed_agents", list(AGENTS))
    if not isinstance(allowed, list) or any(agent not in AGENTS for agent in allowed):
        raise ValueError("Project allowed_agents must be a list of known agent names")
    user_enabled = {agent: user.get("agents", {}).get(agent, {}).get(
        "enabled", DEFAULTS["agents"][agent]["enabled"]) for agent in AGENTS}
    project_enabled = {agent: project.get("agents", {}).get(agent, {}).get("enabled", True)
                       for agent in AGENTS}
    values, sources = {}, {}
    for layer, source in ((DEFAULTS, "builtin"), (user, "user"), (project, "project"),
                          ({key: topology[key] for key in ("lead", "workers", "fallback") if key in topology},
                           "topology")):
        _merge(values, layer, sources, source)
    environment = {}
    if "AGENT_RELAY_LEAD" in os.environ:
        environment["lead"] = os.environ["AGENT_RELAY_LEAD"]
    if "AGENT_RELAY_WORKERS" in os.environ:
        environment["workers"] = [worker.strip() for worker in os.environ["AGENT_RELAY_WORKERS"].split(",")
                                  if worker.strip()]
    _merge(values, environment, sources, "environment")
    if overrides:
        if not isinstance(overrides, dict):
            raise ValueError("Configuration overrides must be an object")
        _merge(values, {key: value for key, value in overrides.items() if value is not None}, sources, "cli")
    # Report actual effective availability rather than a later layer's attempted bypass.
    for agent in AGENTS:
        if not user_enabled[agent]:
            values.setdefault("agents", {}).setdefault(agent, {})["enabled"] = False
            sources[f"agents.{agent}.enabled"] = "user" if "enabled" in user.get("agents", {}).get(agent, {}) else "builtin"
        elif not project_enabled[agent]:
            values["agents"][agent]["enabled"] = False
            sources[f"agents.{agent}.enabled"] = "project"
    result = {"values": values, "sources": sources,
              "policy": {"user_enabled": user_enabled, "allowed_agents": list(allowed),
                         "project_enabled": project_enabled}}
    try:
        validate_topology(result, values["lead"], values["workers"], values["fallback"])
    except ValueError as error:
        if validate:
            raise
        result["validation_error"] = str(error)
    return result


def use_topology(root, lead, workers, fallback="ask", home=None):
    """Save the requested topology outside the repository, preserving other state."""
    state = ProjectState(root, home=home)
    if lead == "antigravity:self":
        identifier = os.environ.get("ANTIGRAVITY_CONVERSATION_ID")
        if not identifier or any(c.isspace() or c in ":/\\\0" for c in identifier):
            raise ValueError("ANTIGRAVITY_CONVERSATION_ID is missing or invalid")
        lead = "antigravity:" + identifier
    requested = {"lead": lead, "workers": workers, "fallback": fallback}
    with state.lock():
        config = resolve(root, home=state.home, overrides=requested)
        topology = validate_topology(config, lead, workers, fallback)
        thread = state.threads().get(lead, {})
        if thread.get("role", "worker") == "worker" and thread.get("agent") in workers:
            raise ValueError("Lead cannot use the same handle as a registered worker")
        if any(r.get("handle") == lead and r.get("status") in
               ("prepared", "dispatching", "accepted", "delivery_unknown") for r in state.requests()):
            raise ValueError("Lead cannot use the same handle as an active worker")
        if lead.startswith("antigravity:"):
            from .adapters.antigravity import observe_lead
            observe_lead(state.home, lead.partition(":")[2])
        previous = state.read_json("topology.json", {})
        if not isinstance(previous, dict):
            raise ValueError("topology.json must be an object")
        state.write_json("topology.json", {**previous, **topology, "root": str(state.root)})
    return resolve(root, home=state.home)
