#!/usr/bin/env python3
"""Agent-driven search over BIND options and host tuning, scored by max
sustainable QPS.

A Claude agent runs the search. This script sets up the name server
connection, gives the agent its tools (agent_tools.py), and gets out of the
way. The agent decides:

  - which configurations to measure, and in what order
  - when to measure a configuration again to check run-to-run noise
  - when to look at the name server (read-only diagnostics such as UDP drop
    counters, softnet_stat, NIC statistics and sysctls)
  - when to stop, and what to recommend in its final report

The tools enforce the rules. Values must be inside the ``agent:`` bounds in
agent_search.yaml, and at most --max-configs measurements are made (resumed,
failed and repeated ones included). Each measurement is installed, measured
and restored exactly as grid_search.py does it (search.Evaluator). When the
run ends or is interrupted, the base configuration is restored.

With --mode open (or agent.mode: open) there are no parameters or bounds.
Each measurement is any named.conf.options statements plus any root shell
commands on the name server, optionally followed by a reboot, and
inspect_server runs any command. Only BIND's protected statements are refused.
host_state.py snapshots the server before the search and restores it after
every measurement (rebooting if the kernel command line changed). The agent
sees whatever it could not restore as residual drift.

Claude is reached one of two ways (--agent-backend, or agent.backend):

  api          The Anthropic API, billed as API usage (Console credits):
                   pip install anthropic
                   export ANTHROPIC_API_KEY=...
                   export ANTHROPIC_WORKSPACE_ID=...   # only for keys not scoped to a workspace
  claude-code  One Claude Code session (claude -p), which uses a Claude Pro/Max
               subscription's usage limits. Install Claude Code and log in
               once by running `claude`.

    python3 agent_search.py --max-configs 15
    python3 agent_search.py --dry-run          # show the agent's first choice, measure nothing
"""
import argparse
import json
import logging
import os
import sys
import time

from agent_runner import BACKENDS, DEFAULT_MODEL, make_runner
from agent_tools import (AgentError, OpenEvaluator, OpenSearchTools, SearchTools,
                         Transcript, build_search_space)
from bind_config import load_base_options
import host_state
import knobs
import ledger as ledger_mod
from max_sustainable_qps import get_clients
from servers import get_adapter
import validity
from search import (
    OPTIMIZATION_DIR,
    SCRIPT_NAME,
    Evaluator,
    load_grid,
    previous_rows,
    restore_or_exit,
)
import system_config
from system_config import SystemConfigError

log = logging.getLogger("agent_search")

DEFAULT_CONFIG = os.path.join(OPTIMIZATION_DIR, "agent_search.yaml")
RESULTS_NAME = "agent_results"
OPEN_RESULTS_NAME = "agent_open_results"
MODES = ["bounded", "open"]
REPORT_NAME = "agent_report.json"
DEFAULT_MAX_CONFIGS = 20
EFFORTS = ["low", "medium", "high", "xhigh", "max"]

# Settings from the base config that tell the agent how points are scored.
CONTEXT_KEYS = ["dns_service", "tool", "threads", "ports_per_thread",
                "max_sustainable_qps"]
# Open mode also tells the agent which hosts and interface it must not break.
OPEN_CONTEXT_KEYS = CONTEXT_KEYS + ["hosts", "client_interface"]


def default_max_turns(max_configs, mode="bounded"):
    """Room for a few tool calls (results, details, diagnostics) per
    measurement, plus planning and the report. Open mode inspects more."""
    return (12 if mode == "open" else 8) * max_configs + 20


def known_knobs(parameters):
    """The config's parameters, shown to an open-mode agent as ideas."""
    return [{"name": p["name"],
             "description": (p.get("agent") or {}).get("description", ""),
             "values_tried": p.get("values", [])} for p in parameters]


def make_open_search(config, server, adapter, results_dir, output_dir, max_configs,
                     timeout, transcript, dry_run=False, resumed=(),
                     results_name=OPEN_RESULTS_NAME, reboot_timeout=None,
                     catalog=None, facts=None, ledger=None, validator=None,
                     extra_context="", proposals_path=None):
    """Snapshot the host and build the open-mode (evaluator, tools) pair.
    Used by main() and by the campaign's explore phase. Returns None if the
    host cannot be snapshotted."""
    host_baseline = None
    if not dry_run:
        try:
            host_baseline = host_state.snapshot(server, save_files=True)
        except host_state.HostStateError as e:
            log.error("Could not snapshot the host state on %s: %s", server, e)
            return None
        os.makedirs(results_dir, exist_ok=True)
        snapshot_path = os.path.join(results_dir, "host_baseline.json")
        with open(snapshot_path, "w") as f:
            json.dump(host_baseline, f, indent=1)
        log.info("Host baseline written to %s", snapshot_path)
    evaluator = OpenEvaluator(
        config, server, adapter.load_base(), host_baseline, output_dir,
        results_name, timeout, resumed,
        reboot_timeout=reboot_timeout or host_state.DEFAULT_REBOOT_TIMEOUT,
        adapter=adapter, validator=validator, ledger=ledger, catalog=catalog)
    tools = OpenSearchTools(
        known_knobs(config.get("parameters") or []), evaluator,
        context={key: config[key] for key in OPEN_CONTEXT_KEYS if key in config},
        max_configs=max_configs, transcript=transcript, server=server,
        dry_run=dry_run, catalog=catalog, facts=facts,
        extra_context=extra_context, proposals_path=proposals_path)
    return evaluator, tools


def write_report(tools, results_dir):
    """Write the agent's report (and the recommended change as a script)."""
    if not tools.report:
        return None
    report_path = os.path.join(results_dir, REPORT_NAME)
    os.makedirs(results_dir, exist_ok=True)
    with open(report_path, "w") as f:
        json.dump(tools.report, f, indent=2, default=str)
    script = tools.report.get("recommended_apply_script")
    if script:
        script_path = os.path.join(results_dir, "recommended_apply.sh")
        with open(script_path, "w") as f:
            f.write(script)
        log.info("Commands for the recommended change: %s", script_path)
    log.info("Agent recommends %s (%s QPS)", tools.report["recommended_point_id"],
             tools.report["recommended_max_qps_passed"])
    log.info("Report written to %s", report_path)
    return report_path


def main():
    parser = argparse.ArgumentParser(
        description="Agent-driven search over BIND options and host tuning, "
                    "scored by max sustainable QPS"
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG,
                        help="YAML defining the parameters, agent settings and "
                             "the search config")
    parser.add_argument("--mode", choices=MODES,
                        help="bounded: the agent picks values for the config's "
                             "parameters, within their agent: bounds. open: the "
                             "agent may apply any named.conf.options statements "
                             "and any root commands on the name server (default: "
                             "agent.mode, else bounded)")
    parser.add_argument("--max-configs", type=int,
                        help="Most measurements to make, failed, repeated and "
                             "resumed ones included (default: agent.max_configs "
                             f"in the config, else {DEFAULT_MAX_CONFIGS})")
    parser.add_argument("--agent-backend", choices=sorted(BACKENDS),
                        help="How to reach Claude: api (Anthropic API credits) "
                             "or claude-code (claude -p, subscription limits). "
                             "Default: agent.backend, else api")
    parser.add_argument("--claude-bin", default="claude",
                        help="claude-code backend: path to the claude executable")
    parser.add_argument("--agent-model",
                        help=f"Claude model (default: agent.model, else {DEFAULT_MODEL} "
                             "for api and Claude Code's own default for claude-code)")
    parser.add_argument("--agent-effort", choices=EFFORTS,
                        help="Claude effort level (default: agent.effort, else high)")
    parser.add_argument("--agent-max-turns", type=int,
                        help="Most agent turns before the run is stopped "
                             "(default: agent.max_turns, else 8 per measurement + 20)")
    parser.add_argument("--max-session-usage", type=float, metavar="PCT",
                        help="claude-code backend: stop measuring once your "
                             "subscription's 5-hour session usage reaches PCT%% "
                             "(default: agent.max_session_usage, else no limit)")
    parser.add_argument("--max-weekly-usage", type=float, metavar="PCT",
                        help="claude-code backend: stop measuring once your "
                             "subscription's weekly usage reaches PCT%% "
                             "(default: agent.max_weekly_usage, else no limit)")
    parser.add_argument("--dns-service",
                        help="DNS service to evaluate (overrides the config), "
                             "e.g. ns_knot or ns_nsd")
    parser.add_argument("--ledger", metavar="PATH",
                        help="Append every measurement to this ledger "
                             f"(e.g. {ledger_mod.DEFAULT_PATH})")
    parser.add_argument("--env", help="Environment name recorded in the ledger "
                                      "(e.g. the CloudLab hardware type)")
    parser.add_argument("--validate", action="store_true",
                        help="Check every measurement for correct answers and "
                             "an unchanged zone/query input (validity.py)")
    parser.add_argument("--server", help="Name server host (overrides the config)")
    parser.add_argument("--output-dir", default="results",
                        help="Output directory for results")
    parser.add_argument("--point-timeout", type=int, default=21600,
                        help="Seconds to allow one evaluation before moving on")
    parser.add_argument("--resume", action="store_true",
                        help="Give the agent the measurements recorded by a "
                             "previous agent search; they count toward "
                             "--max-configs")
    parser.add_argument("--dry-run", action="store_true",
                        help="Let the agent choose its first configuration, "
                             "show what would be applied, and stop without "
                             "changing the server")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = load_grid(args.config)
    agent_settings = config.get("agent") or {}
    max_configs = (args.max_configs if args.max_configs is not None
                   else agent_settings.get("max_configs", DEFAULT_MAX_CONFIGS))
    if max_configs < 1:
        parser.error("--max-configs must be >= 1")
    mode = args.mode or agent_settings.get("mode", "bounded")
    if mode not in MODES:
        parser.error(f"agent.mode must be one of {MODES}")
    reboot_timeout = agent_settings.get("reboot_timeout",
                                        host_state.DEFAULT_REBOOT_TIMEOUT)
    max_turns = (args.agent_max_turns or agent_settings.get("max_turns")
                 or default_max_turns(max_configs, mode))
    if max_turns < 1:
        parser.error("--agent-max-turns must be >= 1")
    backend_name = args.agent_backend or agent_settings.get("backend", "api")
    if backend_name not in BACKENDS:
        parser.error(f"agent.backend must be one of {sorted(BACKENDS)}")
    model = args.agent_model or agent_settings.get("model")
    effort = args.agent_effort or agent_settings.get("effort", "high")
    usage_limits = {}
    for window, arg, key in [("five_hour", args.max_session_usage, "max_session_usage"),
                             ("seven_day", args.max_weekly_usage, "max_weekly_usage")]:
        pct = arg if arg is not None else agent_settings.get(key)
        if pct is None:
            continue
        if not 0 < pct <= 100:
            parser.error(f"{key} must be a percentage in (0, 100]")
        usage_limits[window] = pct / 100
    if usage_limits and backend_name != "claude-code":
        log.warning("Usage limits apply only to the claude-code backend; ignoring them")

    parameters = config["parameters"]
    if args.dns_service:
        config["dns_service"] = args.dns_service
    if args.server:
        config.setdefault("hosts", {})["server"] = args.server

    dns_service = config.get("dns_service", "ns_bind")
    search_output_dir = args.output_dir if os.path.isabs(args.output_dir) else os.path.join(OPTIMIZATION_DIR, args.output_dir)
    results_dir = os.path.join(search_output_dir, SCRIPT_NAME)

    server = config.get("hosts", {}).get("server")
    if not server:
        log.error("No name server host configured")
        return 2

    space = []
    if mode == "bounded":
        try:
            space = build_search_space(parameters)
        except ValueError as e:
            log.error("%s: %s", args.config, e)
            return 2

    log.info("=== Agent search (%s mode) %s, at most %d measurement(s), "
             "%s backend, model %s (effort %s)%s ===", mode,
             f"over {len(parameters)} parameter(s)" if mode == "bounded"
             else "over anything on the name server", max_configs,
             backend_name, model or "default", effort,
             " [DRY RUN]" if args.dry_run else "")
    for spec in space:
        bounds = (spec["choices"] if spec["type"] == "enum"
                  else f"{spec['type']} [{spec['min']}, {spec['max']}]"
                       + (" or default" if spec["allow_default"] else ""))
        log.info("  %s: %s", spec["name"], bounds)
    log.info("Name server: %s, service: %s", server, dns_service)
    adapter = get_adapter(dns_service)
    if mode == "bounded" and adapter.name != "bind":
        parser.error("bounded mode tunes BIND parameters; use --mode open for "
                     f"{adapter.name}")

    try:
        runner = make_runner(backend_name, model=model, effort=effort,
                             max_turns=max_turns, claude_bin=args.claude_bin,
                             tool_timeout_s=args.point_timeout,
                             usage_limits=usage_limits)
    except AgentError as e:
        log.error("%s", e)
        return 2

    results_name = OPEN_RESULTS_NAME if mode == "open" else RESULTS_NAME
    resumed = (previous_rows(search_output_dir, f"{results_name}.csv")
               if args.resume else [])
    if resumed:
        log.info("Resuming: %d measurement(s) already recorded", len(resumed))

    base_text = load_base_options()
    transcript = Transcript(os.path.join(results_dir, "agent_transcript.jsonl"))
    catalog = knobs.load()
    client = get_clients(config)[0]
    facts = ledger_mod.fingerprint(server, client) if not args.dry_run else {}
    ledger = None
    if args.ledger and not args.dry_run:
        ledger = ledger_mod.Ledger(
            args.ledger, env=args.env, hardware=facts, software=adapter.name,
            phase=f"agent_{mode}", experiment_id=os.path.basename(search_output_dir),
            min_qps_fidelity_pct=config["max_sustainable_qps"].get("min_qps_fidelity_pct"))
    validator = None
    if args.validate and not args.dry_run:
        validator = validity.Validator(config, adapter)
        try:
            validator.ensure_reference()
        except (validity.ValidityError, RuntimeError, TimeoutError) as e:
            log.error("Could not capture the reference answers: %s", e)
            return 2
    system_baseline = None
    if mode == "open":
        built = make_open_search(
            config, server, adapter, results_dir, search_output_dir, max_configs,
            args.point_timeout, transcript, dry_run=args.dry_run, resumed=resumed,
            results_name=results_name, reboot_timeout=reboot_timeout,
            catalog=catalog, facts=facts, ledger=ledger, validator=validator,
            proposals_path=os.path.join(results_dir, "knobs_proposed.jsonl"))
        if built is None:
            return 2
        evaluator, tools = built
    else:
        if not args.dry_run and any(system_config.is_system_parameter(p["name"])
                                    for p in parameters):
            try:
                system_baseline = system_config.snapshot(server)
            except SystemConfigError as e:
                log.error("Could not snapshot host settings on %s: %s", server, e)
                return 2
        evaluator = Evaluator(config, server, base_text, system_baseline,
                              search_output_dir, results_name, args.point_timeout,
                              resumed, validator=validator, ledger=ledger,
                              catalog=catalog)
        tools = SearchTools(
            space, evaluator,
            context={key: config[key] for key in CONTEXT_KEYS if key in config},
            max_configs=max_configs, transcript=transcript, server=server,
            dry_run=args.dry_run,
        )
    started = time.time()

    status = 0
    try:
        runner.run(tools, transcript)
    except AgentError as e:
        log.error("Agent stopped: %s", e)
        status = 1
    finally:
        if not args.dry_run:
            path = evaluator.export()
            if path:
                log.info("Results written to %s", path)
            if mode == "open":
                evaluator.restore_host()
            else:
                restore_or_exit(server, base_text, system_baseline)

    if args.dry_run:
        return status

    log.info("=== Agent search finished in %.1fs: %d measurement(s) ===",
             time.time() - started, len(evaluator.rows))
    best = tools.best()
    if best:
        log.info("Best measured: %s at %d QPS", best[0], best[1])
    else:
        log.warning("No measurement produced a usable result")

    if write_report(tools, results_dir):
        print("\n=== Agent report ===\n" + tools.report["summary"])
    return status


if __name__ == "__main__":
    sys.exit(main())
