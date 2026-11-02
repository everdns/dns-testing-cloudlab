"""Shared pieces of the configuration searches (grid_search.py, agent_search.py).

A search proposes points (dicts of parameter values) and hands each one to an
Evaluator, which installs it on the name server (named.conf.options overrides
through bind_config.py, host tuning through system_config.py), runs the
max-sustainable-QPS search, restores the base, and records the row. The helpers
here load the search config, name points, read previous results for --resume,
and score rows.

Optionally an Evaluator also runs a validity.Validator around each
measurement (correct answers, untouched zone and query input) and appends
every row to a ledger.Ledger, tagged with knobs.yaml names. A row that fails
validation keeps its numbers but scores as nothing (row_qps returns None).
"""
import csv
import hashlib
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError

import yaml

from bind_config import (
    BindConfigError,
    install_options,
    render_options,
    restore_base,
)
from knobs import KnobError
from max_sustainable_qps import ResultStore, run_max_sustainable_qps
from servers.base import ServerConfigError
import system_config
from system_config import SystemConfigError

log = logging.getLogger("search")

# Results of every search go under <output-dir>/grid_search/.
SCRIPT_NAME = "grid_search"
OPTIMIZATION_DIR = os.path.dirname(os.path.abspath(__file__))

EXIT_RESTORE_FAILED = 3

# A configuration that cannot be applied raises one of these.
CONFIG_ERRORS = (BindConfigError, SystemConfigError, ServerConfigError, KnobError)

# Keep run directory names well under the 255-byte filename limit.
MAX_RUN_DIR_NAME = 100


def load_grid(path):
    """Load a search config YAML. The same dict doubles as the parameters and
    the base config passed to run_max_sustainable_qps for every point."""
    with open(path) as f:
        config = yaml.safe_load(f)

    params = config.get("parameters")
    if not params:
        raise ValueError(f"{path}: no 'parameters' list configured")
    for p in params:
        if "name" not in p:
            raise ValueError(f"{path}: a parameter entry is missing 'name'")
        if not p.get("values"):
            raise ValueError(f"{path}: parameter '{p['name']}' has no values")
    return config


def point_id(overrides):
    parts = [f"{name}={value}" for name, value in overrides.items()]
    return re.sub(r"[^A-Za-z0-9=_.-]", "_", "__".join(parts))


def run_dir_name(pid):
    """Shorten ``pid`` for use as a directory name, keeping it unique via a hash."""
    if len(pid) <= MAX_RUN_DIR_NAME:
        return pid
    digest = hashlib.sha1(pid.encode()).hexdigest()[:12]
    return f"{pid[:MAX_RUN_DIR_NAME - 13]}_{digest}"


def split_overrides(overrides):
    """Split a point into (named.conf.options overrides, host-level overrides)."""
    bind, system = {}, {}
    for name, value in overrides.items():
        (system if system_config.is_system_parameter(name) else bind)[name] = value
    return bind, system


def restore_or_exit(server, base_text, system_baseline=None, adapter=None):
    """Put the base config (and host baseline, if any) back, or stop the search.

    A failed restore leaves the name server holding an unknown config, so every
    later point would be measuring something other than what it proposed. There
    is nothing useful to do but stop and say so. ``adapter`` (servers/) picks
    the server software whose config is restored; the default is BIND.
    """
    try:
        if adapter is None:
            restore_base(server, base_text)
        else:
            adapter.restore_base(server, base_text)
    except (BindConfigError, ServerConfigError) as e:
        name = adapter.name if adapter else "bind"
        log.error("Failed to restore the base config on %s: %s", server, e)
        log.error("The name server is left in a modified state. Reinstall it "
                  "with ns_software/%s/install.sh before running again.", name)
        raise SystemExit(EXIT_RESTORE_FAILED)
    if system_baseline is None:
        return
    try:
        system_config.restore(server, system_baseline)
    except SystemConfigError as e:
        log.error("Failed to restore the system baseline on %s: %s", server, e)
        log.error("Host tuning (sysctls, ufw, iptables raw rules, named "
                  "LimitNOFILE drop-in) is left modified. Reboot the name "
                  "server before running again.")
        raise SystemExit(EXIT_RESTORE_FAILED)


def run_evaluation(config, output_dir, timeout=None, hooks=None):
    """Run the max-sustainable-QPS search in-process for the installed config.

    Returns (status, summary, trial_rows). status is "ok", "failed", or
    "timeout". Runs in a worker thread so a hung point does not block forever;
    on timeout the thread is abandoned rather than killed, and a fresh shallow
    copy of ``config`` is passed to each call so an abandoned, still-running
    thread from a prior timeout can't race the next point's mutation of shared
    config keys (e.g. "runtime").
    """
    executor = ThreadPoolExecutor(max_workers=1)
    # config["simulate_max_qps"] runs the search without touching any host
    # (campaign.py --simulate).
    future = executor.submit(run_max_sustainable_qps, dict(config), output_dir,
                             simulate_max_qps=config.get("simulate_max_qps"),
                             hooks=hooks)
    try:
        summary, trial_rows = future.result(timeout=timeout)
    except FutureTimeoutError:
        log.error("Evaluation timed out after %ss", timeout)
        executor.shutdown(wait=False)
        return "timeout", None, None
    except (ValueError, RuntimeError, TimeoutError) as e:
        log.error("max_sustainable_qps failed: %s", e)
        executor.shutdown(wait=False)
        return "failed", None, None
    executor.shutdown(wait=False)
    return "ok", summary, trial_rows


def min_fidelity(trial_rows):
    """Lowest qps_fidelity_pct across the run's trials, or None.

    A run whose fidelity dipped below the threshold was limited by the load
    generator, not by the server, so its max QPS is not a server measurement.
    """
    values = [row["qps_fidelity_pct"] for row in (trial_rows or [])
              if isinstance(row.get("qps_fidelity_pct"), (int, float))]
    return min(values) if values else None


def previous_rows(output_dir, filename):
    """Rows recorded by a previous run, for --resume."""
    path = os.path.join(output_dir, SCRIPT_NAME, filename)
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [row for row in csv.DictReader(f) if row.get("point_id")]


def row_qps(row):
    """max_qps_passed for a successful, valid row, else None."""
    if row.get("status") != "ok":
        return None
    if str(row.get("validity_ok", "")).lower() == "false":
        return None
    try:
        return int(float(row["max_qps_passed"]))
    except (KeyError, TypeError, ValueError):
        return None


class Evaluator:
    """Installs a point on the name server, measures it, restores the base, and
    records the row. Each point id is measured at most once per run, and rows
    from a resumed run count as already measured.

    ``validator`` (validity.Validator) checks each measurement; ``ledger``
    (ledger.Ledger) receives every row; ``catalog`` (knobs.Catalog) tags rows
    with knob names. All three are optional."""

    def __init__(self, config, server, base_text, system_baseline,
                 output_dir, results_name, timeout, resumed_rows=(),
                 validator=None, ledger=None, catalog=None):
        self.config = config
        self.server = server
        self.base_text = base_text
        self.system_baseline = system_baseline
        self.output_dir = output_dir
        self.results_name = results_name
        self.timeout = timeout
        self.validator = validator
        self.ledger = ledger
        self.catalog = catalog
        self.store = ResultStore(output_dir)
        self.rows = {}
        for row in resumed_rows:
            self.store.add_result(row)
            self.rows[row["point_id"]] = row

    def export(self):
        path = self.store.export_csv(SCRIPT_NAME, f"{self.results_name}.csv")
        self.store.export_json(SCRIPT_NAME, f"{self.results_name}.json")
        return path

    def _apply(self, overrides, run_dir):
        """Install ``overrides`` on the name server. Raises BindConfigError or
        SystemConfigError if it cannot be applied."""
        bind_overrides, system_overrides = split_overrides(overrides)
        rendered = render_options(self.base_text, bind_overrides)
        install_options(self.server, rendered)
        system_config.apply(self.server, system_overrides)

    def _restore(self, overrides, run_dir):
        """Put the base configuration back. Returns extra row fields, if any."""
        restore_or_exit(self.server, self.base_text, self.system_baseline)

    def knobs_for(self, overrides):
        """The point as knobs.yaml names, for the ledger."""
        return self.catalog.from_parameters(overrides) if self.catalog else {}

    def change_for(self, overrides):
        """The point as applied, for the ledger."""
        return dict(overrides)

    def ledger_extra(self, overrides):
        """More ledger fields for this point (subclasses: build ids)."""
        return {}

    def _record(self, row, overrides, validity=None):
        """Keep ``row``: results store, exports, and the ledger."""
        self.store.add_result(row)
        self.export()
        if self.ledger is not None:
            try:
                self.ledger.record(row, knobs=self.knobs_for(overrides),
                                   change=self.change_for(overrides),
                                   validity=validity,
                                   service=self.config.get("dns_service"),
                                   **self.ledger_extra(overrides))
            except (OSError, ValueError, TypeError) as e:
                log.error("Could not append %s to the ledger: %s", row["point_id"], e)

    def evaluate(self, overrides, label, extra=None, pid=None):
        """Return the row for ``overrides``, measuring it only if needed.

        ``pid`` defaults to point_id(overrides); pass a distinct id to measure
        the same configuration again (e.g. to gauge noise)."""
        pid = pid or point_id(overrides)
        if pid in self.rows:
            log.info("%s Reusing %s (already recorded)", label, pid)
            return self.rows[pid]

        log.info("%s Proposing %s", label, pid)
        row = {
            "point_id": pid,
            "index": len(self.store.results) + 1,
            **(extra or {}),
            **overrides,
            "status": "",
            "error": "",
            "max_qps_passed": "",
            "max_qps_tested": "",
            "hit_max_qps_ceiling": "",
            "total_trials_run": "",
            "search_duration_s": "",
            "min_qps_fidelity_pct": "",
            "run_dir": "",
        }
        self.rows[pid] = row

        run_dir = os.path.join(self.output_dir, SCRIPT_NAME, "runs", run_dir_name(pid))
        try:
            self._apply(overrides, run_dir)
        except CONFIG_ERRORS as e:
            log.error("Configuration update failed: %s", e)
            row["status"] = "config_failed"
            row["error"] = str(e)
            row.update(self._restore(overrides, run_dir) or {})
            self._record(row, overrides)
            return row

        row["run_dir"] = run_dir
        hooks = (self.validator.begin(self.config.get("dns_service"))
                 if self.validator is not None else None)
        try:
            status, summary, trial_rows = run_evaluation(
                self.config, run_dir, timeout=self.timeout, hooks=hooks,
            )
        finally:
            row.update(self._restore(overrides, run_dir) or {})

        validity = None
        if hooks is not None:
            validity = hooks.result()
            row["validity_ok"] = validity["ok"]
            row["validity_reasons"] = "; ".join(validity["reasons"])[:1000]
            if not validity["ok"]:
                log.warning("%s %s is INVALID: %s", label, pid, row["validity_reasons"])

        row["status"] = status
        if status == "ok":
            row["max_qps_passed"] = summary["max_qps_passed"]
            row["max_qps_tested"] = summary["max_qps_tested"]
            row["hit_max_qps_ceiling"] = summary["hit_max_qps_ceiling"]
            row["total_trials_run"] = summary["total_trials_run"]
            row["search_duration_s"] = summary["search_duration_s"]
            fidelity = min_fidelity(trial_rows)
            row["min_qps_fidelity_pct"] = "" if fidelity is None else fidelity
            log.info("%s %s -> %d QPS", label, pid, summary["max_qps_passed"])

        self._record(row, overrides, validity)
        return row
