"""Layered settings with user availability and project policy kept immutable."""

from copy import deepcopy
import json
import os
from pathlib import Path

from .state import ProjectState


AGENTS = ("claude", "codex", "antigravity")
ROLES = ("lead", "worker")
REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
EXECUTION_KEYS = ("model", "reasoning_effort")
_UNSET = object()
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
        _validate_execution(settings, f"agents.{agent}")
    return value


def _validate_execution(settings, path):
    """Validate execution fields without interpreting arbitrary agent metadata."""
    for key in EXECUTION_KEYS:
        value = settings.get(key)
        if value is not None and (not isinstance(value, str) or not value.strip() or
                                  any(char in value for char in "\n\r\0")):
            raise ValueError(f"{path}.{key} must be a nonempty string or null")
    effort = settings.get("reasoning_effort")
    if effort is not None and effort not in REASONING_EFFORTS:
        raise ValueError(f"{path}.reasoning_effort must be one of: " + ", ".join(REASONING_EFFORTS))
    for role in ROLES:
        if role in settings:
            if not isinstance(settings[role], dict):
                raise ValueError(f"{path}.{role} must be an object")
            _validate_execution(settings[role], f"{path}.{role}")


def _execution_layer(agents):
    """Mutable topology may select execution defaults, never agent availability."""
    if not isinstance(agents, dict):
        raise ValueError("topology.json agents must be an object")
    layer = {}
    for agent, settings in agents.items():
        if agent not in AGENTS or not isinstance(settings, dict):
            raise ValueError(f"Invalid topology execution settings for {agent}")
        _validate_execution(settings, f"agents.{agent}")
        layer[agent] = {key: settings[key] for key in EXECUTION_KEYS if key in settings}
        for role in ROLES:
            if role in settings:
                layer[agent][role] = {key: settings[role][key] for key in EXECUTION_KEYS
                                      if key in settings[role]}
    return layer


def execution_settings(values, agent, role="worker", thread=None):
    """Select shared < role < saved thread settings; null inherits its fallback."""
    if agent not in AGENTS or role not in ROLES:
        raise ValueError("execution settings require a known agent and lead/worker role")
    settings = values.get("agents", {}).get(agent, {})
    _validate_execution(settings, f"agents.{agent}")
    saved = (thread or {}).get("execution", {})
    if not isinstance(saved, dict):
        raise ValueError("thread.execution must be an object")
    _validate_execution(saved, "thread.execution")
    selected = dict.fromkeys(EXECUTION_KEYS)
    for layer in (settings, settings.get(role, {}), saved):
        for key in EXECUTION_KEYS:
            if layer.get(key) is not None:
                selected[key] = layer[key]
    if agent == "claude" and any(selected.values()):
        raise ValueError("Claude model/reasoning control is unavailable; select it in Claude")
    if agent == "antigravity" and selected["reasoning_effort"] is not None:
        raise ValueError("Antigravity reasoning_effort control is unsupported")
    return selected


def configure_execution(root, agent, role, *, model=_UNSET, reasoning_effort=_UNSET, home=None):
    """Save project execution defaults outside the checkout; null restores inheritance."""
    if agent not in AGENTS or role not in ROLES:
        raise ValueError("configure requires a known agent and lead/worker role")
    changes = {key: value for key, value in (("model", model), ("reasoning_effort", reasoning_effort))
               if value is not _UNSET}
    if not changes:
        raise ValueError("configure requires model/reasoning settings or an explicit clear flag")
    _validate_execution(changes, f"agents.{agent}.{role}")
    state = ProjectState(root, home=home)

    def checked():
        resolved = resolve(root, home=state.home, validate=False)
        policy = resolved["policy"]
        if not policy["user_enabled"].get(agent, False):
            raise ValueError(f"Agent {agent} is disabled in user configuration")
        if agent not in policy["allowed_agents"]:
            raise ValueError(f"Agent {agent} is not allowed by project configuration")
        if not policy["project_enabled"].get(agent, True):
            raise ValueError(f"Agent {agent} is disabled in project configuration")
        candidate = deepcopy(resolved["values"])
        candidate.setdefault("agents", {}).setdefault(agent, {}).setdefault(role, {}).update(changes)
        execution_settings(candidate, agent, role)
        return resolved

    checked()  # Reject invalid selections before creating mutable state.
    with state.lock():
        checked()
        previous = state.read_json("topology.json", {})
        previous.setdefault("agents", {}).setdefault(agent, {}).setdefault(role, {}).update(changes)
        previous.setdefault("root", str(state.root))
        state.write_json("topology.json", previous)
    return resolve(root, home=state.home, validate=False)


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


def project_config_path(root):
    """Use legacy project policy only with explicit local migration guidance."""
    import sys
    current, legacy = Path(root) / ".handback.json", Path(root) / ".agent-relay.json"
    if legacy.exists():
        print(f"WARN Legacy project configuration: {legacy}; copy to {current}. "
              + ("The new file takes precedence." if current.exists() else "Reading legacy policy."),
              file=sys.stderr)
    return current if current.exists() or not legacy.exists() else legacy


def resolve(root, home=None, overrides=None, validate=True):
    """Resolve defaults < user < project < topology < environment < CLI."""
    state = ProjectState(root, home=home)
    user = _read_object(state.home / "config.json")
    project = _read_object(project_config_path(state.checkout_root))
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
    topology_layer = {key: topology[key] for key in ("lead", "workers", "fallback") if key in topology}
    topology_layer["agents"] = _execution_layer(topology.get("agents", {}))
    for layer, source in ((DEFAULTS, "builtin"), (user, "user"), (project, "project"),
                          (topology_layer, "topology")):
        _merge(values, layer, sources, source)
    environment = {}
    if "HANDBACK_LEAD" in os.environ:
        environment["lead"] = os.environ["HANDBACK_LEAD"]
    if "HANDBACK_WORKERS" in os.environ:
        environment["workers"] = [worker.strip() for worker in os.environ["HANDBACK_WORKERS"].split(",")
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
