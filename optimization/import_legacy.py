#!/usr/bin/env python3
"""Convert the saved batch-search results (leaderboard/data*/<env>/*.json,
from run_searches.sh) into ledger records, so earlier runs count in the
cross-hardware analysis and in the agents' history.

Grid and bounded rows are tagged through knobs.yaml's parameter aliases.
Open-mode rows only have free-form commands, so their knobs are inferred from
the commands with the patterns below (knobs_inferred=true). These runs had no
validity checks: records carry validity_checked=false and legacy=true.

    python3 optimization/import_legacy.py --out leaderboard/campaign_data/legacy/ledger.jsonl
    python3 analysis/importance.py --ledger leaderboard/campaign_data/legacy/ledger.jsonl
"""
import argparse
import glob
import json
import os
import re
import sys

import knobs as knobs_mod
from ledger import SCHEMA, _bool, _num

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEARCHES = {"1_agent_bounded": "agent_bounded", "2_coordinate_descent": "coordinate_descent",
            "3_agent_open": "agent_open"}

# (knob, pattern over the change's commands and statements, value or a
# function of the match). The first number group is the value when given.
PATTERNS = [
    ("rmem_max", r"net\.core\.rmem_max\s*=\s*(\d+)", None),
    ("wmem_max", r"net\.core\.wmem_max\s*=\s*(\d+)", None),
    ("netdev_max_backlog", r"netdev_max_backlog\s*=\s*(\d+)", None),
    ("netdev_budget", r"netdev_budget\s*=\s*(\d+)\D+netdev_budget_usecs\s*=\s*(\d+)",
     lambda m: f"{m.group(1)}/{m.group(2)}"),
    ("rp_filter_off", r"rp_filter\s*=\s*0", True),
    ("irqbalance_off", r"(stop|disable|mask)\s+irqbalance", True),
    ("irq_pin", r"smp_affinity", "sequential"),
    ("rps", r"rps_cpus", "all-cpus"),
    ("xps", r"xps_cpus", True),
    ("rx_coalesce_usecs", r"rx-usecs\s+(\d+)", None),
    ("rx_coalesce_frames", r"rx-frames\s+(\d+)", None),
    ("gro_off", r"\bgro\s+off", True),
    ("rx_ring", r"ethtool\s+-G\s+\S+\s+rx\s+\d+", "max"),
    ("nic_channels", r"ethtool\s+-L\s+\S+\s+(?:combined|rx)\s+(\d+)", None),
    ("qdisc", r"default_qdisc\s*=\s*(pfifo_fast|fq|noqueue)", lambda m: m.group(1)),
    ("governor_performance", r"performance\s*>\s*\S*scaling_governor|"
                             r"default_governor=performance", True),
    ("cstate_max", r"max_cstate=(\d+)", None),
    ("cstate_max", r"cpuidle/state\[?(\d)", lambda m: max(0, int(m.group(1)) - 1)),
    ("thp", r"transparent_hugepage(?:/enabled|=)\s*(always|madvise|never)|"
            r"echo\s+(always|madvise|never)\s*>\s*\S*transparent_hugepage",
     lambda m: m.group(1) or m.group(2)),
    ("mitigations_off", r"mitigations=off", True),
    ("udp_rcvbuf", r"udp-receive-buffer\s+(\d+)", None),
    ("udp_sndbuf", r"udp-send-buffer\s+(\d+)", None),
    ("reuseport", r"reuseport\s+(yes|no)", lambda m: m.group(1)),
    ("server_thread_pin", r"taskset|CPUAffinity", True),
    ("message_compression_off", r"message-compression\s+no", True),
    ("zone_statistics_off", r"zone-statistics\s+(none|no)", True),
    ("workers", r"named\b[^\n]*\s-n\s+(\d+)", None),
    ("conntrack", r"--notrack|NOTRACK", "notrack"),
    ("conntrack", r"ufw\s+(--force\s+)?disable", "ufw-off"),
]


def infer_knobs(catalog, text):
    found = {}
    for name, pattern, value in PATTERNS:
        if name in found:
            continue
        m = re.search(pattern, text)
        if not m:
            continue
        if callable(value):
            v = value(m)
        elif value is None:
            v = int(m.group(1))
        else:
            v = value
        try:
            v = catalog.normalize(name, v)
        except knobs_mod.KnobError:
            continue
        if v != catalog.knobs[name]["baseline"]:
            found[name] = v
    return found


def convert(path, run, env, search, catalog):
    with open(path) as f:
        rows = json.load(f)
    params = set()
    for row in rows:
        params |= {k for k in row if k in catalog.aliases}
    out = []
    for row in rows:
        if search == "agent_open":
            change = {k: json.loads(row.get(k) or "[]") for k in
                      ("named_options", "host_commands", "revert_commands")}
            change["label"] = row.get("label")
            change["reboot_after_apply"] = _bool(row.get("reboot_after_apply"))
            knobs = infer_knobs(catalog, "\n".join(change["named_options"]
                                                  + change["host_commands"]))
            inferred = True
        else:
            change = {k: row.get(k) for k in params}
            knobs = catalog.from_parameters(change)
            inferred = False
        fidelity = _num(row.get("min_qps_fidelity_pct"))
        out.append({
            "schema": SCHEMA, "ts": None, "env": env, "hardware": {}, "software": "bind",
            "service": "ns_bind", "build_id": "package", "phase": f"legacy_{search}",
            "experiment_id": f"legacy-{run}-{env}-{search}", "point_id": row.get("point_id"),
            "label": row.get("label"), "replicate_group": None,
            "repeat_of": row.get("repeat_of") or None,
            "hypothesis": row.get("hypothesis") or row.get("agent_reasoning") or None,
            "status": row.get("status"), "error": row.get("error") or None,
            "knobs": knobs, "knobs_inferred": inferred, "change": change,
            "max_qps_passed": _num(row.get("max_qps_passed")),
            "max_qps_tested": _num(row.get("max_qps_tested")),
            "hit_ceiling": _bool(row.get("hit_max_qps_ceiling")),
            "min_fidelity": fidelity,
            "client_limited": fidelity is not None and fidelity < 99.0,
            "validity": {}, "validity_checked": False, "legacy": True,
            "levels": [], "run_dir": row.get("run_dir"),
            "duration_s": _num(row.get("search_duration_s")),
        })
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default=os.path.join(REPO, "leaderboard", "campaign_data",
                                                      "legacy", "ledger.jsonl"))
    args = parser.parse_args()
    catalog = knobs_mod.load()
    records = []
    for data_dir in sorted(glob.glob(os.path.join(REPO, "leaderboard", "data*"))):
        if not os.path.isdir(data_dir):
            continue
        run = "run1" if os.path.basename(data_dir) == "data" else \
            os.path.basename(data_dir).replace("data_", "")
        for path in sorted(glob.glob(os.path.join(data_dir, "*", "*.json"))):
            name = os.path.basename(path)[:-5]
            if name not in SEARCHES:
                continue
            env = os.path.basename(os.path.dirname(path))
            converted = convert(path, run, env, SEARCHES[name], catalog)
            records += converted
            print(f"{run}/{env}/{name}: {len(converted)} records")
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w") as f:
        for record in records:
            f.write(json.dumps(record, default=str) + "\n")
    print(f"{len(records)} records -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
