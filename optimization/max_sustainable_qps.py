#!/usr/bin/env python3
"""Max sustainable QPS evaluation.

Determines the highest QPS at which a DNS server still satisfies a configurable
answer-rate threshold, using a two-phase search:

  Phase 1: exponentially raise the QPS until a level fails (upper bound).
  Phase 2: binary search between the last passing and first failing level.

All QPS values are integer multiples of ``min_qps_step``; the search operates on
QPS indices (qps = qps_idx * min_qps_step) so the resolution is exact.

Supports exactly one load-generation tool (``dnsperf`` or ``kxdpgun``) against
one DNS service per run. Self-contained: grid_search.py imports and calls
``run_max_sustainable_qps`` directly, and this module can also be run standalone:

    python3 -m optimization.max_sustainable_qps --config optimization/grid_search.yaml
"""
import argparse
import csv
import json
import logging
import os
import re
import statistics
import subprocess
import sys
import time
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import yaml

log = logging.getLogger(__name__)

SCRIPT_NAME = "max_sustainable_qps"


# ---------------------------------------------------------------------------
# Remote execution
# ---------------------------------------------------------------------------

def is_local(host):
    """Check if host refers to the local machine."""
    return host in ("localhost", "127.0.0.1", "::1")


def ssh_run(host, command, timeout=None, check=False):
    """Run a command on a remote host via SSH, or locally if host is localhost.

    Returns subprocess.CompletedProcess. On timeout, kills the process (and the
    remote process if over SSH), then re-raises subprocess.TimeoutExpired with
    any partial stdout/stderr captured.
    """
    if is_local(host):
        log.debug("Local exec: %s", command)
        proc = subprocess.Popen(
            command, shell=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            log.warning("Local command timed out, killed: %s", command)
            raise subprocess.TimeoutExpired(
                command, timeout, output=stdout, stderr=stderr,
            )
        return subprocess.CompletedProcess(
            command, proc.returncode, stdout, stderr,
        )
    else:
        log.debug("SSH exec on %s: %s", host, command)
        ssh_cmd = [
            "ssh", "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=accept-new", host, command,
        ]
        proc = subprocess.Popen(
            ssh_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            log.warning("SSH command timed out on %s, killing remote process: %s",
                        host, command)
            binary = command.split()[0]
            try:
                subprocess.run(
                    ["ssh", "-o", "BatchMode=yes",
                     "-o", "StrictHostKeyChecking=accept-new",
                     host, f"pkill -f {binary}"],
                    timeout=10, capture_output=True, text=True,
                )
            except Exception:
                log.warning("pkill failed, trying killall for %s on %s",
                            binary, host)
                try:
                    subprocess.run(
                        ["ssh", "-o", "BatchMode=yes",
                         "-o", "StrictHostKeyChecking=accept-new",
                         host, f"killall -9 {binary}"],
                        timeout=10, capture_output=True, text=True,
                    )
                except Exception:
                    log.error("Could not kill %s on %s", binary, host)
            raise subprocess.TimeoutExpired(
                command, timeout, output=stdout, stderr=stderr,
            )
        result = subprocess.CompletedProcess(
            ssh_cmd, proc.returncode, stdout, stderr,
        )
        if check and result.returncode != 0:
            raise RuntimeError(
                f"SSH command failed on {host} (rc={result.returncode}): {command}\n"
                f"stderr: {result.stderr}"
            )
        return result


def ssh_run_many(host_commands, timeout=None, check=False):
    """Run commands on multiple hosts concurrently via SSH.

    Args:
        host_commands: dict mapping host -> command string.
        timeout: per-host timeout passed to ssh_run.

    Returns a dict mapping host -> subprocess.CompletedProcess, or the raised
    exception (e.g. subprocess.TimeoutExpired) for hosts that failed. One host
    failing does not abort the others — each result is captured independently so
    the caller can inspect partial output.
    """
    results = {}

    def _run(host, command):
        try:
            return ssh_run(host, command, timeout=timeout, check=check)
        except Exception as e:  # noqa: BLE001 - captured per host for the caller
            return e

    with ThreadPoolExecutor(max_workers=max(1, len(host_commands))) as executor:
        futures = {
            executor.submit(_run, host, cmd): host
            for host, cmd in host_commands.items()
        }
        for future in futures:
            host = futures[future]
            results[host] = future.result()
    return results


def extract_run_result(result):
    """Normalize an ssh_run_many per-host result into a uniform tuple.

    Returns (stdout, stderr, returncode, timed_out). ``returncode`` is None when
    the host raised before producing one. Handles both CompletedProcess and the
    captured exceptions (TimeoutExpired and others) returned by ssh_run_many.
    """
    if isinstance(result, subprocess.TimeoutExpired):
        return result.stdout or "", result.stderr or "", None, True
    if isinstance(result, Exception):
        return "", str(result), None, False
    return result.stdout, result.stderr, result.returncode, False


def ssh_run_background(host, command):
    """Start a command on a remote host in the background.

    Returns subprocess.Popen. The caller must manage the process lifecycle.
    """
    if is_local(host):
        log.debug("Local background exec: %s", command)
        return subprocess.Popen(
            command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True,
        )
    else:
        log.debug("SSH background exec on %s: %s", host, command)
        return subprocess.Popen(
            ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
             host, command],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )


def scp_from(host, remote_path, local_path):
    """Copy a file from remote host to local path."""
    if is_local(host):
        subprocess.run(["cp", remote_path, local_path], check=True)
    else:
        log.debug("SCP from %s:%s -> %s", host, remote_path, local_path)
        subprocess.run(
            ["scp", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
             f"{host}:{remote_path}", local_path],
            check=True, capture_output=True, text=True,
        )


def scp_to(host, local_path, remote_path):
    """Copy a file from local to remote host."""
    if is_local(host):
        subprocess.run(["cp", local_path, remote_path], check=True)
    else:
        log.debug("SCP to %s:%s <- %s", host, remote_path, local_path)
        subprocess.run(
            ["scp", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
             local_path, f"{host}:{remote_path}"],
            check=True, capture_output=True, text=True,
        )


# ---------------------------------------------------------------------------
# Hosts
# ---------------------------------------------------------------------------

def get_clients(config):
    """Return the list of load-generation client hosts from config."""
    clients = config.get("hosts", {}).get("clients")
    if not clients:
        raise ValueError("No load-generation hosts configured under 'hosts.clients'")
    if isinstance(clients, str):
        clients = [clients]
    return list(clients)


def client_interface_for(config, host):
    """Network interface kxdpgun uses on ``host``.

    ``client_interfaces`` (host -> interface) overrides the global
    ``client_interface`` for hosts whose NIC names differ.
    """
    per_host = config.get("client_interfaces") or {}
    return per_host.get(host, config.get("client_interface"))


def split_qps(total_qps, n):
    """Split a target QPS evenly across ``n`` hosts.

    Returns a list of ``n`` integers that sum to ``total_qps``. Any remainder
    is distributed one-per-host to the first hosts, e.g.
    ``split_qps(100000, 3) -> [33334, 33333, 33333]``.
    """
    if n < 1:
        raise ValueError(f"Number of hosts must be >= 1, got {n}")
    base = total_qps // n
    remainder = total_qps % n
    return [base + (1 if i < remainder else 0) for i in range(n)]


def host_token(host):
    """Return a filesystem-safe token for a host (for raw output filenames)."""
    return re.sub(r"[@.:/]", "-", str(host))


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

@dataclass
class ToolResult:
    queries_sent: int = 0
    queries_completed: int = 0
    queries_lost: int = 0
    achieved_qps: float = 0.0
    run_time: float = 0.0
    avg_latency: float | None = None
    min_latency: float | None = None
    max_latency: float | None = None
    latency_stddev: float | None = None
    response_codes: dict[str, int] = field(default_factory=dict)
    raw_output: str = ""


def aggregate_tool_results(tool_results):
    """Combine per-host ToolResults into a single aggregate ToolResult.

    Counters (queries_sent/completed/lost) and achieved_qps are summed. Latency
    is combined as a query-weighted mean (weighted by queries_completed), with
    min/max taken across hosts. Latency stddev is pooled across hosts using a
    degrees-of-freedom weighted average of per-host variances:

        s_p = sqrt( sum((n_i - 1) * s_i**2) / (sum(n_i) - k) )

    where n_i = queries_completed for host i and k = number of hosts with
    n_i >= 2 and a reported stddev.
    """
    agg = ToolResult()
    if not tool_results:
        return agg

    for code in (c for r in tool_results for c in r.response_codes):
        agg.response_codes[code] = sum(
            r.response_codes.get(code, 0) for r in tool_results
        )

    agg.queries_sent = sum(r.queries_sent for r in tool_results)
    agg.queries_completed = sum(r.queries_completed for r in tool_results)
    agg.queries_lost = sum(r.queries_lost for r in tool_results)
    agg.achieved_qps = sum(r.achieved_qps for r in tool_results)
    agg.run_time = max((r.run_time for r in tool_results), default=0.0)

    weighted = [
        (r.avg_latency, r.queries_completed)
        for r in tool_results
        if r.avg_latency is not None and r.queries_completed > 0
    ]
    total_weight = sum(w for _, w in weighted)
    if total_weight > 0:
        agg.avg_latency = sum(lat * w for lat, w in weighted) / total_weight

    mins = [r.min_latency for r in tool_results if r.min_latency is not None]
    maxs = [r.max_latency for r in tool_results if r.max_latency is not None]
    if mins:
        agg.min_latency = min(mins)
    if maxs:
        agg.max_latency = max(maxs)

    pool = [
        (r.latency_stddev, r.queries_completed)
        for r in tool_results
        if r.latency_stddev is not None and r.queries_completed >= 2
    ]
    denom = sum(n for _, n in pool) - len(pool)
    if denom > 0:
        numer = sum((n - 1) * (s ** 2) for s, n in pool)
        agg.latency_stddev = (numer / denom) ** 0.5

    return agg


class ResultStore:
    """Stores and exports evaluation results."""

    def __init__(self, output_dir):
        self.output_dir = output_dir
        self.results = []

    def _ensure_dir(self, *parts):
        path = os.path.join(self.output_dir, *parts)
        os.makedirs(path, exist_ok=True)
        return path

    def save_raw_output(self, script, filename, content):
        """Save raw tool output."""
        raw_dir = self._ensure_dir(script, "raw")
        path = os.path.join(raw_dir, filename)
        with open(path, "w") as f:
            f.write(content)
        return path

    def add_result(self, row):
        """Add a result row (dict)."""
        self.results.append(row)

    def export_csv(self, script, filename="results.csv"):
        """Export accumulated results to CSV."""
        if not self.results:
            return
        out_dir = self._ensure_dir(script)
        path = os.path.join(out_dir, filename)
        keys = list(dict.fromkeys(k for row in self.results for k in row))
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=keys)
            writer.writeheader()
            writer.writerows(self.results)
        return path

    def export_json(self, script, filename="results.json"):
        """Export accumulated results to JSON."""
        if not self.results:
            return
        out_dir = self._ensure_dir(script)
        path = os.path.join(out_dir, filename)
        with open(path, "w") as f:
            json.dump(self.results, f, indent=2, default=str)
        return path


# ---------------------------------------------------------------------------
# DNS service control
# ---------------------------------------------------------------------------

def start_dns_service(config, service_name):
    """Start a DNS service on the server host."""
    server = config["hosts"]["server"]
    start_script = config["dns_services"]["start_script"]

    log.info("Starting DNS service '%s' on %s", service_name, server)
    result = ssh_run(server, f"{start_script} {service_name}", timeout=30)

    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to start {service_name}: {result.stderr}"
        )
    log.info("DNS service '%s' started", service_name)


def stop_dns_service(config, service_name=None):
    """Stop a DNS service on the server host.

    Args:
        config: global config dict
        service_name: specific service to stop, or None to stop all
    """
    server = config["hosts"]["server"]
    stop_script = config["dns_services"]["stop_script"]

    cmd = f"{stop_script} {service_name}" if service_name else stop_script
    log.info("Stopping DNS service%s on %s",
             f" '{service_name}'" if service_name else "s", server)

    result = ssh_run(server, cmd, timeout=30)
    if result.returncode != 0:
        log.warning("Stop command returned non-zero: %s", result.stderr)


def restart_dns_service(config, service_name):
    """Stop and start a DNS service, then wait until it answers queries.

    A server that has just been overloaded can serve later trials worse than a
    fresh one, so each trial starts from a freshly started server.
    """
    stop_dns_service(config, service_name)
    time.sleep(2)
    start_dns_service(config, service_name)
    wait_for_dns_ready(config, timeout=300)


def wait_for_dns_ready(config, timeout=300):
    """Poll until the DNS server on the resolver IP responds to queries.

    Uses dig to send a test query.
    """
    server = config["hosts"]["server"]
    client = get_clients(config)[0]

    log.info("Waiting for DNS server at %s to be ready...", server)
    deadline = time.time() + timeout

    while time.time() < deadline:
        try:
            result = ssh_run(client, f"dig @{server}", timeout=5)
        except subprocess.TimeoutExpired:
            time.sleep(1)
            continue
        if result.returncode == 0 and result.stdout.strip():
            log.info("DNS server at %s is ready", server)
            return True
        time.sleep(1)

    raise TimeoutError(f"DNS server at {server} not ready after {timeout}s")


def reset_clients(config, tool):
    """Undo client-side state a previous, interrupted run may have left behind
    (e.g. an XDP program still attached by a killed kxdpgun)."""
    clients = get_clients(config)
    commands = {}
    for host in clients:
        command = tool.cleanup_command(
            {**config, "client_interface": client_interface_for(config, host)})
        if command:
            commands[host] = command
    if not commands:
        return
    results = ssh_run_many(commands, timeout=30)
    for host in clients:
        _, stderr, rc, timed_out = extract_run_result(results[host])
        if timed_out or rc != 0:
            log.warning("Client cleanup on %s failed (%s): %s", host,
                        "timed out" if timed_out else f"rc={rc}", stderr.strip())


# ---------------------------------------------------------------------------
# collectl
# ---------------------------------------------------------------------------

# Metric id -> (collectl column name, conversion fn, output key prefix)
_SINGLE_COLUMN_METRICS = [
    ("cpu_totl",   "[CPU]Totl%",   lambda x: x,        "pct"),
    ("cpu_user",   "[CPU]User%",   lambda x: x,        "pct"),
    ("cpu_sys",    "[CPU]Sys%",    lambda x: x,        "pct"),
    ("mem_used",   "[MEM]Used",    lambda x: x / 1024, "mb"),
    ("mem_tot",    "[MEM]Tot",     lambda x: x / 1024, "mb"),
    ("mem_free",   "[MEM]Free",    lambda x: x / 1024, "mb"),
    ("mem_cached", "[MEM]Cached",  lambda x: x / 1024, "mb"),
    ("net_rx_pkt", "[NET]RxPktTot", lambda x: x,       ""),
    ("net_tx_pkt", "[NET]TxPktTot", lambda x: x,       ""),
]


def start_collectl(config, duration_s, output_file="/tmp/collectl_trail.txt"):
    """Start collectl on the DNS server host in the background.

    Returns subprocess.Popen handle, or None on dry-run.
    """
    server = config["hosts"]["server"]
    cmd = (
        f"nohup collectl -scndm --plot -c {duration_s} > {output_file} 2>/dev/null"
    )
    log.info("Starting collectl on %s: %s", server, cmd)

    if config.get("dry_run"):
        log.info("[DRY RUN] Would execute: ssh %s '%s'", server, cmd)
        return None

    return ssh_run_background(server, cmd)


def wait_collectl(proc, timeout=None):
    """Wait for collectl to finish. Tolerant of None proc."""
    if proc is None:
        return "", ""
    stdout, stderr = proc.communicate(timeout=timeout)
    return stdout, stderr


def collect_collectl_file(config, remote_file, local_path):
    """SCP the collectl trail file back from the server to local_path."""
    server = config["hosts"]["server"]
    os.makedirs(os.path.dirname(local_path), exist_ok=True)
    scp_from(server, remote_file, local_path)
    return local_path


def run_collectl_session(config, runtime_s, remote_output, margin: int):
    """Start collectl and wait the margin so sampling is warm before the tool.

    duration = runtime_s + 2 * margin (cover pre-tool and post-tool windows).
    Returns {proc, output_file, duration, margin}.
    """
    duration = runtime_s + 2 * margin

    proc = start_collectl(config, duration, remote_output)

    if not config.get("dry_run"):
        time.sleep(margin)

    return {
        "proc": proc,
        "output_file": remote_output,
        "duration": duration,
        "margin": margin,
    }


def _find_header_and_rows(path):
    """Find the `#Date Time ...` header and return (columns, data_rows)."""
    columns = None
    rows = []
    with open(path) as f:
        for line in f:
            line = line.rstrip("\n")
            if not line.strip():
                continue
            if line.startswith("#"):
                if columns is None and line.startswith("#Date Time"):
                    columns = line.lstrip("#").split()
                continue
            if columns is None:
                continue
            parts = line.split()
            if len(parts) != len(columns):
                continue
            rows.append(parts)
    return columns, rows


def _extract_series(columns, rows):
    """Return dict of metric_id -> list[float] time series."""
    col_index = {name: i for i, name in enumerate(columns)}
    series = {}

    for metric_id, col_name, conv, _suffix in _SINGLE_COLUMN_METRICS:
        idx = col_index.get(col_name)
        if idx is None:
            log.warning("collectl column '%s' not found for metric '%s'",
                        col_name, metric_id)
            continue
        values = []
        for row in rows:
            try:
                values.append(conv(float(row[idx])))
            except (ValueError, IndexError):
                continue
        series[metric_id] = values

    if "cpu_totl" not in series:
        idx_idle = col_index.get("[CPU]Idle%")
        if idx_idle is not None:
            values = []
            for row in rows:
                try:
                    values.append(100.0 - float(row[idx_idle]))
                except (ValueError, IndexError):
                    continue
            series["cpu_totl"] = values
            log.info("Using 100 - [CPU]Idle%% as fallback for cpu_totl")

    rx_idx = col_index.get("[NET]RxKBTot")
    tx_idx = col_index.get("[NET]TxKBTot")
    if rx_idx is not None and tx_idx is not None:
        sum_values, rx_values, tx_values = [], [], []
        for row in rows:
            try:
                rx = float(row[rx_idx])
                tx = float(row[tx_idx])
            except (ValueError, IndexError):
                continue
            sum_values.append(rx + tx)
            rx_values.append(rx)
            tx_values.append(tx)
        series["net_kb"] = sum_values
        series["net_rx_kb"] = rx_values
        series["net_tx_kb"] = tx_values
    else:
        log.warning("collectl columns [NET]RxKBTot / [NET]TxKBTot not found "
                    "for metric 'net_kb'")

    return series


def _median_peak_keys(metric_id):
    """Return (median_key, peak_key) for a metric id."""
    if metric_id.startswith("cpu_"):
        return f"{metric_id}_median_pct", f"{metric_id}_peak_pct"
    if metric_id.startswith("mem_"):
        return f"{metric_id}_median_mb", f"{metric_id}_peak_mb"
    if metric_id in ("net_kb", "net_rx_kb", "net_tx_kb"):
        return f"{metric_id}_median_kbps", f"{metric_id}_peak_kbps"
    return f"{metric_id}_median", f"{metric_id}_peak"


def parse_collectl_file(path, margin_s):
    """Parse a collectl --plot trail file and aggregate median + peak.

    Drops the first `margin_s` and last `margin_s` rows (1 sample/sec) before
    aggregating. Returns a dict of scalar metrics, or an empty dict on
    failure. Never raises.
    """
    try:
        columns, rows = _find_header_and_rows(path)
        if columns is None:
            log.warning("collectl file %s has no '#Date Time' header", path)
            return {}
        if not rows:
            log.warning("collectl file %s has no data rows", path)
            return {}

        margin = max(0, int(margin_s))
        if margin > 0 and len(rows) > 2 * margin:
            cropped = rows[margin:-margin]
        else:
            cropped = rows

        if not cropped:
            log.warning("collectl file %s has no rows after cropping margin=%d",
                        path, margin)
            return {}

        series = _extract_series(columns, cropped)
        out = {}
        for metric_id, values in series.items():
            if not values:
                continue
            median_key, peak_key = _median_peak_keys(metric_id)
            out[median_key] = float(statistics.median(values))
            out[peak_key] = float(max(values))
        return out
    except Exception as e:
        log.warning("Failed to parse collectl file %s: %s", path, e)
        return {}


# ---------------------------------------------------------------------------
# Load-generation tools
# ---------------------------------------------------------------------------

class Tool(ABC):
    """Abstract base class for DNS load testing tool adapters."""

    name: str = ""
    reports_latency: bool = False

    @abstractmethod
    def build_command(self, config, qps):
        """Build the shell command string to run this tool."""

    @abstractmethod
    def parse_output(self, stdout):
        """Parse tool stdout into a ToolResult."""

    def validate_params(self, config, qps):
        """Check tool-specific constraints. Raise ValueError on violation."""
        pass

    def cleanup_command(self, config):
        """Shell command that undoes any client-side state this tool leaves
        behind when it is killed mid-run, or None if there is nothing to undo."""
        return None


class Dnsperf(Tool):
    name = "dnsperf"
    reports_latency = True

    def validate_params(self, config, qps):
        threads = config["threads"]
        max_outstanding = 65536 * threads
        if max_outstanding < 1:
            raise ValueError(f"Invalid max_outstanding: {max_outstanding}")

    def build_command(self, config, qps):
        server = config["hosts"]["server"]
        runtime = config["runtime"]
        input_file = config["input_file"]
        threads = config["threads"]
        ports_per_thread = config["ports_per_thread"]
        timeout = config["timeout"]
        max_outstanding = 65536 * threads
        clients = threads * ports_per_thread

        return (
            f"dnsperf -s {server} -l {runtime} -d {input_file}"
            f" -c {clients} -T {threads}"
            f" -Q {qps} -q {max_outstanding}"
            f" -O suppress=timeout -O qps-threshold-wait=0 -t {timeout}"
        )

    def parse_output(self, stdout):
        result = ToolResult(raw_output=stdout)

        def find_int(pattern):
            m = re.search(pattern, stdout)
            return int(m.group(1)) if m else 0

        def find_float(pattern):
            m = re.search(pattern, stdout)
            return float(m.group(1)) if m else 0.0

        result.queries_sent = find_int(r"Queries sent:\s+(\d+)")
        result.queries_completed = find_int(r"Queries completed:\s+(\d+)")
        result.queries_lost = find_int(r"Queries lost:\s+(\d+)")
        result.run_time = find_float(r"Run time \(s\):\s+([\d.]+)")
        result.achieved_qps = find_float(r"Queries per second:\s+([\d.]+)")
        result.avg_latency = find_float(r"Average Latency \(s\):\s+([\d.]+)")

        m = re.search(r"Average Latency \(s\):\s+[\d.]+\s+\(min\s+([\d.]+),\s+max\s+([\d.]+)\)", stdout)
        if m:
            result.min_latency = float(m.group(1))
            result.max_latency = float(m.group(2))

        result.latency_stddev = find_float(r"Latency StdDev \(s\):\s+([\d.]+)")

        for m in re.finditer(r"(NOERROR|SERVFAIL|NXDOMAIN|REFUSED)\s+(\d+)", stdout):
            result.response_codes[m.group(1)] = int(m.group(2))

        return result


class Kxdpgun(Tool):
    name = "kxdpgun"
    reports_latency = False

    def build_command(self, config, qps):
        server = config["hosts"]["server"]
        runtime = config["runtime"]
        input_file = config["input_file"]
        interface = config["client_interface"]

        # Detach the XDP program even when kxdpgun fails, keeping its exit
        # status. A program left attached swallows every reply on the
        # interface, so later readiness checks and trials all time out.
        return (
            f"sudo kxdpgun -t {runtime} -Q {qps}"
            f" -i {input_file} -I {interface}"
            f" {server} --mode generic"
            f"; rc=$?; {self.cleanup_command(config)}; exit $rc"
        )

    def cleanup_command(self, config):
        return f"sudo ip link set dev {config['client_interface']} xdpgeneric off"

    def parse_output(self, stdout):
        result = ToolResult(raw_output=stdout)

        m = re.search(r"total queries:\s+([\d,]+)", stdout)
        if m:
            result.queries_sent = int(m.group(1).replace(",", ""))

        for m_code in re.finditer(r"responded\s+(\w+):\s+(\d+)", stdout):
            result.response_codes[m_code.group(1)] = int(m_code.group(2))

        result.queries_completed = result.response_codes.get("NOERROR", 0)
        result.queries_lost = result.queries_sent - result.queries_completed

        m = re.search(r"duration:\s+(\d+)\s+s", stdout)
        if m:
            result.run_time = float(m.group(1))

        if result.run_time > 0:
            result.achieved_qps = result.queries_completed / result.run_time

        return result


TOOLS = {
    "dnsperf": Dnsperf,
    "kxdpgun": Kxdpgun,
}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config(config_path):
    """Load configuration from a YAML file."""
    with open(config_path) as f:
        return yaml.safe_load(f)


def validate_search_config(config):
    """Validate the search parameters and resolve them into a params dict.

    Raises ValueError with an actionable message on any violation. Validation is
    done up front because a bad parameter here costs an entire run of wall clock
    time.
    """
    section = config.get("max_sustainable_qps")
    if not section:
        raise ValueError(
            "Config is missing the 'max_sustainable_qps' section (see "
            "optimization/grid_search.yaml for an example)"
        )

    def required_int(key):
        value = section.get(key)
        if value is None:
            raise ValueError(f"max_sustainable_qps.{key} is required")
        return int(value)

    initial_qps = required_int("initial_qps")
    min_qps_step = required_int("min_qps_step")
    max_qps = required_int("max_qps")
    num_trials = required_int("num_trials")
    min_passes = required_int("min_passes")
    trial_duration = required_int("trial_duration")
    answer_rate_threshold = float(section.get("answer_rate_threshold", 99.0))
    min_qps_fidelity_pct = float(section.get("min_qps_fidelity_pct", 99.0))
    collectl_margin = int(section.get("collectl_margin", 5))
    ramp_factor = float(section.get("ramp_factor", 2.0))

    if initial_qps <= 0:
        raise ValueError(
            f"max_sustainable_qps.initial_qps must be > 0, got {initial_qps}"
        )
    if min_qps_step <= 0:
        raise ValueError(
            f"max_sustainable_qps.min_qps_step must be > 0, got {min_qps_step}"
        )
    if initial_qps % min_qps_step != 0:
        raise ValueError(
            f"max_sustainable_qps.initial_qps ({initial_qps}) must be exactly "
            f"divisible by min_qps_step ({min_qps_step})"
        )
    if num_trials <= 0:
        raise ValueError(
            f"max_sustainable_qps.num_trials must be > 0, got {num_trials}"
        )
    if min_passes <= 0:
        raise ValueError(
            f"max_sustainable_qps.min_passes must be > 0, got {min_passes}"
        )
    if min_passes > num_trials:
        raise ValueError(
            f"max_sustainable_qps.min_passes ({min_passes}) must be <= "
            f"num_trials ({num_trials})"
        )
    if not 0 < answer_rate_threshold <= 100:
        raise ValueError(
            "max_sustainable_qps.answer_rate_threshold is a percentage and must "
            f"be in (0, 100], got {answer_rate_threshold}"
        )
    if trial_duration <= 0:
        raise ValueError(
            f"max_sustainable_qps.trial_duration must be > 0, got {trial_duration}"
        )
    if collectl_margin < 0:
        raise ValueError(
            f"max_sustainable_qps.collectl_margin must be >= 0, got {collectl_margin}"
        )
    if ramp_factor <= 1.0:
        raise ValueError(
            f"max_sustainable_qps.ramp_factor must be > 1, got {ramp_factor}"
        )
    if max_qps < min_qps_step:
        raise ValueError(
            f"max_sustainable_qps.max_qps ({max_qps}) must be >= "
            f"min_qps_step ({min_qps_step})"
        )
    if max_qps % min_qps_step != 0:
        adjusted = (max_qps // min_qps_step) * min_qps_step
        log.warning(
            "max_qps (%d) is not a multiple of min_qps_step (%d); flooring to %d",
            max_qps, min_qps_step, adjusted,
        )
        max_qps = adjusted

    tool_name = config.get("tool")
    if not tool_name:
        raise ValueError("Config must set 'tool' to 'dnsperf' or 'kxdpgun'")
    if tool_name not in TOOLS:
        raise ValueError(
            f"Unknown tool: {tool_name}. Available: {list(TOOLS)}"
        )
    tool = TOOLS[tool_name]()

    dns_service = config.get("dns_service")
    if not dns_service:
        raise ValueError("Config must set 'dns_service'")

    # The tool adapters both size their run from config["runtime"]; trial_duration
    # is the user-facing name for the same quantity.
    config["runtime"] = trial_duration

    return {
        "initial_qps": initial_qps,
        "min_qps_step": min_qps_step,
        "max_qps": max_qps,
        "num_trials": num_trials,
        "min_passes": min_passes,
        "max_fails": num_trials - min_passes,
        "trial_duration": trial_duration,
        "answer_rate_threshold": answer_rate_threshold,
        "min_qps_fidelity_pct": min_qps_fidelity_pct,
        "collectl": bool(section.get("collectl", False)),
        "collectl_margin": collectl_margin,
        "ramp_factor": ramp_factor,
        "tool": tool,
        "dns_service": dns_service,
        "simulate_max_qps": None,
        "hooks": None,
    }


# ---------------------------------------------------------------------------
# Search algorithm
# ---------------------------------------------------------------------------

def _new_trial_row(params, qps, trial, status):
    """Build a trial row with the specified column order and neutral values."""
    return {
        "dns_service": params["dns_service"],
        "tool": params["tool"].name,
        "target_qps": qps,
        "trial": trial + 1,
        "achieved_qps": 0.0,
        "queries_sent": 0,
        "queries_completed": 0,
        "queries_lost": 0,
        "answer_rate_pct": 0.0,
        "passed": False,
        "qps_fidelity_pct": 0.0,
        "status": status,
    }


def _simulated_trial_row(params, qps, trial):
    """Synthesise a trial result for --simulate-max-qps (no remote execution)."""
    row = _new_trial_row(params, qps, trial, "simulated")
    passed = qps <= params["simulate_max_qps"]
    answer_rate = 100.0 if passed else 50.0
    row.update({
        "achieved_qps": float(qps if passed else qps // 2),
        "queries_sent": qps * params["trial_duration"],
        "queries_completed": int(qps * params["trial_duration"] * answer_rate / 100.0),
        "queries_lost": qps * params["trial_duration"]
                        - int(qps * params["trial_duration"] * answer_rate / 100.0),
        "answer_rate_pct": answer_rate,
        "passed": passed,
        "qps_fidelity_pct": 100.0,
    })
    return row


def run_trial(config, params, qps, trial, trial_store):
    """Run one trial at ``qps`` and return its result row.

    Never raises: an infrastructure failure is recorded as a trial with a 0.0
    answer rate, which counts as a failure. A server that cannot be reached is
    not a server that is sustaining the load, and this guarantees the search
    terminates instead of hanging on a broken host.
    """
    if params["simulate_max_qps"] is not None:
        return _simulated_trial_row(params, qps, trial)

    tool = params["tool"]
    dns_service = params["dns_service"]
    trial_duration = params["trial_duration"]
    collectl_margin = params["collectl_margin"]
    dry_run = config.get("dry_run", False)

    row = _new_trial_row(params, qps, trial, "error")

    clients = get_clients(config)
    shares = split_qps(qps, len(clients))

    try:
        host_cmds = {}
        for host, share in zip(clients, shares):
            tool.validate_params(config, share)
            host_config = {**config,
                           "client_interface": client_interface_for(config, host)}
            host_cmds[host] = tool.build_command(host_config, share)
    except Exception as e:
        log.error("Failed to build %s command at %d QPS trial %d: %s",
                  tool.name, qps, trial + 1, e)
        return row

    log.info("Trial %d at %d QPS: %s vs %s across %d host(s): %s",
             trial + 1, qps, tool.name, dns_service, len(clients),
             dict(zip(clients, shares)))

    if dry_run:
        log.info("[DRY RUN] Would restart %s on %s before the trial",
                 dns_service, config["hosts"]["server"])
    else:
        try:
            restart_dns_service(config, dns_service)
        except Exception as e:
            log.error("Failed to restart %s before %d QPS trial %d: %s",
                      dns_service, qps, trial + 1, e)
            row["status"] = "restart_failed"
            return row

    if dry_run:
        for host, cmd in host_cmds.items():
            log.info("[DRY RUN] Would run on %s: %s", host, cmd)
        if params["collectl"]:
            log.info(
                "[DRY RUN] Would run collectl on server for %d seconds "
                "(trial_duration=%d, margin=%d)",
                trial_duration + 2 * collectl_margin, trial_duration, collectl_margin,
            )
        row["status"] = "dry_run"
        return row

    collectl_session = None
    collectl_local_path = None
    if params["collectl"]:
        try:
            remote_trail = (
                f"/tmp/collectl_{dns_service}_{tool.name}_{qps}_{trial}.txt"
            )
            collectl_session = run_collectl_session(
                config, trial_duration, remote_trail, margin=collectl_margin,
            )
            collectl_local_path = os.path.join(
                trial_store._ensure_dir(SCRIPT_NAME, "collectl"),
                f"{dns_service}_{tool.name}_{qps}qps_trial{trial}.collectl.txt",
            )
        except Exception as e:
            log.warning("Failed to start collectl: %s. Continuing without it.", e)
            collectl_session = None

    hooks = params.get("hooks")
    hook_handle = None
    if hooks is not None:
        try:
            hook_handle = hooks.trial_start(config, qps, trial)
        except Exception as e:
            log.warning("Trial hook failed to start at %d QPS trial %d: %s",
                        qps, trial + 1, e)

    try:
        for host, cmd in host_cmds.items():
            log.info("Running on %s: %s", host, cmd)
        tool_timeout = trial_duration + 2 * collectl_margin + 120
        run_results = ssh_run_many(host_cmds, timeout=tool_timeout)

        per_host = []
        for host, share in zip(clients, shares):
            stdout, stderr, rc, host_timed_out = extract_run_result(run_results[host])
            if host_timed_out:
                log.warning("%s timed out on %s at %d QPS", tool.name, host, share)
            elif rc not in (0, None):
                log.warning("%s returned exit code %d on %s", tool.name, rc, host)

            trial_store.save_raw_output(
                SCRIPT_NAME,
                f"{dns_service}_{tool.name}_{qps}qps_trial{trial}_{host_token(host)}.txt",
                f"=== COMMAND ===\n{host_cmds[host]}\n"
                f"=== STDOUT ===\n{stdout}\n=== STDERR ===\n{stderr}",
            )
            try:
                tr = tool.parse_output(stdout)
            except Exception:
                tr = ToolResult()
            per_host.append(tr)

        agg = aggregate_tool_results(per_host)

        answer_rate = 0.0
        if agg.queries_sent > 0:
            answer_rate = round(
                agg.queries_completed / agg.queries_sent * 100.0, 4
            )

        row.update({
            "achieved_qps": agg.achieved_qps,
            "queries_sent": agg.queries_sent,
            "queries_completed": agg.queries_completed,
            "queries_lost": agg.queries_lost,
            "answer_rate_pct": answer_rate,
            "passed": answer_rate >= params["answer_rate_threshold"],
            "status": "ok",
        })

        # The tool can fall short of the requested rate before the server does.
        # Advisory only: it does not affect pass/fail, but without it a
        # generator-side bottleneck is indistinguishable from server headroom.
        expected_queries = qps * trial_duration
        if expected_queries > 0:
            fidelity = round(agg.queries_sent / expected_queries * 100.0, 4)
            row["qps_fidelity_pct"] = fidelity
            if fidelity < params["min_qps_fidelity_pct"]:
                log.warning(
                    "%s sent only %.2f%% of the %d queries requested at %d QPS "
                    "(trial %d). The load generator may be the bottleneck, so "
                    "this level's answer rate may overstate server capacity.",
                    tool.name, fidelity, expected_queries, qps, trial + 1,
                )

        if tool.reports_latency:
            row["avg_latency_s"] = agg.avg_latency
            row["min_latency_s"] = agg.min_latency
            row["max_latency_s"] = agg.max_latency
            row["latency_stddev_s"] = agg.latency_stddev

    except subprocess.TimeoutExpired:
        log.error("%s timed out at %d QPS trial %d", tool.name, qps, trial + 1)
        row["status"] = "timeout"
    except Exception as e:
        log.error("Error running %s at %d QPS trial %d: %s",
                  tool.name, qps, trial + 1, e)
        row["status"] = "error"

    # Like collectl, a hook only adds fields; it never fails a trial.
    if hook_handle is not None:
        try:
            row.update(hooks.trial_end(hook_handle) or {})
        except Exception as e:
            log.warning("Trial hook failed at %d QPS trial %d: %s", qps, trial + 1, e)

    # Collect collectl regardless of how the run went; it must never fail a trial.
    if collectl_session is not None:
        try:
            wait_collectl(collectl_session["proc"], timeout=collectl_margin + 30)
            collect_collectl_file(
                config, collectl_session["output_file"], collectl_local_path,
            )
            metrics = parse_collectl_file(collectl_local_path, collectl_margin)
            row.update({k: v for k, v in metrics.items() if v is not None})
        except Exception as e:
            log.warning(
                "collectl collection/parse failed at %d QPS trial %d: %s",
                qps, trial + 1, e,
            )

    return row


def level_test(config, params, qps, trial_store):
    """Determine whether ``qps`` passes the evaluation criteria.

    Runs up to num_trials trials and stops as soon as the outcome is
    mathematically determined: the level fails once num_fails exceeds max_fails,
    and passes once num_passes reaches min_passes.

    Returns (passed, level_row).
    """
    num_trials = params["num_trials"]
    min_passes = params["min_passes"]
    max_fails = params["max_fails"]
    threshold = params["answer_rate_threshold"]

    log.info("=== Level test at %d QPS (need %d/%d trials at >= %.4f%% answer "
             "rate; fails after %d) ===",
             qps, min_passes, num_trials, threshold, max_fails + 1)

    num_passes = 0
    num_fails = 0
    achieved_qps_values = []
    answer_rates = []
    passed = None

    for trial in range(num_trials):
        row = run_trial(config, params, qps, trial, trial_store)
        trial_store.add_result(row)
        achieved_qps_values.append(row["achieved_qps"])
        answer_rates.append(row["answer_rate_pct"])

        if row["passed"]:
            num_passes += 1
        else:
            num_fails += 1

        log.info("Trial %d/%d at %d QPS: answer rate %.4f%% -> %s "
                 "(passes=%d fails=%d, status=%s)",
                 trial + 1, num_trials, qps, row["answer_rate_pct"],
                 "PASS" if row["passed"] else "FAIL",
                 num_passes, num_fails, row["status"])

        if num_fails > max_fails:
            passed = False
            break
        if num_passes >= min_passes:
            passed = True
            break

        if not config.get("dry_run") and params["simulate_max_qps"] is None:
            pause = config.get("pause_between_runs", 0)
            if pause:
                log.info("Pausing %ds...", pause)
                time.sleep(pause)

    if passed is None:
        # Unreachable while min_passes <= num_trials; kept as a safety net.
        passed = num_passes >= min_passes

    trials_run = num_passes + num_fails
    level_row = {
        "target_qps": qps,
        "num_trials": trials_run,
        "num_passes": num_passes,
        "num_fails": num_fails,
        "average_achieved_qps": (
            round(sum(achieved_qps_values) / len(achieved_qps_values), 2)
            if achieved_qps_values else 0.0
        ),
        "average_answer_rate_pct": (
            round(sum(answer_rates) / len(answer_rates), 4)
            if answer_rates else 0.0
        ),
        "passed": passed,
        "max_trials": num_trials,
    }

    log.info("=== Level %d QPS: %s after %d trial(s) (%d pass / %d fail, "
             "avg answer rate %.4f%%) ===",
             qps, "PASS" if passed else "FAIL", trials_run, num_passes,
             num_fails, level_row["average_answer_rate_pct"])

    return passed, level_row


def run_search(config, params, trial_store, level_store):
    """Run the two-phase QPS search and return the summary row."""
    min_qps_step = params["min_qps_step"]
    initial_qps_idx = params["initial_qps"] // min_qps_step
    max_qps_idx = params["max_qps"] // min_qps_step

    low_qps_idx = 1
    high_qps_idx = min(initial_qps_idx, max_qps_idx)
    max_passing_qps = 0
    tested_levels = {}  # qps_idx -> passed
    started = time.time()

    def test(qps_idx):
        """Test one level, reusing a cached verdict rather than re-running it."""
        if qps_idx in tested_levels:
            log.info("Level %d QPS already tested -> %s; not repeating",
                     qps_idx * min_qps_step,
                     "PASS" if tested_levels[qps_idx] else "FAIL")
            return tested_levels[qps_idx]

        passed, level_row = level_test(
            config, params, qps_idx * min_qps_step, trial_store,
        )
        tested_levels[qps_idx] = passed
        level_store.add_result(level_row)
        # Export after every level so an interrupted run still leaves complete
        # data on disk.
        trial_store.export_csv(SCRIPT_NAME, "trial_results.csv")
        level_store.export_csv(SCRIPT_NAME, "level_tests.csv")
        return passed

    log.info("--- Phase 1: searching for an upper bound from %d QPS ---",
             high_qps_idx * min_qps_step)
    hit_ceiling = False
    while True:
        at_ceiling = high_qps_idx >= max_qps_idx
        if at_ceiling and high_qps_idx != max_qps_idx:
            log.info("Clamping level to the max_qps ceiling of %d QPS",
                     max_qps_idx * min_qps_step)
            high_qps_idx = max_qps_idx

        if not test(high_qps_idx):
            break

        # Everything at or below this level is known to pass, so the binary
        # search never needs to revisit it.
        low_qps_idx = high_qps_idx + 1
        max_passing_qps = high_qps_idx * min_qps_step

        if at_ceiling:
            hit_ceiling = True
            log.warning(
                "The max_qps ceiling of %d QPS PASSED. Finishing the search "
                "early: the server's real limit was never bracketed, so "
                "max_qps_passed=%d is a LOWER BOUND. Raise max_qps to find the "
                "true maximum.",
                max_passing_qps, max_passing_qps,
            )
            break

        # A ramp_factor below 2 (with initial_qps near a known result) needs
        # fewer levels when the answer is roughly known already.
        high_qps_idx = max(high_qps_idx + 1, int(high_qps_idx * params["ramp_factor"]))

    if not hit_ceiling:
        log.info("--- Phase 2: binary search between %d and %d QPS ---",
                 low_qps_idx * min_qps_step, high_qps_idx * min_qps_step)
        while low_qps_idx < high_qps_idx:
            mid_qps_idx = low_qps_idx + ((high_qps_idx - low_qps_idx) // 2)
            if test(mid_qps_idx):
                low_qps_idx = mid_qps_idx + 1
                max_passing_qps = max(max_passing_qps, mid_qps_idx * min_qps_step)
            else:
                high_qps_idx = mid_qps_idx

    max_qps_tested = (
        max(tested_levels) * min_qps_step if tested_levels else 0
    )

    return {
        "max_qps_passed": max_passing_qps,
        "max_qps_tested": max_qps_tested,
        "num_qps_values_tested": len(tested_levels),
        "dns_service": params["dns_service"],
        "tool": params["tool"].name,
        "initial_qps": params["initial_qps"],
        "min_qps_step": min_qps_step,
        "max_qps_ceiling": params["max_qps"],
        "num_trials": params["num_trials"],
        "min_passes": params["min_passes"],
        "answer_rate_threshold": params["answer_rate_threshold"],
        "trial_duration": params["trial_duration"],
        "hit_max_qps_ceiling": hit_ceiling,
        "total_trials_run": len(trial_store.results),
        "search_duration_s": round(time.time() - started, 1),
    }


def _call_hook(hook, config, simulating):
    try:
        hook(config, simulating)
    except Exception as e:
        log.warning("Measurement hook %s failed: %s", getattr(hook, "__name__", hook), e)


def run_max_sustainable_qps(config, output_dir, simulate_max_qps=None, hooks=None):
    """Validate ``config``, run the full search, and return (summary, trial_rows).

    Manages the DNS service lifecycle (start/stop) around the search unless
    ``simulate_max_qps`` is set or ``config["dry_run"]`` is true. Writes
    trial_results.csv, level_tests.csv and search_summary.csv/json under
    ``output_dir/max_sustainable_qps/`` as a side effect, but the return value
    is the source of truth for callers running in-process (e.g. grid_search.py).

    ``hooks`` (optional, see validity.MeasurementHooks) is called with the
    service up: on_ready(config) once it answers, trial_start/trial_end around
    every trial (their fields are added to the trial row), and
    before_stop(config) after the search. Hooks never fail the search.

    Raises ValueError on a bad config, RuntimeError or TimeoutError if the DNS
    service fails to start.
    """
    params = validate_search_config(config)
    params["simulate_max_qps"] = simulate_max_qps
    params["hooks"] = hooks
    simulating = simulate_max_qps is not None

    trial_store = ResultStore(output_dir)
    level_store = ResultStore(output_dir)
    summary_store = ResultStore(output_dir)

    log.info("=== Max Sustainable QPS Evaluation ===")
    log.info("DNS service: %s", params["dns_service"])
    log.info("Tool: %s", params["tool"].name)
    log.info("Initial QPS: %d, min QPS step: %d, max QPS: %d",
             params["initial_qps"], params["min_qps_step"], params["max_qps"])
    log.info("Level criteria: %d/%d trials at >= %.4f%% answer rate "
             "(max %d failures), %ds per trial",
             params["min_passes"], params["num_trials"],
             params["answer_rate_threshold"], params["max_fails"],
             params["trial_duration"])
    if params["collectl"]:
        log.info("collectl enabled: will sample the DNS server with margin=%ds per trial",
                 params["collectl_margin"])
    if simulating:
        log.warning("SIMULATION MODE: no remote commands will run; levels pass "
                    "iff QPS <= %d", simulate_max_qps)

    manage_service = not simulating and not config.get("dry_run")

    if manage_service:
        reset_clients(config, params["tool"])
        stop_dns_service(config)
        time.sleep(2)
        start_dns_service(config, params["dns_service"])
        wait_for_dns_ready(config, timeout=300)
    if hooks is not None and not config.get("dry_run"):
        _call_hook(hooks.on_ready, config, simulating)

    try:
        summary = run_search(config, params, trial_store, level_store)
    finally:
        if hooks is not None and not config.get("dry_run"):
            _call_hook(hooks.before_stop, config, simulating)
        if manage_service:
            try:
                reset_clients(config, params["tool"])
            except Exception as e:
                log.warning("Failed to reset clients: %s", e)
            try:
                stop_dns_service(config, params["dns_service"])
            except Exception as e:
                log.warning("Failed to stop %s: %s", params["dns_service"], e)

    summary_store.add_result(summary)
    trial_store.export_csv(SCRIPT_NAME, "trial_results.csv")
    level_store.export_csv(SCRIPT_NAME, "level_tests.csv")
    summary_store.export_csv(SCRIPT_NAME, "search_summary.csv")
    summary_store.export_json(SCRIPT_NAME, "search_summary.json")

    log.info("=== Max sustainable QPS: %d (highest QPS tested: %d, "
             "%d QPS values tested, %d trials, %.1fs) ===",
             summary["max_qps_passed"], summary["max_qps_tested"],
             summary["num_qps_values_tested"], summary["total_trials_run"],
             summary["search_duration_s"])
    if summary["hit_max_qps_ceiling"]:
        log.warning("Result is a LOWER BOUND: the max_qps ceiling passed.")

    return summary, trial_store.results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_arg_parser():
    parser = argparse.ArgumentParser(
        description="Find the maximum sustainable QPS of a DNS server"
    )
    parser.add_argument("--config", required=True, help="Path to config YAML file")
    parser.add_argument("--server", help="Name server host (user@host)")
    parser.add_argument("--clients", nargs="+",
                        help="Load-generation client hosts (user@host or localhost); "
                             "target QPS is split evenly across them")
    parser.add_argument("--client-interface", help="Network interface on client (for kxdpgun)")
    parser.add_argument("--input-file", help="Path to the dnsperf-format query input file")
    parser.add_argument("--threads", type=int, help="Number of dnsperf threads")
    parser.add_argument("--ports-per-thread", type=int, help="dnsperf ports per thread")
    parser.add_argument("--timeout", type=int, help="Query timeout in seconds")
    parser.add_argument("--pause-between-runs", type=int, help="Pause between trials in seconds")
    parser.add_argument("--tool", help="Load-generation tool: dnsperf or kxdpgun")
    parser.add_argument("--dns-service", help="DNS service to evaluate")
    parser.add_argument("--output-dir", default="results", help="Output directory for results")
    parser.add_argument("--dry-run", action="store_true", help="Print commands without executing")

    parser.add_argument("--initial-qps", type=int, help="Starting QPS for the exponential ramp")
    parser.add_argument("--min-qps-step", type=int,
                        help="Search resolution; all QPS values are integer multiples of this")
    parser.add_argument("--max-qps", type=int, help="Ceiling QPS the search will never exceed")
    parser.add_argument("--num-trials", type=int, help="Maximum trials per QPS level")
    parser.add_argument("--min-passes", type=int, help="Passing trials required for a level to pass")
    parser.add_argument("--trial-duration", type=int, help="Seconds per trial")
    parser.add_argument("--answer-rate-threshold", type=float,
                        help="Answer rate percent a trial must meet to pass")
    parser.add_argument("--min-qps-fidelity-pct", type=float,
                        help="Warn when the tool sends less than this percent of the "
                             "requested queries. Advisory only.")
    parser.add_argument("--collectl", dest="collectl", action="store_true",
                        help="Run collectl on the DNS server during each trial")
    parser.add_argument("--no-collectl", dest="collectl", action="store_false",
                        help="Disable collectl monitoring")
    parser.set_defaults(collectl=None)
    parser.add_argument("--collectl-margin", type=int,
                        help="Seconds collectl starts before and continues after each trial")
    parser.add_argument(
        "--simulate-max-qps", type=int,
        help="Self-test mode: skip all remote execution and treat a level as passing "
             "iff its QPS is <= this value. Exercises the search algorithm offline.",
    )
    return parser


def _apply_cli_overrides(config, args):
    if args.server:
        config.setdefault("hosts", {})["server"] = args.server
    if args.clients:
        config.setdefault("hosts", {})["clients"] = args.clients
    if args.client_interface:
        config["client_interface"] = args.client_interface
    if args.input_file:
        config["input_file"] = args.input_file
    if args.threads is not None:
        config["threads"] = args.threads
    if args.ports_per_thread is not None:
        config["ports_per_thread"] = args.ports_per_thread
    if args.timeout is not None:
        config["timeout"] = args.timeout
    if args.pause_between_runs is not None:
        config["pause_between_runs"] = args.pause_between_runs
    if args.tool:
        config["tool"] = args.tool
    if args.dns_service:
        config["dns_service"] = args.dns_service

    m = config.setdefault("max_sustainable_qps", {})
    overrides = {
        "initial_qps": args.initial_qps,
        "min_qps_step": args.min_qps_step,
        "max_qps": args.max_qps,
        "num_trials": args.num_trials,
        "min_passes": args.min_passes,
        "trial_duration": args.trial_duration,
        "answer_rate_threshold": args.answer_rate_threshold,
        "min_qps_fidelity_pct": args.min_qps_fidelity_pct,
        "collectl": args.collectl,
        "collectl_margin": args.collectl_margin,
    }
    for key, value in overrides.items():
        if value is not None:
            m[key] = value
    return config


def main():
    parser = _build_arg_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = load_config(args.config)
    config = _apply_cli_overrides(config, args)
    config["dry_run"] = args.dry_run

    try:
        run_max_sustainable_qps(
            config, args.output_dir, simulate_max_qps=args.simulate_max_qps,
        )
    except ValueError as e:
        log.error("Invalid configuration: %s", e)
        return 2
    except (RuntimeError, TimeoutError) as e:
        log.error("Evaluation failed: %s", e)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
