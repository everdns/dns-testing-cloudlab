"""Operations shared by the CLI (launch.py) and the MCP server (mcp_server.py)."""
import os
import time

import yaml

from client import CloudLabError, PortalClient, parse_nodes, ssh_command

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")
# Values of profile.py's name_server_software parameter.
SOFTWARE = ["bind", "powerdns-authoritative-server", "knotdns", "nsd", "unbound", "all", "none"]
READY_WORDS = ("ready",)
FAILED_WORDS = ("failed", "terminated", "error")


def load_config(path=None):
    with open(path or CONFIG_PATH) as f:
        return yaml.safe_load(f) or {}


def build_launch_body(config, name, **overrides):
    """Build the POST /experiments body. `overrides` entries that are None are ignored."""
    o = {k: v for k, v in overrides.items() if v is not None}
    project = o.get("project") or config.get("project")
    profile_name = o.get("profile_name") or config.get("profile_name")
    if not project:
        raise CloudLabError("project is not set (config.yaml `project` or --project)")
    if not profile_name:
        raise CloudLabError("profile_name is not set (config.yaml or --profile-name)")
    duration = o.get("duration", config.get("duration", 4))
    cap = config.get("max_duration")
    if cap and duration > cap:
        raise CloudLabError(f"duration {duration}h exceeds max_duration {cap}h in config.yaml")

    bindings = dict(config.get("parameters") or {})
    bindings.update({k: o[k] for k in ("num_testers", "name_server_software", "name_server_hardware",
                                       "tester_hardware", "allow_interswitch_links") if k in o})
    if bindings.get("name_server_software") not in SOFTWARE:
        raise CloudLabError(f"name_server_software must be one of {SOFTWARE}")
    if not 1 <= int(bindings.get("num_testers", 1)) <= 200:
        raise CloudLabError("num_testers must be between 1 and 200")

    # The portal passes profile parameters as strings ("1", "True"), as in its sample binding files.
    bindings = {k: ("True" if v is True else "False" if v is False else str(v)) for k, v in bindings.items()}

    body = {"name": name, "project": project, "profile_name": profile_name,
            "profile_project": o.get("profile_project") or config.get("profile_project") or project,
            "duration": duration, "bindings": bindings}
    refspec = o.get("refspec") or config.get("refspec")
    if refspec:
        body["refspec"] = refspec
    return body


def experiment_status(exp):
    """Best-effort status string from an experiment record (field name varies)."""
    if isinstance(exp, dict):
        for key in ("status", "state"):
            if isinstance(exp.get(key), str):
                return exp[key]
    return "unknown"


def describe(client, experiment_id):
    exp = client.get_experiment(experiment_id)
    result = {"experiment": exp, "status": experiment_status(exp), "nodes": []}
    try:
        for n in parse_nodes(client.manifests(experiment_id)):
            result["nodes"].append({**n, "ssh": ssh_command(n, exp.get("creator") if isinstance(exp, dict) else None)})
    except CloudLabError:
        pass  # manifest is absent until the experiment is provisioned
    return result


def wait_ready(client, experiment_id, timeout=3600, interval=30, sleep=time.sleep):
    deadline = time.monotonic() + timeout
    while True:
        info = describe(client, experiment_id)
        status = info["status"].lower()
        if any(w in status for w in READY_WORDS) and "not" not in status:
            return info
        if any(w in status for w in FAILED_WORDS):
            raise CloudLabError(f"experiment {experiment_id} is {info['status']}")
        if time.monotonic() >= deadline:
            raise CloudLabError(f"timed out after {timeout}s; last status: {info['status']}")
        sleep(interval)


def extend(client, config, experiment_id, hours, reason):
    cap = config.get("max_duration")
    if cap and hours > cap:
        raise CloudLabError(f"extension of {hours}h exceeds max_duration {cap}h in config.yaml")
    return client.extend(experiment_id, hours, reason)


def make_client():
    return PortalClient()
