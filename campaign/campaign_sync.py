#!/usr/bin/env python3
"""Laptop side of the campaigns: pull every environment's results, push back
what the others learned, and run commands on the campaign daemons.

Environments are found through the CloudLab Portal API (cloudlab_api/): each
running experiment of this profile is one environment, named after its
NS_Local hardware type, reached through its test_host_0. Or list them in
campaign/envs.yaml:

    envs:
      rs620: {host: pc123.emulab.net, user: me}
      d710:  {host: pc456.emulab.net}

    python3 campaign/campaign_sync.py pull              # results -> leaderboard/campaign_data/<env>/
    python3 campaign/campaign_sync.py push              # findings + expiry -> each env's ~/campaign/
    python3 campaign/campaign_sync.py status            # campaign.py status on every env
    python3 campaign/campaign_sync.py start|stop [--env rs620]

`pull` then `python3 analysis/importance.py` rebuilds the cross-hardware
report and the shared findings that `push` sends to every environment's inbox.
"""
import argparse
import json
import os
import subprocess
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DATA = os.path.join(REPO, "leaderboard", "campaign_data")
ENVS_FILE = os.path.join(HERE, "envs.yaml")
REMOTE = "~/campaign"
PULL = ["ledger.jsonl", "state.json", "knobs_proposed.jsonl", "ALERT", "campaign.log",
        "experiments"]
SERVICE = "dns-campaign"


def discover():
    """{env: {host, user, expires}} from running CloudLab experiments."""
    sys.path.insert(0, os.path.join(REPO, "cloudlab_api"))
    import ops  # noqa: E402
    client = ops.make_client()
    envs = {}
    experiments = client.list_experiments()
    if isinstance(experiments, dict):
        experiments = experiments.get("experiments") or list(experiments.values())
    for exp in experiments or []:
        exp_id = exp.get("id") or exp.get("uuid") if isinstance(exp, dict) else exp
        try:
            info = ops.describe(client, exp_id)
        except Exception as e:  # noqa: BLE001 - one bad experiment should not stop the rest
            print(f"skipping experiment {exp_id}: {e}", file=sys.stderr)
            continue
        nodes = {n.get("client_id"): n for n in info["nodes"]}
        ns, tester = nodes.get("NS_Local"), nodes.get("test_host_0")
        if not ns or not tester or not tester.get("hostname"):
            continue
        record = info["experiment"] if isinstance(info["experiment"], dict) else {}
        envs[ns.get("hardware_type") or exp_id] = {
            "host": tester["hostname"],
            "user": record.get("creator") if record.get("creator") in tester["ssh_users"]
                    else tester.get("ssh_user"),
            "expires": record.get("expires") or record.get("expires_at"),
            "experiment": exp_id,
        }
    return envs


def load_envs(args):
    if os.path.exists(ENVS_FILE) and not args.discover:
        with open(ENVS_FILE) as f:
            envs = (yaml.safe_load(f) or {}).get("envs") or {}
    else:
        envs = discover()
    if args.env:
        envs = {k: v for k, v in envs.items() if k in args.env}
    if not envs:
        raise SystemExit("no environments found (CloudLab API or campaign/envs.yaml)")
    return envs


def ssh_base(env, key):
    target = f"{env['user']}@{env['host']}" if env.get("user") else env["host"]
    opts = ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new"]
    if key:
        opts += ["-i", key]
    return target, opts


def ssh(env, key, command, timeout=120):
    target, opts = ssh_base(env, key)
    return subprocess.run(["ssh", *opts, target, command], capture_output=True,
                          text=True, timeout=timeout)


def rsync(env, key, sources, dest, push=False):
    target, opts = ssh_base(env, key)
    shell = "ssh " + " ".join(opts)
    if push:
        cmd = ["rsync", "-a", "-e", shell, *sources, f"{target}:{dest}"]
    else:
        cmd = ["rsync", "-a", "--ignore-missing-args", "-e", shell,
               *[f"{target}:{REMOTE}/{s}" for s in sources], dest]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=3600)


def pull(envs, key):
    for name, env in envs.items():
        dest = os.path.join(DATA, name)
        os.makedirs(dest, exist_ok=True)
        result = rsync(env, key, PULL, dest + "/")
        state = "ok" if result.returncode == 0 else result.stderr.strip()[-300:]
        with open(os.path.join(dest, "env.json"), "w") as f:
            json.dump({k: v for k, v in env.items()}, f, indent=1, default=str)
        alert = os.path.join(dest, "ALERT")
        print(f"{name}: pull {state}" + (" -- ALERT present" if os.path.exists(alert) else ""))


def push(envs, key):
    shared = [os.path.join(DATA, n) for n in ("shared_findings.md", "shared_knobs.json")
              if os.path.exists(os.path.join(DATA, n))]
    for name, env in envs.items():
        ssh(env, key, f"mkdir -p {REMOTE}/inbox")
        if shared:
            result = rsync(env, key, shared, f"{REMOTE}/inbox/", push=True)
            print(f"{name}: findings {'ok' if result.returncode == 0 else result.stderr[-200:]}")
        if env.get("expires"):
            ssh(env, key, f"echo {env['expires']} > {REMOTE}/expires_at")
            print(f"{name}: expires_at {env['expires']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=["pull", "push", "status", "start", "stop"])
    parser.add_argument("--env", action="append", help="limit to this environment (repeatable)")
    parser.add_argument("--key", default=os.path.expanduser("~/.ssh/cloudlab"),
                        help="SSH private key (default ~/.ssh/cloudlab; ignored if missing)")
    parser.add_argument("--discover", action="store_true",
                        help="use the CloudLab API even if campaign/envs.yaml exists")
    args = parser.parse_args()
    key = args.key if os.path.exists(args.key) else None
    envs = load_envs(args)

    if args.action == "pull":
        pull(envs, key)
    elif args.action == "push":
        push(envs, key)
    else:
        command = {
            "status": "python3 /local/repository/campaign/campaign.py status",
            "start": f"systemctl --user start {SERVICE} && systemctl --user is-active {SERVICE}",
            "stop": "python3 /local/repository/campaign/campaign.py stop",
        }[args.action]
        for name, env in envs.items():
            result = ssh(env, key, command)
            print(f"===== {name} ({env['host']})\n{result.stdout}{result.stderr}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
