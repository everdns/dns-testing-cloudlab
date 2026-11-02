"""Append-only measurement ledger shared by every search and campaign phase.

Each measurement made by search.Evaluator (grid, coordinate descent, agent
bounded/open, source variants, campaign attribution runs) becomes one JSON line
in a ledger file. The per-search CSV/JSON exports stay as they are; the ledger
is what cross-run and cross-hardware analysis reads (analysis/importance.py),
so every record carries the same fields whatever produced it:

    schema, ts, env, hardware{cpu_model, cpus, nic_driver, rx_queues, kernel,
    mem_gb}, software, service, build_id, version, phase, experiment_id,
    point_id, label, replicate_group, repeat_of, hypothesis,
    knobs{canonical name: value},
    change{raw change}, host_profile_id, status, max_qps_passed,
    max_qps_tested, hit_ceiling, min_fidelity, client_limited,
    validity{ok, reasons, ...}, levels[[qps, answer %, passed]], run_dir,
    duration_s

``knobs`` uses the names in knobs.yaml, so "rx-usecs 500 on rs620" and
"rx-usecs 200 on d710" are the same variable in the analysis.
"""
import csv
import datetime as dt
import json
import logging
import os
import threading

from max_sustainable_qps import ssh_run

log = logging.getLogger("ledger")

SCHEMA = 1
DEFAULT_PATH = os.path.expanduser("~/campaign/ledger.jsonl")

# Fields of an Evaluator row copied as-is into a record.
ROW_FIELDS = ["point_id", "label", "replicate_group", "repeat_of", "hypothesis",
              "status", "error", "run_dir"]

# One shell script, so fingerprinting costs one SSH round trip.
FINGERPRINT_SCRIPT = r'''
iface=$(ip -o route get {client} 2>/dev/null | sed -n 's/.* dev \([^ ]*\).*/\1/p')
echo "cpu_model=$(lscpu | sed -n 's/^Model name:[[:space:]]*//p' | head -1)"
echo "cpus=$(nproc)"
echo "sockets=$(lscpu | sed -n 's/^Socket(s):[[:space:]]*//p')"
echo "numa_nodes=$(lscpu | sed -n 's/^NUMA node(s):[[:space:]]*//p')"
echo "kernel=$(uname -r)"
echo "mem_gb=$(awk '/MemTotal/ {{printf "%.0f", $2/1048576}}' /proc/meminfo)"
echo "iface=$iface"
echo "nic_driver=$(ethtool -i $iface 2>/dev/null | sed -n 's/^driver: //p')"
echo "nic_speed=$(cat /sys/class/net/$iface/speed 2>/dev/null)"
echo "rx_queues=$(ls -d /sys/class/net/$iface/queues/rx-* 2>/dev/null | wc -l)"
echo "arch=$(uname -m)"
'''


def now_iso():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fingerprint(server, client):
    """Hardware facts of ``server``, as seen on the interface that reaches
    ``client``. Returns {} if the server cannot be read."""
    try:
        result = ssh_run(server, FINGERPRINT_SCRIPT.format(client=client), timeout=60)
    except Exception as e:  # noqa: BLE001 - fingerprinting is best effort
        log.warning("Could not fingerprint %s: %s", server, e)
        return {}
    facts = {}
    for line in (result.stdout or "").splitlines():
        key, _, value = line.partition("=")
        value = value.strip()
        if key:
            facts[key] = int(value) if value.isdigit() else value
    return facts


def _num(value):
    """int/float from a row value that may be a CSV string; None if empty."""
    if value in ("", None):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else number


def _bool(value):
    if isinstance(value, bool):
        return value
    if value in ("", None):
        return None
    return str(value).strip().lower() == "true"


def read_levels(run_dir):
    """Compact per-level results of one measurement: [qps, answer %, passed]."""
    path = os.path.join(run_dir or "", "max_sustainable_qps", "level_tests.csv")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [[_num(r.get("target_qps")), _num(r.get("average_answer_rate_pct")),
                 _bool(r.get("passed"))] for r in csv.DictReader(f)]


class Ledger:
    """Writes ledger records. ``context`` holds the fields that are the same
    for every measurement of a run (env, hardware, software, phase, ...) and
    may be updated between measurements."""

    def __init__(self, path=DEFAULT_PATH, **context):
        self.path = path
        self.context = context
        self._lock = threading.Lock()

    def record(self, row, knobs=None, change=None, validity=None, **extra):
        """Append one record built from an Evaluator row. Returns the record."""
        fidelity = _num(row.get("min_qps_fidelity_pct"))
        threshold = (self.context.get("min_qps_fidelity_pct") or 99.0)
        record = {
            "schema": SCHEMA,
            "ts": now_iso(),
            **self.context,
            **{k: row.get(k) or None for k in ROW_FIELDS},
            "knobs": knobs or {},
            "change": change or {},
            "max_qps_passed": _num(row.get("max_qps_passed")),
            "max_qps_tested": _num(row.get("max_qps_tested")),
            "hit_ceiling": _bool(row.get("hit_max_qps_ceiling")),
            "min_fidelity": fidelity,
            "client_limited": fidelity is not None and fidelity < threshold,
            "validity": validity or {},
            "levels": read_levels(row.get("run_dir")),
            "duration_s": _num(row.get("search_duration_s")),
            **extra,
        }
        if not self.path:
            return record
        with self._lock:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            with open(self.path, "a") as f:
                f.write(json.dumps(record, default=str) + "\n")
        return record


def read(path):
    """Every record in a ledger file (missing file: none). Skips lines that
    do not parse, e.g. one cut short by a crash."""
    if not os.path.exists(path):
        return []
    records = []
    with open(path) as f:
        for number, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                log.warning("%s:%d: skipping a line that is not JSON", path, number)
    return records


def usable(record):
    """True for a record whose score is a valid server measurement: it ran,
    passed the validity checks, and the load generator kept up."""
    return (record.get("status") == "ok"
            and record.get("max_qps_passed") is not None
            and record.get("validity", {}).get("ok", True)
            and not record.get("client_limited"))
