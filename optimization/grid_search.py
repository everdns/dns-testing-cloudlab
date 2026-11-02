#!/usr/bin/env python3
"""Grid search over BIND options and host tuning, scored by max sustainable QPS.

For each point proposed:

  1. Propose a configuration (the next combination of parameter values).
  2. Attempt the configuration update on the name server.
  3a. On success, run the max-sustainable-QPS search and record the result.
  3b. On failure, record why and return to step 1.

--mode grid (default) proposes every combination. --mode coordinate-descent
proposes points by coordinate descent (maximizing QPS): starting from each
parameter's first value, it sweeps one parameter at a time with the others held
at the current best, over up to --max-passes passes.

grid_search.yaml is the only configuration file this needs: it holds both the
grid parameters and the base search config (hosts, tool, dns service, etc.)
passed to optimization.max_sustainable_qps.

    python3 grid_search.py --output-dir results
    python3 grid_search.py --mode coordinate-descent --max-passes 3
"""
import argparse
import itertools
import logging
import os
import sys
import time

from bind_config import install_options, load_base_options, render_options
import knobs
import ledger as ledger_mod
from max_sustainable_qps import get_clients
from servers import get_adapter
import validity
from search import (
    OPTIMIZATION_DIR,
    Evaluator,
    load_grid,
    point_id,
    previous_rows,
    restore_or_exit,
    row_qps,
    split_overrides,
)
import system_config
from system_config import SystemConfigError

log = logging.getLogger("grid_search")

DEFAULT_GRID = os.path.join(OPTIMIZATION_DIR, "grid_search.yaml")


def propose_configurations(parameters):
    """Yield every combination of parameter values as an ordered dict."""
    names = [p["name"] for p in parameters]
    for combo in itertools.product(*(p["values"] for p in parameters)):
        yield dict(zip(names, combo))


def run_grid(evaluator, parameters):
    """Measure every combination. Returns (point_id, qps) of the best, or None."""
    points = list(propose_configurations(parameters))
    best = None
    for index, overrides in enumerate(points, start=1):
        row = evaluator.evaluate(overrides, f"[{index}/{len(points)}]",
                                 {"index": index})
        qps = row_qps(row)
        if qps is not None and (best is None or qps > best[1]):
            best = (row["point_id"], qps)
    return best


def run_coordinate_descent(evaluator, parameters, max_passes, min_improvement_pct):
    """Coordinate descent (maximizing QPS): starting from each parameter's first value, sweep one
    parameter at a time with the rest held at the current best, and move to a
    new value only if it beats the current point by more than
    ``min_improvement_pct``. Repeat passes until one changes nothing or
    ``max_passes`` is reached. Returns (point_id, qps) of the final point, or
    None if no point produced a usable result."""
    current = {p["name"]: p["values"][0] for p in parameters}
    current_row = evaluator.evaluate(current, "[start]",
                                     {"pass": 0, "coordinate": ""})
    current_qps = row_qps(current_row)

    for pass_no in range(1, max_passes + 1):
        log.info("=== Coordinate pass %d/%d from %s (%s QPS) ===", pass_no,
                 max_passes, point_id(current), current_qps)
        changed = False
        for p in parameters:
            name = p["name"]
            best_value, best_qps = current[name], current_qps
            for value in p["values"]:
                if value == current[name]:
                    continue
                candidate = {**current, name: value}
                row = evaluator.evaluate(
                    candidate, f"[pass {pass_no} {name}]",
                    {"pass": pass_no, "coordinate": name},
                )
                qps = row_qps(row)
                if qps is None:
                    continue
                if current_qps is None:
                    beats = best_qps is None or qps > best_qps
                else:
                    threshold = current_qps * (1 + min_improvement_pct / 100)
                    beats = qps > threshold and qps > best_qps
                if beats:
                    best_value, best_qps = value, qps

            if best_value != current[name]:
                log.info("%s: %s -> %s (%s -> %s QPS)", name, current[name],
                         best_value, current_qps, best_qps)
                current[name] = best_value
                current_qps = best_qps
                changed = True
            else:
                log.info("%s: keeping %s", name, current[name])

        if not changed:
            log.info("Converged after pass %d: no parameter changed", pass_no)
            break
    else:
        log.warning("Stopped after --max-passes=%d without converging", max_passes)

    return None if current_qps is None else (point_id(current), current_qps)


def main():
    parser = argparse.ArgumentParser(
        description="Grid search or coordinate descent over BIND options and "
                    "host tuning, scored by max sustainable QPS"
    )
    parser.add_argument("--grid", default=DEFAULT_GRID,
                        help="Grid config YAML defining parameters and the search config")
    parser.add_argument("--mode", choices=["grid", "coordinate-descent"],
                        default="grid",
                        help="grid: measure every combination. "
                             "coordinate-descent: optimize one parameter at a "
                             "time, starting from each parameter's first value")
    parser.add_argument("--max-passes", type=int, default=3,
                        help="coordinate-descent mode: most passes over all "
                             "parameters")
    parser.add_argument("--min-improvement-pct", type=float, default=1.0,
                        help="coordinate-descent mode: a value must beat the "
                             "current point's QPS by more than this percent "
                             "to replace it")
    parser.add_argument("--dns-service",
                        help="DNS service to evaluate (overrides the grid config)")
    parser.add_argument("--server", help="Name server host (overrides the grid config)")
    parser.add_argument("--output-dir", default="results",
                        help="Output directory for results")
    parser.add_argument("--point-timeout", type=int, default=21600,
                        help="Seconds to allow one evaluation before moving on")
    parser.add_argument("--resume", action="store_true",
                        help="Keep and skip points already recorded by this mode")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print each proposed configuration and stop")
    parser.add_argument("--ledger", metavar="PATH",
                        help="Append every measurement to this ledger")
    parser.add_argument("--env", help="Environment name recorded in the ledger")
    parser.add_argument("--validate", action="store_true",
                        help="Check every measurement for correct answers and "
                             "an unchanged zone/query input (validity.py)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.max_passes < 1:
        parser.error("--max-passes must be >= 1")
    if args.min_improvement_pct < 0:
        parser.error("--min-improvement-pct must be >= 0")

    config = load_grid(args.grid)
    parameters = config["parameters"]
    if args.dns_service:
        config["dns_service"] = args.dns_service
    if args.server:
        config.setdefault("hosts", {})["server"] = args.server

    dns_service = config.get("dns_service", "ns_bind")
    search_output_dir = args.output_dir if os.path.isabs(args.output_dir) else os.path.join(OPTIMIZATION_DIR, args.output_dir)
    results_name = "grid_results" if args.mode == "grid" else "coordinate_descent_results"

    server = config.get("hosts", {}).get("server")
    if not server:
        log.error("No name server host configured")
        return 2

    base_text = load_base_options()
    uses_system = any(system_config.is_system_parameter(p["name"])
                      for p in parameters)

    if args.mode == "grid":
        points = list(propose_configurations(parameters))
        log.info("=== Grid search over %d parameter(s), %d point(s) ===",
                 len(parameters), len(points))
    else:
        points = [{p["name"]: p["values"][0] for p in parameters}]
        per_pass = sum(len(p["values"]) - 1 for p in parameters)
        log.info("=== Coordinate descent over %d parameter(s): at most %d "
                 "point(s) per pass, %d pass(es), min improvement %.2f%% ===",
                 len(parameters), per_pass, args.max_passes,
                 args.min_improvement_pct)
    for p in parameters:
        log.info("  %s: %s", p["name"], p["values"])
    log.info("Name server: %s, service: %s", server, dns_service)

    if args.dry_run:
        if args.mode == "coordinate-descent":
            log.info("Coordinate descent depends on measurements; showing only "
                     "the starting point")
        for overrides in points:
            print(f"--- {point_id(overrides)} ---")
            bind_overrides, system_overrides = split_overrides(overrides)
            install_options(server, render_options(base_text, bind_overrides),
                            dry_run=True)
            system_config.apply(server, system_overrides, dry_run=True)
        return 0

    resumed = previous_rows(search_output_dir, f"{results_name}.csv") if args.resume else []
    if resumed:
        log.info("Resuming: %d point(s) already recorded", len(resumed))

    system_baseline = None
    if uses_system:
        try:
            system_baseline = system_config.snapshot(server)
        except SystemConfigError as e:
            log.error("Could not snapshot host settings on %s: %s", server, e)
            return 2

    ledger = None
    if args.ledger:
        ledger = ledger_mod.Ledger(
            args.ledger, env=args.env, software="bind",
            hardware=ledger_mod.fingerprint(server, get_clients(config)[0]),
            phase=args.mode.replace("-", "_"),
            experiment_id=os.path.basename(search_output_dir),
            min_qps_fidelity_pct=config["max_sustainable_qps"].get("min_qps_fidelity_pct"))
    validator = None
    if args.validate:
        validator = validity.Validator(config, get_adapter(dns_service))
        try:
            validator.ensure_reference()
        except (validity.ValidityError, RuntimeError, TimeoutError) as e:
            log.error("Could not capture the reference answers: %s", e)
            return 2

    evaluator = Evaluator(config, server, base_text, system_baseline,
                          search_output_dir, results_name, args.point_timeout,
                          resumed, validator=validator, ledger=ledger,
                          catalog=knobs.load())
    started = time.time()

    try:
        if args.mode == "grid":
            best = run_grid(evaluator, parameters)
        else:
            best = run_coordinate_descent(evaluator, parameters, args.max_passes,
                                  args.min_improvement_pct)
    finally:
        path = evaluator.export()
        if path:
            log.info("Results written to %s", path)
        restore_or_exit(server, base_text, system_baseline)

    log.info("=== %s finished in %.1fs ===",
             "Grid search" if args.mode == "grid" else "Coordinate descent",
             time.time() - started)
    if best:
        log.info("Best: %s at %d QPS", best[0], best[1])
    else:
        log.warning("No point produced a usable result")
    return 0


if __name__ == "__main__":
    sys.exit(main())
