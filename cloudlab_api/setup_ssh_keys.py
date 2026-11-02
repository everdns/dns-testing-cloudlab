#!/usr/bin/env python3
"""Let test_host_0 ssh to every other node of a CloudLab experiment.

For each experiment id this script, working from this machine over ssh:
  1. looks up the nodes through the Portal API;
  2. creates an ed25519 key on test_host_0 if it has none (an existing key is reused);
  3. appends that public key to ~/.ssh/authorized_keys on every node (skipped where present);
  4. adds every node's internal address to test_host_0's known_hosts;
  5. checks that test_host_0 can ssh to each other node without a prompt.

It is safe to run again. Your own ssh key must already be able to log in to the nodes
(register it on your CloudLab account; use --identity to pick a specific key file).

    python3 setup_ssh_keys.py <experiment_id> [<experiment_id> ...] [--identity ~/.ssh/key] [--dry-run]
"""
import argparse
import subprocess
import sys

import ops
from client import CloudLabError

SOURCE = "test_host_0"
KEY_PATH = "~/.ssh/id_ed25519"

# Appends the public key (substituted for KEY) only if that exact line is absent.
INSTALL_KEY = r"""set -e
umask 077
mkdir -p ~/.ssh && chmod 700 ~/.ssh
touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys
key='KEY'
if grep -qxF "$key" ~/.ssh/authorized_keys; then echo present; else echo "$key" >> ~/.ssh/authorized_keys; echo added; fi"""

ENSURE_KEY = r"""set -e
mkdir -p ~/.ssh && chmod 700 ~/.ssh
if [ ! -f KEY ]; then ssh-keygen -q -t ed25519 -N '' -C "SOURCE@$(hostname -s)" -f KEY; echo created >&2; fi
cat KEY.pub""".replace("KEY", KEY_PATH).replace("SOURCE", SOURCE)


def ssh_args(host, user, identity=None, timeout=20):
    args = ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={timeout}",
            "-o", "StrictHostKeyChecking=accept-new"]
    if identity:
        args += ["-i", identity, "-o", "IdentitiesOnly=yes"]
    return args + [f"{user}@{host}" if user else host]


def run_remote(host, user, script, identity=None):
    """Run a shell script on a node (sent on stdin); returns (returncode, stdout, stderr)."""
    p = subprocess.run(ssh_args(host, user, identity) + ["bash -s"], input=script,
                       capture_output=True, text=True, timeout=120)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def verify_script(targets):
    """Run on test_host_0: record host keys, then ssh to each target without a prompt."""
    lines = ["touch ~/.ssh/known_hosts"]
    for ip in targets:
        lines.append(f"ssh-keygen -F {ip} >/dev/null 2>&1 || ssh-keyscan -T 5 {ip} >> ~/.ssh/known_hosts 2>/dev/null")
    for ip in targets:
        lines.append(f'if h=$(ssh -o BatchMode=yes -o ConnectTimeout=8 {ip} hostname 2>/dev/null); '
                     f'then echo "{ip} OK $h"; else echo "{ip} FAIL"; fi')
    return "\n".join(lines)


def setup(client, experiment_id, identity=None, dry_run=False, runner=run_remote, out=print):
    info = ops.describe(client, experiment_id)
    nodes = info["nodes"]
    name = info["experiment"].get("name", experiment_id)
    by_id = {n["client_id"]: n for n in nodes}
    if SOURCE not in by_id:
        raise CloudLabError(f"{name}: no node named {SOURCE} (status {info['status']}); nodes: {sorted(by_id)}")
    user = info["experiment"].get("creator")
    if not all(n.get("hostname") for n in nodes):
        raise CloudLabError(f"{name}: not all nodes have a hostname yet (status {info['status']}); wait and retry")

    out(f"== {name} ({experiment_id}): {', '.join(sorted(by_id))}")
    if dry_run:
        out(f"   would create {KEY_PATH} on {SOURCE} if missing, then authorize it on every node as {user}")
        for n in nodes:
            out(f"   ssh {user}@{n['hostname']}  ({n['client_id']}, internal {n['ips']})")
        return True

    src = by_id[SOURCE]
    rc, pub, err = runner(src["hostname"], user, ENSURE_KEY, identity)
    if rc != 0 or not pub.startswith("ssh-ed25519 ") or "'" in pub or "\n" in pub:
        raise CloudLabError(f"{name}: cannot create/read the key on {SOURCE} ({src['hostname']}): {err or pub}")
    out(f"   {SOURCE}: key {'created' if 'created' in err else 'already present'} ({pub.split()[-1]})")

    ok = True
    for n in nodes:
        rc, res, err = runner(n["hostname"], user, INSTALL_KEY.replace("'KEY'", "'" + pub + "'"), identity)
        out(f"   {n['client_id']:<12} authorized_keys: {res if rc == 0 else 'FAILED ' + err}")
        ok &= rc == 0
    targets = [n["ips"][0] for n in nodes if n["client_id"] != SOURCE and n["ips"]]
    rc, res, err = runner(src["hostname"], user, verify_script(targets), identity)
    out("   from " + SOURCE + ": " + (res.replace("\n", "\n                  ") if rc == 0 else "FAILED " + err))
    ok &= rc == 0 and bool(targets) and all(line.split()[1] == "OK" for line in res.splitlines() if line.strip())
    return ok


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("experiment_ids", nargs="+")
    p.add_argument("--identity", help="private key file used to log in to the nodes from this machine")
    p.add_argument("--dry-run", action="store_true", help="show what would be done; touch nothing")
    args = p.parse_args(argv)
    client = ops.make_client()
    failed = 0
    for eid in args.experiment_ids:
        try:
            if not setup(client, eid, args.identity, args.dry_run):
                failed += 1
        except CloudLabError as e:
            print(f"error: {e}", file=sys.stderr)
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
