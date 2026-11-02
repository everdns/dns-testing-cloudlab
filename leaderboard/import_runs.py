#!/usr/bin/env python3
"""Turn search results saved under data/<env>/ into leaderboard entries.

One entry per environment and search: the best measurement whose load
generator delivered at least 99% of the requested queries. Each entry carries
the setup steps and the commands that apply the winning configuration.

    python3 import_runs.py [--data data] [--run-label run1]
"""
import argparse
import json
import os

import publish  # also puts optimization/ on sys.path
import bind_config
import system_config

HERE = os.path.dirname(os.path.abspath(__file__))
SEARCHES = {
    "1_agent_bounded": ("agent bounded", "agent_search.py --mode bounded --agent-backend claude-code --max-configs 20"),
    "2_coordinate_descent": ("coordinate descent", "grid_search.py --mode coordinate-descent --max-passes 3"),
    "3_agent_open": ("agent open", "agent_search.py --mode open --agent-backend claude-code --max-configs 20"),
}
PARAMS = ["udp-receive-buffer", "udp-send-buffer", "max-udp-size", "udp-conntrack", "rmem",
          "netdev-max-backlog", "netdev-budget", "udp-mem", "optmem-max", "named-nofile"]
# Per environment: load-generator NIC, how it was set to one RX queue, and whether
# a single load generator capped the result.
ENVS = {
    "r320": {"nic": "enp8s0d1", "threads": 16, "fix": "sudo ethtool -L enp8s0d1 rx 1 tx 1", "capped": False},
    "d710": {"nic": "eno4", "threads": 8, "fix": "sudo ethtool -L eno4 rx 1 tx 1", "capped": False},
    "rs620": {"nic": "enp4s0f0np0", "threads": 40, "fix": "sudo ethtool -X enp4s0f0np0 equal 1", "capped": True},
}
CAP_NOTE = {
    1: ("Lower bound. One load generator can send about 700k-780k QPS here, so the score is limited by "
        "the load generator, not necessarily by the name server. Measurements below 99% query fidelity are excluded."),
    2: ("Lower bound. Two load generators were used, but some higher-rate levels still delivered under 99% of the "
        "requested queries, so the load generators may still limit the score. Those measurements are excluded."),
}


OPEN_NOTE = ("These are the agent's literal commands. They can include node-specific paths, helper services "
             "(samplers, zone-file restore) and a reboot, so read them before running them on another machine.")


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def as_list(v):
    """Open-mode rows store these fields as JSON text in some result files."""
    if isinstance(v, str):
        return json.loads(v) if v.strip() else []
    return list(v or [])


def is_valid(r):
    fid = num(r.get("min_qps_fidelity_pct"))
    return r.get("status") == "ok" and num(r.get("max_qps_passed")) is not None and (fid is None or fid >= 99)


def setup_steps(env, script, clients):
    e = ENVS[env]
    steps = [
        {"title": "Launch the CloudLab experiment (1 name server + 2 load generators)", "commands": [
            f"python3 cloudlab_api/launch.py launch --name dns-{env} --num-testers 2 --server-software bind "
            f"--server-hw {env} --tester-hw {env}",
            "python3 cloudlab_api/launch.py wait <experiment_id>",
            "python3 cloudlab_api/launch.py status <experiment_id>   # node ssh commands"]},
        {"title": "On test_host_0: create an SSH key and authorize it on every node", "commands": [
            'ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -N "" -q',
            "cat ~/.ssh/id_ed25519.pub   # append this line to ~/.ssh/authorized_keys on NS_Local and test_host_1"]},
        {"title": "On test_host_0: generate the zone and load it on the name server", "commands": [
            "cd /local/repository/optimization && bash optimization_setup.sh"]},
        {"title": "On every load generator: receive replies on one RX queue (kxdpgun in generic mode reads queue 0 only)",
         "commands": [e["fix"]]},
        {"title": "Point the search config at this machine", "commands": [
            f"client_interface: {e['nic']}   # in agent_search.yaml / grid_search.yaml",
            f"threads: {e['threads']}",
            "max_sustainable_qps: {num_trials: 5, min_passes: 4, trial_duration: 30}"]},
        {"title": "Run the search (from test_host_0)", "commands": [
            f"cd /local/repository/optimization && python3 {script}"]},
    ]
    if clients > 1:
        extra = [
            "scp /local/repository/dnsperf_input 10.10.1.3:/tmp/ && ssh 10.10.1.3 "
            "'sudo install -m 644 /tmp/dnsperf_input /local/repository/dnsperf_input'",
            "hosts.clients: [10.10.1.2, 10.10.1.3]   # in agent_search.yaml and grid_search.yaml"]
        if env == "rs620":
            extra.insert(0, "ssh 10.10.1.3 'sudo ip link set dev enp65s0f0np0 down && sudo ip link set dev enp65s0f0np0 "
                            "name enp4s0f0np0 && sudo ip link set dev enp4s0f0np0 up'   # same NIC name on both clients")
        steps.insert(-1, {"title": "Use the second load generator too (test_host_1, 10.10.1.3)", "commands": extra})
    return steps


def params_optimize(row):
    config = {k: row[k] for k in PARAMS if k in row and str(row[k]) != "default"}
    system = {k: v for k, v in config.items() if system_config.is_system_parameter(k)}
    statements = [bind_config.render_statement(k, v) for k, v in config.items() if k not in system]
    steps = [
        {"title": "Host tuning (sysctls, firewall, limits)", "commands": system_config.render_commands(system)},
        {"title": "bind configuration", "commands": publish.bind_commands(statements)},
        {"title": "Restart the DNS service", "commands": [
            "sudo /local/repository/stop_dns_service.sh ns_bind", "sudo /local/repository/start_dns_service.sh ns_bind"]},
    ]
    return config, [s for s in steps if s["commands"]]


def open_optimize(row):
    statements = as_list(row.get("named_options"))
    config = {}
    for s in statements:
        parts = s.rstrip(";").split(None, 1)
        if len(parts) == 2:
            config[parts[0]] = int(parts[1]) if parts[1].isdigit() else parts[1]
    host = as_list(row.get("host_commands"))
    config["root commands"] = len(host)
    if row.get("reboot_after_apply"):
        config["reboot"] = True
    steps = [
        {"title": "Host changes (run as root on NS_Local)", "commands": host},
        {"title": "bind configuration", "commands": publish.bind_commands(statements)},
    ]
    if row.get("reboot_after_apply"):
        steps.append({"title": "Reboot the name server", "commands": ["sudo reboot"]})
    return config, [s for s in steps if s["commands"]]


def change_list(key, row, config):
    """Every change the winning measurement applied, one entry per change."""
    if key != "3_agent_open":
        return [f"{k} = {v}" for k, v in config.items()]
    out = [f"named.conf.options: {x}" for x in as_list(row.get("named_options"))]
    out += [f"root: {c}" for c in as_list(row.get("host_commands"))]
    if row.get("reboot_after_apply"):
        out.append("reboot the name server")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=os.path.join(HERE, "data"))
    ap.add_argument("--run-label", default="run1", help="suffix for entry ids")
    ap.add_argument("--clients", type=int, default=1, help="load generators used")
    ap.add_argument("--date", default="2026-10-09")
    ap.add_argument("--keep", action="store_true", help="do not rebuild results.js")
    args = ap.parse_args()

    for env, e in ENVS.items():
        for key, (label, script) in SEARCHES.items():
            with open(os.path.join(args.data, env, key + ".json")) as f:
                rows = json.load(f)
            valid = [r for r in rows if is_valid(r)]
            if not valid:
                print(f"{env} {label}: no valid measurement, skipped")
                continue
            best = max(valid, key=lambda r: num(r["max_qps_passed"]))
            config, optimize = (open_optimize if key == "3_agent_open" else params_optimize)(best)
            config = {"search": label, "load generators": args.clients, **config}
            entry = {
                "id": f"bind-{env}-{key}-{args.run_label}",
                "software": "bind", "version": "",
                "hardware": {"name": env},
                "config": config,
                "metrics": {"max_qps": int(num(best["max_qps_passed"])), "lower_bound": e["capped"]},
                "setup": setup_steps(env, script, args.clients), "optimize": optimize,
                "measured_with": {"load generators": args.clients, "tool": "kxdpgun (generic mode)",
                                  "per level": "5 trials x 30 s",
                                  "measurements in search": len(rows)},
                "changes": change_list(key, best, config),
                "notes": " ".join(filter(None, [CAP_NOTE[min(args.clients, 2)] if e["capped"] else "",
                                         OPEN_NOTE if key == "3_agent_open" else ""])),
                "date": args.date,
            }
            with open(os.path.join(HERE, "entries", entry["id"] + ".json"), "w") as f:
                json.dump(entry, f, indent=1)
            print(f"{env:6} {label:19} {entry['metrics']['max_qps']:>9,} QPS  ({len(valid)}/{len(rows)} valid)")
    publish.rebuild()


if __name__ == "__main__":
    main()
