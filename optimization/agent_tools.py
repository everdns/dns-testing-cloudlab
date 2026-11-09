"""Tools the Claude agent uses to run agent_search.py.

The agent controls the search. It decides what to measure, when to repeat a
measurement to check noise, when to look at the name server, and when to stop.
SearchTools holds the tools it calls:

  measure_configuration  install, measure, restore and record one configuration
  get_results            every measurement so far
  get_run_details        per-level and per-trial data of one measurement
  inspect_server         read-only diagnostics on the name server
  finish                 end the search with a report

The tools only enforce the rules. Every value must be inside the parameter's
bounds, and it must render through the same code that bind_config.py and
system_config.py use to change the server. The run is limited to
``max_configs`` measurements. Between measurements the name server always holds
its base configuration. A bad call returns an error for the agent to fix; it
never reaches the server.

Search space: each entry in the config's ``parameters`` list may carry an
``agent:`` block describing what the agent may propose.

    - name: rmem
      values: [default, 16777216]            # shown to the agent as hints
      agent: {type: int, min: 212992, max: 268435456, allow_default: true}
    - name: netdev-budget
      values: [default, "600/4000"]
      agent: {type: pair, min: [1, 1], max: [100000, 1000000], allow_default: true}

``type`` is one of int, pair ("<a>/<b>", each part bounded), or enum (the
default when there is no ``agent:`` block; choices are ``values`` unless
``choices`` is given). ``allow_default`` defaults to whether "default" is in
``values``.
"""
import csv
import hashlib
import inspect
import json
import logging
import os
import re
import shlex
import subprocess
import threading
import time

import bind_config
import host_state
import system_config
from bind_config import BindConfigError
from knobs import KnobError
from ledger import now_iso
from max_sustainable_qps import ssh_run
from servers import ServerConfigError, get_adapter
from search import (EXIT_RESTORE_FAILED, Evaluator, point_id, restore_or_exit,
                    row_qps, split_overrides)
from system_config import SystemConfigError

log = logging.getLogger("agent_tools")

DEFAULT = system_config.DEFAULT

# Per-row fields shown to the agent, besides the parameter values.
RESULT_FIELDS = [
    "status", "error", "max_qps_passed", "max_qps_tested",
    "hit_max_qps_ceiling", "min_qps_fidelity_pct", "validity_ok",
    "validity_reasons", "repeat_of", "hypothesis",
]
MAX_TEXT = 600
MAX_TOOL_OUTPUT = 12000
PROBE_TIMEOUT = 60

SYSTEM_PROMPT = """\
You are running a performance-tuning campaign on an authoritative DNS name \
server (BIND plus Linux host network settings). The goal is the highest max \
sustainable QPS: the highest query rate at which the server still answers \
nearly every query, as measured by a load generator on separate client \
machines. You are in charge of the whole search: what to measure, in what \
order, when to repeat a measurement, when to look at the server, and when to \
stop.

Your tools:
- measure_configuration installs a configuration on the real server, measures \
it (this takes several minutes), restores the base configuration, and returns \
the result. Each call uses one measurement from a small fixed budget, even if \
it fails.
- get_results lists every measurement so far. get_run_details shows the \
per-level answer rates of one measurement. This is where you can see how \
sharply the server falls off.
- inspect_server runs read-only diagnostics on the name server: counters, \
sysctls, NIC and CPU layout. The server holds its base configuration between \
measurements, and most counters are cumulative since boot. Compare them \
before and after a measurement to see where packets were dropped.
- finish ends the campaign with your report.

How to work:
- Form hypotheses about what limits throughput (socket buffers, packet drops \
in conntrack or the backlog, NAPI budget, file descriptors, EDNS size). Test \
first the ones that could matter most, and follow up on what the results \
show. Parameters interact, so changing several at once is fine when you have \
a reason to.
- Runs are noisy. Before you build on a difference of a few percent, or \
before you recommend a winner, consider spending a measurement to repeat it \
(repeat=true).
- The first listed value of every parameter is the starting configuration. \
Measuring it gives you a baseline to compare against.

Reading a result:
- max_qps_passed is the score. Higher is better.
- hit_max_qps_ceiling=True means the search reached its QPS ceiling. The \
score is then a lower bound, not the server's limit.
- A min_qps_fidelity_pct below the configured threshold means the load \
generator could not send the target rate. That score reflects the client, not \
the server.
- status config_failed means the configuration could not be applied (see \
error). timeout or failed means the measurement did not finish.

When you have used the budget, or you are confident more measurements cannot \
beat the best result, call finish. Recommend the configuration you trust most, \
say how sure you are and why, and explain what you learned about what limits \
this server. Call finish exactly once, then end your turn."""


class AgentError(Exception):
    pass


# ---------------------------------------------------------------------------
# Search space
# ---------------------------------------------------------------------------

def _is_default(value):
    return isinstance(value, str) and value.strip().lower() == DEFAULT


def build_search_space(parameters):
    """Per-parameter specs from the config's ``parameters`` list."""
    space = []
    for p in parameters:
        agent = dict(p.get("agent") or {})
        kind = agent.get("type", "enum")
        spec = {
            "name": p["name"],
            "type": kind,
            "hints": list(p.get("values") or []),
            "description": agent.get("description", ""),
        }
        if kind == "enum":
            spec["choices"] = list(agent.get("choices") or p["values"])
        elif kind in ("int", "pair"):
            if "min" not in agent or "max" not in agent:
                raise ValueError(f"parameter '{p['name']}': agent type {kind} "
                                 "needs min and max")
            spec["min"], spec["max"] = agent["min"], agent["max"]
            spec["allow_default"] = agent.get(
                "allow_default", any(_is_default(v) for v in spec["hints"]))
            if kind == "pair" and not (len(spec["min"]) == len(spec["max"]) == 2):
                raise ValueError(f"parameter '{p['name']}': pair min/max must "
                                 "be two-element lists")
        else:
            raise ValueError(f"parameter '{p['name']}': unknown agent type {kind!r}")
        space.append(spec)
    return space


def _value_schema(spec):
    if spec["type"] == "enum":
        return {"type": "string", "enum": [str(c) for c in spec["choices"]]}
    base = {"type": "integer"} if spec["type"] == "int" else {"type": "string"}
    if spec.get("allow_default"):
        return {"anyOf": [base, {"type": "string", "enum": [DEFAULT]}]}
    return base


def configuration_schema(space):
    return {
        "type": "object",
        "properties": {spec["name"]: _value_schema(spec) for spec in space},
        "required": [spec["name"] for spec in space],
        "additionalProperties": False,
    }


def describe_space(space):
    """The search space as the agent sees it."""
    out = []
    for spec in space:
        entry = {"name": spec["name"], "type": spec["type"]}
        if spec["description"]:
            entry["description"] = spec["description"]
        if spec["type"] == "enum":
            entry["choices"] = spec["choices"]
        else:
            entry["min"], entry["max"] = spec["min"], spec["max"]
            entry["default_allowed"] = spec["allow_default"]
            if spec["type"] == "pair":
                entry["format"] = "'<first>/<second>', each part within its min/max"
            entry["suggested_values"] = spec["hints"]
        out.append(entry)
    return out


def _check_int(name, value, low, high):
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name}: expected an integer, got {value!r}")
    if isinstance(value, float) and value != number:
        raise ValueError(f"{name}: expected an integer, got {value!r}")
    if not low <= number <= high:
        raise ValueError(f"{name}: {number} is outside [{low}, {high}]")
    return number


def _normalize(spec, value):
    name = spec["name"]
    if spec["type"] == "enum":
        for choice in spec["choices"]:
            if str(choice) == str(value):
                return choice
        raise ValueError(f"{name}: {value!r} is not one of {spec['choices']}")
    if _is_default(value):
        if not spec["allow_default"]:
            raise ValueError(f"{name}: '{DEFAULT}' is not allowed")
        return DEFAULT
    if spec["type"] == "int":
        return _check_int(name, value, spec["min"], spec["max"])
    parts = str(value).split("/")
    if len(parts) != 2:
        raise ValueError(f"{name}: expected '<first>/<second>', got {value!r}")
    first, second = (_check_int(name, part.strip(), low, high)
                     for part, low, high in zip(parts, spec["min"], spec["max"]))
    return f"{first}/{second}"


def render_check(name, value):
    """Dry render of one parameter value: the same code that builds the real
    change on the server. Raises BindConfigError or SystemConfigError."""
    if system_config.is_system_parameter(name):
        system_config.render_commands({name: value})
    else:
        bind_config.render_statement(name, value)


def validate_configuration(space, configuration, check=render_check):
    """Return (normalized configuration, errors). The configuration is ordered
    like the search space; errors is empty when it is usable. ``check(name,
    value)`` raises for a value that cannot be applied (default: the BIND /
    system_config dry render; knob_space.py passes a knobs.yaml render)."""
    if not isinstance(configuration, dict):
        return {}, ["configuration must be an object mapping parameter names to values"]
    errors = []
    normalized = {}
    names = {spec["name"] for spec in space}
    for extra in sorted(set(configuration) - names):
        errors.append(f"{extra}: not a parameter of this search")
    for spec in space:
        name = spec["name"]
        if name not in configuration:
            errors.append(f"{name}: missing")
            continue
        try:
            value = _normalize(spec, configuration[name])
        except ValueError as e:
            errors.append(str(e))
            continue
        try:
            check(name, value)
        except (BindConfigError, SystemConfigError, KnobError) as e:
            errors.append(str(e))
            continue
        normalized[name] = value
    return normalized, errors


# ---------------------------------------------------------------------------
# Server diagnostics
#
# Fixed, read-only commands. The agent picks one by name; it cannot run
# anything else on the server.
# ---------------------------------------------------------------------------

SERVER_PROBES = {
    "udp_counters": (
        "UDP and IP counters since boot (receive/send buffer errors, "
        "no-port, in-errors)",
        "nstat -az 2>/dev/null | grep -E '^(Udp|Ip)' || cat /proc/net/snmp",
    ),
    "softnet_stat": (
        "Per-CPU /proc/net/softnet_stat (hex columns: processed, dropped "
        "(backlog full), time_squeeze (NAPI budget exhausted))",
        "cat /proc/net/softnet_stat",
    ),
    "sysctls": (
        "Current net.core, UDP and conntrack sysctls",
        "sysctl -a 2>/dev/null | grep -E '^(net\\.core\\.|net\\.ipv4\\.udp|"
        "net\\.netfilter\\.nf_conntrack_(max|count|buckets))'",
    ),
    "firewall": (
        "ufw status, iptables raw table, conntrack usage",
        "sudo ufw status verbose; echo; sudo iptables -t raw -S; echo; "
        "sudo sysctl net.netfilter.nf_conntrack_count "
        "net.netfilter.nf_conntrack_max 2>&1",
    ),
    "nic": (
        "Interfaces with packet counters, channel/ring sizes, and nonzero "
        "drop/error NIC statistics",
        "ip -s link; for i in $(ls /sys/class/net | grep -v '^lo$'); do "
        "echo \"== $i\"; ethtool -l $i 2>/dev/null; ethtool -g $i 2>/dev/null; "
        "sudo ethtool -S $i 2>/dev/null | grep -Ei 'drop|miss|err|fifo|no_buf' "
        "| grep -Ev ': 0$'; done",
    ),
    "cpu": (
        "CPU topology, load, and how NIC interrupts are spread over CPUs",
        "lscpu; echo; cat /proc/loadavg; echo; "
        "grep -E 'CPU|eth|eno|ens|mlx|ice|i40e' /proc/interrupts | cut -c1-400",
    ),
    "named": (
        "BIND version, named's service limits, and the installed "
        f"{bind_config.INSTALLED_OPTIONS_PATH}",
        "named -V 2>&1 | head -5; systemctl show named -p LimitNOFILE "
        "-p ActiveState 2>&1; echo; "
        f"sudo cat {bind_config.INSTALLED_OPTIONS_PATH}",
    ),
    "memory": (
        "Memory and socket usage",
        "free -m; echo; cat /proc/net/sockstat",
    ),
}


# ---------------------------------------------------------------------------
# Transcript
# ---------------------------------------------------------------------------

class Transcript:
    """Appends one JSON object per event to a .jsonl file."""

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()

    def record(self, event, **fields):
        if not self.path:
            return
        with self._lock:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "a") as f:
                f.write(json.dumps({"time": time.time(), "event": event, **fields},
                                   default=str) + "\n")


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def _clip(value, limit=MAX_TEXT):
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "..."


def _read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return list(csv.DictReader(f))


class SearchTools:
    """The tools the agent calls. Calls run one at a time, so the name
    server only ever sees one change at a time."""

    def __init__(self, space, evaluator, context, max_configs, transcript,
                 server, dry_run=False, check=render_check):
        self.space = space
        self.check = check   # validate_configuration's per-value dry render
        self.evaluator = evaluator
        self.context = context
        self.max_configs = max_configs
        self.transcript = transcript
        self.server = server
        self.dry_run = dry_run
        self.system_prompt = SYSTEM_PROMPT
        self.report = None
        self.dry_run_done = False
        # Set by the runner (e.g. a usage limit was reached): no further
        # measurements, the agent should write its report.
        self.stop_reason = None
        self._lock = threading.Lock()
        self._handlers = {
            "measure_configuration": self.measure_configuration,
            "get_results": self.get_results,
            "get_run_details": self.get_run_details,
            "inspect_server": self.inspect_server,
            "finish": self.finish,
        }

    # -- bookkeeping --------------------------------------------------------

    @property
    def done(self):
        """True once the agent has finished (or a dry run has shown its
        first configuration)."""
        return self.report is not None or self.dry_run_done

    def used(self):
        return len(self.evaluator.rows)

    def remaining(self):
        return max(0, self.max_configs - self.used())

    def best(self):
        """(point_id, qps) of the highest-scoring row, or None."""
        best = None
        for row in self.evaluator.rows.values():
            qps = row_qps(row)
            if qps is not None and (best is None or qps > best[1]):
                best = (row["point_id"], qps)
        return best

    def _row_view(self, row):
        entry = {"point_id": row["point_id"],
                 "configuration": {spec["name"]: row.get(spec["name"], "")
                                   for spec in self.space}}
        for field in RESULT_FIELDS:
            value = row.get(field, "")
            if value not in ("", None):
                entry[field] = _clip(value) if isinstance(value, str) else value
        return entry

    def _budget(self):
        best = self.best()
        return {"measurements_used": self.used(),
                "measurements_remaining": self.remaining(),
                "best_so_far": ({"point_id": best[0], "max_qps_passed": best[1]}
                                if best else None)}

    # -- tool definitions ---------------------------------------------------

    def definitions(self):
        """Tool definitions (name, description, input_schema)."""
        return [
            {
                "name": "measure_configuration",
                "description": (
                    "Install a configuration on the name server, measure its max "
                    "sustainable QPS (several minutes), restore the base "
                    "configuration, and return the result with its per-level "
                    "answer rates. Uses one measurement from the budget even if it "
                    "fails. Give every parameter. A configuration that was already "
                    "measured is rejected unless repeat=true, which measures it "
                    "again to gauge run-to-run noise."),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "configuration": configuration_schema(self.space),
                        "hypothesis": {
                            "type": "string",
                            "description": "What you expect this measurement to "
                                           "show, and why it is worth the budget"},
                        "repeat": {
                            "type": "boolean",
                            "description": "Measure an already-measured "
                                           "configuration again"},
                    },
                    "required": ["configuration", "hypothesis"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "get_results",
                "description": "Every measurement so far, with the remaining budget "
                               "and the best result.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "sort_by": {"type": "string", "enum": ["order", "qps"],
                                    "description": "order measured (default) or "
                                                   "max_qps_passed, best first"},
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": "get_run_details",
                "description": "Per-QPS-level results (passes, fails, average "
                               "answer rate) and per-trial results of one "
                               "measurement, by point_id.",
                "input_schema": {
                    "type": "object",
                    "properties": {"point_id": {"type": "string"}},
                    "required": ["point_id"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "inspect_server",
                "description": "Run a fixed read-only diagnostic on the name "
                               "server. Probes: " + "; ".join(
                                   f"{name}: {desc}"
                                   for name, (desc, _) in SERVER_PROBES.items()),
                "input_schema": {
                    "type": "object",
                    "properties": {"probe": {"type": "string",
                                             "enum": list(SERVER_PROBES)}},
                    "required": ["probe"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "finish",
                "description": "End the search. Call exactly once, when the budget "
                               "is used or further measurements cannot help.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "recommended_point_id": {
                            "type": "string",
                            "description": "point_id of the configuration you "
                                           "recommend, or empty if none worked"},
                        "summary": {
                            "type": "string",
                            "description": "Your report: the recommendation and "
                                           "how sure you are, what limits this "
                                           "server, and what you would try next"},
                    },
                    "required": ["recommended_point_id", "summary"],
                    "additionalProperties": False,
                },
            },
        ]

    def initial_prompt(self):
        rows = [self._row_view(r) for r in self.evaluator.rows.values()]
        previous = ("\n".join(json.dumps(r) for r in rows) if rows
                    else "(nothing measured yet)")
        dry = ("\n\nThis is a DRY RUN: measure_configuration only shows what "
               "would be applied to the server, and the run ends after your "
               "first call to it. Choose the configuration you would measure "
               "first." if self.dry_run else "")
        return (
            "<measurement_setup>\n"
            f"{json.dumps(self.context, indent=2, default=str)}\n"
            "</measurement_setup>\n\n"
            "<search_space>\n"
            f"{json.dumps(describe_space(self.space), indent=2, default=str)}\n"
            "</search_space>\n\n"
            f"<previous_results count=\"{len(rows)}\">\n{previous}\n"
            "</previous_results>\n\n"
            f"Measurement budget: {self.max_configs} in total, "
            f"{self.remaining()} remaining.\n\n"
            "Find the configuration with the highest max sustainable QPS. "
            "Plan how to spend the budget, then begin." + dry
        )

    # -- dispatch -----------------------------------------------------------

    def call(self, name, arguments):
        """Run one tool. Returns (text, is_error); never raises for a bad call."""
        handler = self._handlers.get(name)
        if handler is None:
            return f"unknown tool {name!r}", True
        if not isinstance(arguments, dict):
            return "tool input must be a JSON object", True
        with self._lock:
            self.transcript.record("tool_call", tool=name, input=arguments)
            try:
                inspect.signature(handler).bind(**arguments)
            except TypeError as e:
                result, is_error = f"bad arguments for {name}: {e}", True
            else:
                try:
                    result, is_error = handler(**arguments), False
                except AgentError as e:
                    result, is_error = str(e), True
            text = (result if isinstance(result, str)
                    else json.dumps(result, indent=1, default=str))
            text = _clip(text, MAX_TOOL_OUTPUT)
            self.transcript.record("tool_result", tool=name, is_error=is_error,
                                   text=text)
        if is_error:
            log.warning("Tool %s rejected: %s", name, _clip(text, 300))
        return text, is_error

    # -- tools --------------------------------------------------------------

    def measure_configuration(self, configuration, hypothesis, repeat=False):
        if self.report is not None:
            raise AgentError("the search has already finished")
        if self.stop_reason:
            raise AgentError(f"no more measurements: {self.stop_reason}. Call "
                             "finish now with your report.")
        if self.remaining() == 0:
            raise AgentError("the measurement budget is used up; call finish")
        overrides, errors = validate_configuration(self.space, configuration, self.check)
        if errors:
            raise AgentError("configuration rejected:\n- " + "\n- ".join(errors))

        base_id = point_id(overrides)
        pid = base_id
        if base_id in self.evaluator.rows:
            if not repeat:
                raise AgentError(
                    "this configuration was already measured: "
                    + json.dumps(self._row_view(self.evaluator.rows[base_id]))
                    + "\nChange it, or pass repeat=true to measure it again.")
            n = 2
            while f"{base_id}__rep{n}" in self.evaluator.rows:
                n += 1
            pid = f"{base_id}__rep{n}"

        log.info("Agent hypothesis: %s", hypothesis)
        if self.dry_run:
            self.dry_run_done = True
            bind_overrides, system_overrides = split_overrides(overrides)
            statements = [bind_config.render_statement(n, v)
                          for n, v in bind_overrides.items()]
            commands = system_config.render_commands(system_overrides)
            log.info("[dry run] %s", pid)
            for line in statements:
                log.info("[dry run] named.conf.options: %s", line)
            for line in commands:
                log.info("[dry run] host: %s", line)
            return {
                "dry_run": True,
                "point_id": pid,
                "named.conf.options statements": statements,
                "host commands": commands,
                "note": "Nothing was applied or measured. Call finish now.",
            }

        label = f"[agent {self.used() + 1}/{self.max_configs}]"
        extra = {"iteration": self.used() + 1, "hypothesis": hypothesis}
        if pid != base_id:
            extra["repeat_of"] = base_id
        row = self.evaluator.evaluate(overrides, label, extra, pid=pid)
        return {**self._row_view(row),
                "levels": self._levels(row),
                **self._budget()}

    def get_results(self, sort_by="order"):
        rows = list(self.evaluator.rows.values())
        if sort_by == "qps":
            rows.sort(key=lambda r: row_qps(r) or -1, reverse=True)
        return {**self._budget(),
                "results": [self._row_view(r) for r in rows]}

    def _run_files(self, row):
        run_dir = row.get("run_dir") or ""
        base = os.path.join(run_dir, "max_sustainable_qps")
        return (_read_csv(os.path.join(base, "level_tests.csv")),
                _read_csv(os.path.join(base, "trial_results.csv")))

    def _levels(self, row):
        """Compact per-level view: [target_qps, avg answer rate %, passed]."""
        levels, _ = self._run_files(row)
        return [[lvl.get("target_qps"), lvl.get("average_answer_rate_pct"),
                 lvl.get("passed")] for lvl in levels]

    def get_run_details(self, point_id):
        row = self.evaluator.rows.get(point_id)
        if row is None:
            raise AgentError(f"no measurement with point_id {point_id!r}; "
                             "see get_results")
        levels, trials = self._run_files(row)
        keep = ["target_qps", "trial", "achieved_qps", "answer_rate_pct",
                "qps_fidelity_pct", "passed", "status"]
        return {**self._row_view(row),
                "levels": levels,
                "trials": [{k: t.get(k) for k in keep} for t in trials]}

    def inspect_server(self, probe):
        if probe not in SERVER_PROBES:
            raise AgentError(f"unknown probe {probe!r}; choose from "
                             f"{list(SERVER_PROBES)}")
        _, command = SERVER_PROBES[probe]
        try:
            result = ssh_run(self.server, command, timeout=PROBE_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise AgentError(f"probe {probe} timed out after {PROBE_TIMEOUT}s")
        except OSError as e:
            raise AgentError(f"probe {probe} could not run: {e}")
        output = (result.stdout or "") + (
            f"\n[stderr]\n{result.stderr}" if (result.stderr or "").strip() else "")
        return f"[{probe} on {self.server}, rc={result.returncode}]\n{output}"

    def finish(self, recommended_point_id, summary):
        if self.report is not None:
            return "already finished"
        if recommended_point_id and recommended_point_id not in self.evaluator.rows:
            raise AgentError(f"no measurement with point_id "
                             f"{recommended_point_id!r}; see get_results")
        row = self.evaluator.rows.get(recommended_point_id)
        self.report = {
            "recommended_point_id": recommended_point_id or None,
            "recommended_configuration": ({spec["name"]: row.get(spec["name"])
                                           for spec in self.space} if row else None),
            "recommended_max_qps_passed": row_qps(row) if row else None,
            "summary": summary,
            **self._budget(),
        }
        log.info("Agent finished. Recommended: %s", recommended_point_id or "(none)")
        return "Search finished. End your turn now."


# ---------------------------------------------------------------------------
# Open mode (agent_search.py --mode open)
#
# No parameter list: a measurement is any set of named.conf.options
# statements plus any root shell commands on the name server, optionally
# followed by a reboot. host_state.py puts the server back afterwards.
# ---------------------------------------------------------------------------

OPEN_SYSTEM_PROMPT = """\
You are running a performance-tuning campaign on an authoritative DNS name \
server ({server_name} on Linux). The goal is the highest max sustainable QPS: the \
highest query rate at which the server still answers nearly every query, as \
measured by a load generator on separate client machines. You are in charge \
of the whole search: what to change, what to measure, when to repeat a \
measurement, when to look at the server, and when to stop.

You may change anything on the name server. A configuration is:
- {options_field}: {statement_help}. The server's own config checker \
validates the result. The protected settings ({protected}) are refused.
- host_commands: any shell commands, each run as root with bash on the name \
server, in order, before the measurement: sysctls, ethtool (rings, channels, \
coalescing, offloads, RSS, ntuple, flow hashing), IRQ affinity, RPS/XPS, \
CPU governor/frequency/C-states, THP, irqbalance and other services, systemd \
drop-ins for the server (CPU affinity, limits, scheduling), the server's \
command-line options, the GRUB kernel command line (edit /etc/default/grub, run update-grub, \
set reboot_after_apply=true), modules, and so on. A failing command (nonzero \
exit) aborts the measurement as config_failed.
- revert_commands: run as root after the measurement, before the automatic \
restore. Use them to undo anything the automatic restore does not cover.
- reboot_after_apply: reboot the server after host_commands (needed for \
kernel command-line changes; several minutes each way, because restoring also \
reboots). The server boots the kernel it started on, unless your \
host_commands run grub-reboot or grub-set-default.

After every measurement the server is put back automatically. That covers: \
every writable sysctl; IRQ affinity, RPS/XPS, NIC mtu/txqueuelen/\
gro_flush_timeout/napi settings; CPU online state, governors, frequencies, \
C-state disable flags, intel_pstate, THP; writable module parameters; per-NIC \
ethtool rings, channels, coalescing, offloads, RSS table and key, ntuple \
rules, rx-flow-hash and root qdisc; running services and the tuned profile; \
iptables/ip6tables rules; modules you loaded; and the files under \
/etc/default/grub*, /etc/sysctl*, /etc/modprobe.d, /etc/modules-load.d, \
/etc/systemd/system, /etc/security/limits*, /etc/bind, /etc/knot, /etc/nsd, \
/etc/default/named, /etc/ufw, \
/etc/udev/rules.d, /etc/netplan and /etc/tuned (then the kernel command line, \
with a reboot if it changed). Anything else you change must be undone by your \
revert_commands. Every result reports residual_drift: what still differs from \
the starting state after the restore. If it is not empty, later measurements \
no longer start from the same baseline. Fix it (a measurement with only \
host_commands that undo it) or account for it.

Do not break the server's SSH access or the experiment network (the hosts and \
client_interface in the measurement setup). The harness cannot recover from \
that, and the run ends.

Your tools:
- measure_configuration applies a configuration, measures it (several \
minutes), restores the server, and returns the result. Each call uses one \
measurement from a small fixed budget, even if it fails. The empty \
configuration (no statements, no commands) is the baseline.
- get_results lists every measurement so far. get_run_details shows one \
measurement in full, with its per-level answer rates.
- inspect_server runs any shell command on the name server (as your user; \
use sudo when needed) and returns its output. Use it to read counters, \
settings and hardware before deciding what to change. It is meant for \
reading. Anything you change with it is not undone for you and is not part \
of a measured configuration.
- finish ends the campaign with your report.

How to work:
- Look at the hardware first (CPUs, NUMA, NIC driver and queues, IRQs), then \
form hypotheses about what limits throughput and test the ones that could \
matter most first. Compare drop counters (softnet_stat, UDP RcvbufErrors, NIC \
drops) before and after a measurement to see where packets are lost.
- Runs are noisy. Before you build on a difference of a few percent, or \
before you recommend a winner, consider repeating it (repeat=true).

Reading a result:
- max_qps_passed is the score. Higher is better.
- hit_max_qps_ceiling=True means the search reached its QPS ceiling. The \
score is then a lower bound, not the server's limit.
- A min_qps_fidelity_pct below the configured threshold means the load \
generator could not send the target rate. That score reflects the client, not \
the server.
- status config_failed means the configuration could not be applied (see \
error). timeout or failed means the measurement did not finish.

When you have used the budget, or you are confident more measurements cannot \
beat the best result, call finish. Recommend the configuration you trust most, \
say how sure you are and why, and explain what you learned about what limits \
this server. Call finish exactly once, then end your turn."""

# Appended to the open-mode prompt: the research needs every change described
# in one vocabulary, and only valid measurements count.
KNOBS_PROMPT = """

Knobs (required): this search is part of a study of which tuning variables \
matter on which hardware, so every measurement must say which catalog knobs \
it sets, in the knobs argument ({"knob": value}; {} for the baseline). The \
catalog is in <knob_catalog>. Two ways to use it:
- Give knobs and leave {options_field} and host_commands empty: the harness \
renders the knobs into the exact commands for this server. Prefer this \
whenever the catalog covers what you want, so results are comparable across \
machines.
- Give your own {options_field}/host_commands and list in knobs the catalog \
knobs they implement. For a technique that no knob covers, call propose_knob \
once (name, description, the commands) so it can be added to the catalog, and \
mention it in your hypothesis.

Validity: every measurement is also checked for correct answers (a sample of \
queries is compared with the stock server's answers before, during and after \
the load) and for an unchanged zone file and query input. validity_ok=False \
means the score does not count, whatever max_qps_passed says; \
validity_reasons says why. Never change the zone data, the query input, or \
what the server answers: only how fast it answers."""

CHANGE_FIELDS = ["named_options", "host_commands", "revert_commands"]
HOST_COMMAND_TIMEOUT = 900
INSPECT_TIMEOUT = 120


def _string_list(name, value):
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise AgentError(f"{name} must be a list of strings")
    return [v for v in value if v.strip()]


def encode_change(label, named_options, host_commands, revert_commands, reboot,
                  knobs=None):
    """A change as a row's override fields (JSON strings, so they survive CSV).
    ``named_options`` holds the server's config statements, whatever the
    software; ``knobs`` are the knobs.yaml names the change implements."""
    return {"label": label,
            "named_options": json.dumps(named_options),
            "host_commands": json.dumps(host_commands),
            "revert_commands": json.dumps(revert_commands),
            "reboot_after_apply": bool(reboot),
            "knobs": json.dumps(knobs or {}, sort_keys=True)}


def decode_change(row):
    """The change recorded in a row (or override fields), as plain values."""
    change = {"label": row.get("label", "")}
    for field in CHANGE_FIELDS:
        value = row.get(field) or "[]"
        change[field] = json.loads(value) if isinstance(value, str) else list(value)
    reboot = row.get("reboot_after_apply", False)
    change["reboot_after_apply"] = (reboot if isinstance(reboot, bool)
                                    else str(reboot).lower() == "true")
    knobs = row.get("knobs") or "{}"
    change["knobs"] = json.loads(knobs) if isinstance(knobs, str) else dict(knobs)
    return change


def change_id(change):
    """Point id: the label, plus a hash of what is actually applied."""
    content = {k: change[k] for k in CHANGE_FIELDS + ["reboot_after_apply"]}
    digest = hashlib.sha1(json.dumps(content, sort_keys=True).encode()).hexdigest()[:10]
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", change["label"]).strip("-")[:60]
    return f"{slug or 'change'}__{digest}"


def apply_script(change, config_path=bind_config.INSTALLED_OPTIONS_PATH):
    """A shell script that applies ``change`` by hand on the name server."""
    lines = ["#!/bin/bash", f"# {change['label']}", "set -e", ""]
    if change["named_options"]:
        lines.append(f"# Add to {config_path}:")
        lines += [f"#     {s}" for s in change["named_options"]]
        lines.append("")
    lines += change["host_commands"]
    if change["reboot_after_apply"]:
        lines += ["", "sudo systemctl reboot"]
    return "\n".join(lines) + "\n"


def run_as_root(server, command, timeout=HOST_COMMAND_TIMEOUT):
    """Run one agent command as root on ``server``; returns CompletedProcess."""
    wrapped = f"timeout {timeout} sudo bash -c {shlex.quote(command)}"
    return ssh_run(server, wrapped, timeout=timeout + 30)


class OpenEvaluator(Evaluator):
    """Evaluator whose points are free-form changes, restored by host_state.
    ``adapter`` (servers/) is the server software whose config the change's
    statements go into (default BIND)."""

    def __init__(self, config, server, base_text, host_baseline, output_dir,
                 results_name, timeout, resumed_rows=(), reboot_timeout=900,
                 adapter=None, **kwargs):
        super().__init__(config, server, base_text, None, output_dir,
                         results_name, timeout, resumed_rows, **kwargs)
        self.host_baseline = host_baseline
        self.reboot_timeout = reboot_timeout
        self.adapter = adapter or get_adapter("bind")

    def knobs_for(self, overrides):
        return decode_change(overrides)["knobs"]

    def change_for(self, overrides):
        change = decode_change(overrides)
        change.pop("knobs", None)
        return change

    def _save_applied(self, run_dir, **fields):
        os.makedirs(run_dir, exist_ok=True)
        path = os.path.join(run_dir, "applied.json")
        data = {}
        if os.path.exists(path):
            with open(path) as f:
                data = json.load(f)
        data.update(fields)
        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)

    def _apply(self, overrides, run_dir):
        change = decode_change(overrides)
        self._save_applied(run_dir, change=change)
        rendered = self.adapter.render(self.base_text, change["named_options"])
        self.adapter.install(self.server, rendered)
        for command in change["host_commands"]:
            log.info("host (root): %s", command)
            try:
                result = run_as_root(self.server, command)
            except subprocess.TimeoutExpired:
                raise SystemConfigError(f"timed out: {command}")
            if result.returncode != 0:
                raise SystemConfigError(
                    f"rc={result.returncode}: {command}\n"
                    f"{_clip((result.stderr or result.stdout or '').strip(), 1500)}")
        if change["reboot_after_apply"]:
            # Stay on the search's kernel unless the agent picked one itself.
            picks_kernel = any("grub-reboot" in c or "grub-set-default" in c
                               for c in change["host_commands"])
            kernel = None if picks_kernel else self.host_baseline.get("kernel")
            try:
                host_state.reboot_and_wait(self.server, self.reboot_timeout, kernel)
            except host_state.HostStateError as e:
                raise SystemConfigError(f"reboot failed: {e}")

    def _restore(self, overrides, run_dir):
        change = decode_change(overrides)
        revert_errors = []
        for command in change["revert_commands"]:
            try:
                result = run_as_root(self.server, command)
                if result.returncode != 0:
                    revert_errors.append(
                        f"rc={result.returncode}: {command}: "
                        f"{_clip((result.stderr or '').strip(), 300)}")
            except subprocess.TimeoutExpired:
                revert_errors.append(f"timed out: {command}")
        drift, restore_errors = self.restore_host()
        self._save_applied(run_dir, revert_errors=revert_errors,
                     restore_errors=restore_errors,
                     residual_drift=host_state.summarize(drift, limit=200))
        return {"revert_errors": json.dumps(revert_errors + restore_errors),
                "residual_drift": json.dumps(host_state.summarize(drift))}

    def restore_host(self):
        """Put the whole server back: host state, then named.conf.options.
        Stops the search if the server cannot be reached or read."""
        try:
            drift, errors = host_state.restore(self.server, self.host_baseline,
                                               self.reboot_timeout)
        except host_state.HostStateError as e:
            log.error("Could not restore the host state on %s: %s", self.server, e)
            log.error("The name server is in an unknown state. Check it (or "
                      "reboot it) before running again.")
            raise SystemExit(EXIT_RESTORE_FAILED)
        restore_or_exit(self.server, self.base_text, adapter=self.adapter)
        return drift, errors


def _fill(template, **values):
    """str.format for prompts that also contain literal braces."""
    for name, value in values.items():
        template = template.replace("{" + name + "}", str(value))
    return template


class OpenSearchTools(SearchTools):
    """SearchTools for open mode: free-form changes instead of parameters.

    With a knobs.Catalog, every measurement carries knob tags (validated
    against the catalog), a knobs-only measurement is rendered from the
    catalog, and propose_knob records techniques the catalog lacks.
    ``extra_context`` (e.g. this environment's ledger summary and findings
    from other environments) is added to the first prompt."""

    def __init__(self, hints, evaluator, context, max_configs, transcript,
                 server, dry_run=False, catalog=None, facts=None,
                 extra_context="", proposals_path=None):
        super().__init__([], evaluator, context, max_configs, transcript,
                         server, dry_run)
        self.hints = hints
        self.catalog = catalog
        self.facts = facts or {}
        self.extra_context = extra_context
        self.proposals_path = proposals_path
        self.adapter = getattr(evaluator, "adapter", None) or get_adapter("bind")
        self.options_field = ("named_options" if self.adapter.name == "bind"
                              else "server_options")
        prompt = OPEN_SYSTEM_PROMPT + (KNOBS_PROMPT if catalog else "")
        self.system_prompt = _fill(
            prompt, server_name={"bind": "BIND", "knot": "Knot DNS",
                                 "nsd": "NSD"}.get(self.adapter.name, self.adapter.name),
            options_field=self.options_field,
            statement_help=self.adapter.statement_help,
            protected=", ".join(self.adapter.forbidden))
        if catalog:
            self._handlers["propose_knob"] = self.propose_knob

    def _change_view(self, row, full=False):
        change = decode_change(row)
        if full:
            return change
        return {k: ([_clip(v, 200) for v in change[k]] if k in CHANGE_FIELDS
                    else change[k])
                for k in change if change[k] not in ([], False)}

    def _row_view(self, row, full=False):
        entry = {"point_id": row["point_id"], "change": self._change_view(row, full)}
        for field in RESULT_FIELDS:
            value = row.get(field, "")
            if value not in ("", None):
                entry[field] = _clip(value) if isinstance(value, str) else value
        for field in ("residual_drift", "revert_errors"):
            value = json.loads(row.get(field) or "[]")
            if value:
                entry[field] = value
        return entry

    def definitions(self):
        definitions = super().definitions()
        strings = {"type": "array", "items": {"type": "string"}}
        for d in definitions:
            if d["name"] == "measure_configuration":
                d["description"] = (
                    "Apply a change to the name server (named.conf.options "
                    "statements, root shell commands, optional reboot), measure "
                    "its max sustainable QPS (several minutes), restore the "
                    "server, and return the result with per-level answer rates "
                    "and any residual drift. Uses one measurement from the "
                    "budget even if it fails. The empty change is the baseline. "
                    "A change that was already measured is rejected unless "
                    "repeat=true.")
                d["input_schema"] = {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string",
                                  "description": "Short name for this change, "
                                                 "e.g. 'baseline' or 'rss16-coalesce0'"},
                        self.options_field: {**strings, "description":
                            self.adapter.statement_help},
                        "host_commands": {**strings, "description":
                            "Shell commands run as root with bash, in order"},
                        "revert_commands": {**strings, "description":
                            "Commands run as root after the measurement, before "
                            "the automatic restore"},
                        "reboot_after_apply": {"type": "boolean", "description":
                            "Reboot after host_commands (kernel command line)"},
                        "hypothesis": {"type": "string", "description":
                            "What you expect this measurement to show, and why "
                            "it is worth the budget"},
                        "repeat": {"type": "boolean", "description":
                            "Measure an already-measured change again"},
                    },
                    "required": ["label", "hypothesis"],
                    "additionalProperties": False,
                }
                if self.catalog:
                    d["input_schema"]["properties"]["knobs"] = {
                        "type": "object", "description":
                            "Catalog knobs this change sets, {name: value} ({} "
                            "for the baseline). Alone, they are rendered into "
                            "the commands for you."}
                    d["input_schema"]["required"].append("knobs")
            elif d["name"] == "inspect_server":
                d["description"] = (
                    "Run a shell command on the name server (as your user; use "
                    "sudo when needed) and return its output, or run one of the "
                    "named probes. Probes: " + "; ".join(
                        f"{name}: {desc}" for name, (desc, _) in SERVER_PROBES.items()))
                d["input_schema"] = {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string",
                                    "description": "Any shell command"},
                        "probe": {"type": "string", "enum": list(SERVER_PROBES)},
                    },
                    "additionalProperties": False,
                }
            elif d["name"] == "get_run_details":
                d["description"] = ("The full change, per-QPS-level results and "
                                    "per-trial results of one measurement, by "
                                    "point_id.")
        if self.catalog:
            definitions.insert(-1, {
                "name": "propose_knob",
                "description": "Record a tuning technique the knob catalog does "
                               "not cover, so it can be added and compared "
                               "across machines. Does not measure anything.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string",
                                 "description": "snake_case knob name"},
                        "description": {"type": "string"},
                        "scope": {"type": "string", "enum": ["host", "server"]},
                        "host_commands": strings,
                        "server_options": strings,
                        "evidence": {"type": "string", "description":
                            "point_ids and results that show it matters"},
                    },
                    "required": ["name", "description", "scope"],
                    "additionalProperties": False,
                },
            })
        return definitions

    def initial_prompt(self):
        rows = [self._row_view(r) for r in self.evaluator.rows.values()]
        previous = ("\n".join(json.dumps(r) for r in rows) if rows
                    else "(nothing measured yet)")
        dry = ("\n\nThis is a DRY RUN: measure_configuration only shows what "
               "would be applied to the server, and the run ends after your "
               "first call to it. inspect_server works normally. Choose the "
               "change you would measure first." if self.dry_run else "")
        return (
            "<measurement_setup>\n"
            f"{json.dumps(self.context, indent=2, default=str)}\n"
            "</measurement_setup>\n\n"
            f"<base_config file=\"{self.adapter.config_path}\">\n"
            f"{self.evaluator.base_text}\n"
            "</base_config>\n\n"
            + (f"<server_facts>\n{json.dumps(self.facts, indent=2, default=str)}\n"
               "</server_facts>\n\n" if self.facts else "")
            + ("<knob_catalog>\n"
               f"{json.dumps(self.catalog.describe(self.adapter.name), indent=1, default=str)}\n"
               "</knob_catalog>\n\n" if self.catalog else
               "<known_knobs>\n"
               "Settings that earlier searches tried. They are ideas, not limits.\n"
               f"{json.dumps(self.hints, indent=2, default=str)}\n"
               "</known_knobs>\n\n")
            + (f"{self.extra_context}\n\n" if self.extra_context else "")
            + 
            f"<previous_results count=\"{len(rows)}\">\n{previous}\n"
            "</previous_results>\n\n"
            f"Measurement budget: {self.max_configs} in total, "
            f"{self.remaining()} remaining.\n\n"
            "Find the configuration with the highest max sustainable QPS. "
            "Plan how to spend the budget, then begin." + dry
        )

    def measure_configuration(self, label, hypothesis, named_options=None,
                              host_commands=None, revert_commands=None,
                              reboot_after_apply=False, repeat=False,
                              knobs=None, server_options=None):
        if self.report is not None:
            raise AgentError("the search has already finished")
        if self.stop_reason:
            raise AgentError(f"no more measurements: {self.stop_reason}. Call "
                             "finish now with your report.")
        if self.remaining() == 0:
            raise AgentError("the measurement budget is used up; call finish")
        if named_options is not None and server_options is not None:
            raise AgentError("give named_options or server_options, not both")
        named_options = _string_list(self.options_field,
                                     server_options if named_options is None else named_options)
        host_commands = _string_list("host_commands", host_commands)
        revert_commands = _string_list("revert_commands", revert_commands)
        tags = {}
        if self.catalog is not None:
            if knobs is None:
                raise AgentError("knobs is required: the catalog knobs this change "
                                 "sets, or {} for the baseline")
            tags, errors = self.catalog.validate(knobs)
            unknown = [n for n in (knobs if isinstance(knobs, dict) else {})
                       if n not in self.catalog.knobs]
            if errors:
                raise AgentError("knobs rejected:\n- " + "\n- ".join(errors)
                                 + ("\nFor a technique the catalog lacks, call "
                                    "propose_knob and leave it out of knobs."
                                    if unknown else ""))
            if tags and not (named_options or host_commands):
                try:
                    rendered_change = self.catalog.render(tags, self.adapter.name,
                                                          self.facts)
                except KnobError as e:
                    raise AgentError(f"knobs cannot be rendered: {e}")
                named_options = rendered_change["named_options"]
                host_commands = rendered_change["host_commands"]
                reboot_after_apply = (reboot_after_apply
                                      or rendered_change["reboot_after_apply"])
        try:
            rendered = self.adapter.render(self.evaluator.base_text, named_options)
        except ServerConfigError as e:
            raise AgentError(f"{self.options_field} rejected: {e}")
        overrides = encode_change(label, named_options, host_commands,
                                  revert_commands, reboot_after_apply, tags)
        change = decode_change(overrides)

        base_id = change_id(change)
        pid = base_id
        previous = [r for r in self.evaluator.rows.values()
                    if r["point_id"].split("__rep")[0].endswith(base_id[-10:])]
        if previous:
            if not repeat:
                raise AgentError(
                    "this change was already measured: "
                    + json.dumps(self._row_view(previous[0]))
                    + "\nChange it, or pass repeat=true to measure it again.")
            base_id = previous[0]["point_id"].split("__rep")[0]
            n = 2
            while f"{base_id}__rep{n}" in self.evaluator.rows:
                n += 1
            pid = f"{base_id}__rep{n}"

        log.info("Agent hypothesis: %s", hypothesis)
        if self.dry_run:
            self.dry_run_done = True
            log.info("[dry run] %s", pid)
            for line in host_commands:
                log.info("[dry run] host (root): %s", line)
            return {
                "dry_run": True,
                "point_id": pid,
                self.adapter.config_path: rendered,
                "host_commands": host_commands,
                "revert_commands": revert_commands,
                "reboot_after_apply": bool(reboot_after_apply),
                "note": "Nothing was applied or measured. Call finish now.",
            }

        label_text = f"[agent {self.used() + 1}/{self.max_configs}]"
        extra = {"iteration": self.used() + 1, "hypothesis": hypothesis}
        if pid != base_id:
            extra["repeat_of"] = base_id
        row = self.evaluator.evaluate(overrides, label_text, extra, pid=pid)
        return {**self._row_view(row),
                "levels": self._levels(row),
                **self._budget()}

    def get_run_details(self, point_id):
        details = super().get_run_details(point_id)
        details.update(self._row_view(self.evaluator.rows[point_id], full=True))
        return details

    def propose_knob(self, name, description, scope, host_commands=None,
                     server_options=None, evidence=""):
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name or ""):
            raise AgentError("name must be snake_case")
        if self.catalog and name in self.catalog.knobs:
            raise AgentError(f"{name} is already in the catalog; use it in knobs")
        proposal = {"name": name, "description": description, "scope": scope,
                    "software": self.adapter.name,
                    "host_commands": _string_list("host_commands", host_commands),
                    "server_options": _string_list("server_options", server_options),
                    "evidence": evidence, "env": self.context.get("env"),
                    "proposed": now_iso()}
        if self.proposals_path:
            os.makedirs(os.path.dirname(os.path.abspath(self.proposals_path)), exist_ok=True)
            with open(self.proposals_path, "a") as f:
                f.write(json.dumps(proposal) + "\n")
        self.transcript.record("knob_proposal", **proposal)
        return (f"Recorded proposal {name!r} for review. Until it is in the "
                "catalog, leave it out of knobs and describe it in hypothesis.")

    def inspect_server(self, command=None, probe=None):
        if (command is None) == (probe is None):
            raise AgentError("give exactly one of command or probe")
        if probe is not None:
            return super().inspect_server(probe)
        try:
            result = ssh_run(self.server,
                             f"timeout {INSPECT_TIMEOUT} bash -c {shlex.quote(command)}",
                             timeout=INSPECT_TIMEOUT + 30)
        except subprocess.TimeoutExpired:
            raise AgentError(f"command timed out after {INSPECT_TIMEOUT}s")
        except OSError as e:
            raise AgentError(f"command could not run: {e}")
        output = (result.stdout or "") + (
            f"\n[stderr]\n{result.stderr}" if (result.stderr or "").strip() else "")
        return f"[on {self.server}, rc={result.returncode}]\n{output}"

    def finish(self, recommended_point_id, summary):
        if self.report is not None:
            return "already finished"
        if recommended_point_id and recommended_point_id not in self.evaluator.rows:
            raise AgentError(f"no measurement with point_id "
                             f"{recommended_point_id!r}; see get_results")
        row = self.evaluator.rows.get(recommended_point_id)
        change = decode_change(row) if row else None
        self.report = {
            "recommended_point_id": recommended_point_id or None,
            "recommended_change": change,
            "recommended_apply_script": (apply_script(change, self.adapter.config_path)
                                         if change else None),
            "recommended_max_qps_passed": row_qps(row) if row else None,
            "summary": summary,
            **self._budget(),
        }
        log.info("Agent finished. Recommended: %s", recommended_point_id or "(none)")
        return "Search finished. End your turn now."
