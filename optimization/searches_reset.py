#!/usr/bin/env python3
"""Snapshot the name server before a batch of searches and put it back after
each one (used by run_searches.sh).

    python3 searches_reset.py snapshot --out ~/searches/pristine.json
    python3 searches_reset.py reset    --baseline ~/searches/pristine.json

reset restores named.conf.options and the host state (host_state.restore),
restarts the DNS service, and checks that the server answers. Exit codes:
0 reset and verified, 1 restored but a check failed, 3 could not restore.
campaign/campaign.py calls reset() with the adapters of every server software
it has used, so their configs are restored too.
"""
import argparse
import json
import logging
import os
import subprocess
import sys

import yaml

import host_state
from max_sustainable_qps import ssh_run
from search import OPTIMIZATION_DIR
from servers import ServerConfigError, get_adapter

log = logging.getLogger("searches_reset")

DEFAULT_CONFIG = os.path.join(OPTIMIZATION_DIR, "agent_search.yaml")
QUERY_NAME = "ns1.workbench.lan"
EXIT_CHECK_FAILED = 1
EXIT_RESTORE_FAILED = 3


def snapshot(server, out):
    baseline = host_state.snapshot(server, save_files=True)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w") as f:
        json.dump(baseline, f, indent=1)
    log.info("Pristine host state of %s written to %s", server, out)
    return 0


def restart_service(server, config):
    dns = config["dns_services"]
    service = config["dns_service"]
    ssh_run(server, f"bash {dns['stop_script']}", timeout=120, check=False)
    result = ssh_run(server, f"bash {dns['start_script']} {service}", timeout=120, check=False)
    return result.returncode == 0


def answers(server):
    result = subprocess.run(["dig", "+short", "+time=3", "+tries=2", f"@{server}", QUERY_NAME],
                            capture_output=True, text=True)
    return server in result.stdout.split()


def reset(server, config, baseline_path, adapters=None):
    """Restore the host and every adapter's base config (default: BIND),
    restart config["dns_service"] and check it answers."""
    with open(baseline_path) as f:
        baseline = json.load(f)
    adapters = adapters or [get_adapter("bind")]
    bases = {a.name: a.load_base() for a in adapters}

    try:
        drift, errors = host_state.restore(server, baseline)
        for adapter in adapters:
            adapter.restore_base(server, bases[adapter.name])
    except (ServerConfigError, host_state.HostStateError) as e:
        log.error("Could not restore %s: %s", server, e)
        return EXIT_RESTORE_FAILED
    if errors:
        log.warning("%d restore command(s) failed: %s", len(errors), errors[:5])

    failures = []
    if not restart_service(server, config):
        failures.append("DNS service did not restart")
    if not answers(server):
        failures.append(f"{server} does not answer {QUERY_NAME}")
    for adapter in adapters:
        if adapter.read_installed(server).strip() != bases[adapter.name].strip():
            failures.append(f"{adapter.config_path} differs from the base file")
    if drift:
        # Informational: sysfs/NIC values can differ in form after a JSON round trip.
        log.warning("%d item(s) differ from the pristine snapshot: %s",
                    len(drift), "; ".join(host_state.summarize(drift, limit=10)))

    if failures:
        for failure in failures:
            log.error("Reset check failed: %s", failure)
        return EXIT_CHECK_FAILED
    log.info("Reset of %s verified (answers %s, base config in place)", server, QUERY_NAME)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=["snapshot", "reset"])
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--out", help="snapshot: where to write the pristine snapshot")
    parser.add_argument("--baseline", help="reset: the snapshot written by 'snapshot'")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    with open(args.config) as f:
        config = yaml.safe_load(f)
    server = config["hosts"]["server"]

    if args.action == "snapshot":
        if not args.out:
            parser.error("snapshot needs --out")
        try:
            return snapshot(server, args.out)
        except host_state.HostStateError as e:
            log.error("%s", e)
            return 2
    if not args.baseline:
        parser.error("reset needs --baseline")
    return reset(server, config, args.baseline)


if __name__ == "__main__":
    sys.exit(main())
