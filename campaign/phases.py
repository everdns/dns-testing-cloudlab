"""The campaign's steps. Each takes the Campaign (ctx) and an Experiment and
returns an outcome dict ({"status": ..., ...}) saved as experiments/<id>/
experiment.json. Every measurement they make goes to the ledger through
search.Evaluator, with the step as its phase and a replicate_group label.

Replicates are interleaved in a shuffled order (seeded by the experiment id),
so slow drift of the testbed spreads over all configurations instead of
biasing the ones measured last.
"""
import json
import logging
import os
import random

import stats
from agent_tools import Transcript
from max_sustainable_qps import (ResultStore, run_trial, start_dns_service,
                                 stop_dns_service, validate_search_config)
from servers import get_adapter

log = logging.getLogger("campaign.phases")


def _qps(row):
    from search import row_qps
    return row_qps(row)


def _write(exp, name, data):
    with open(os.path.join(exp.dir, name), "w") as f:
        json.dump(data, f, indent=2, default=str)


def _measure_plan(ctx, evaluator, plan, software, seed):
    """Measure every (knobs, group, rep, expect) of ``plan`` in shuffled
    order. Returns {group: [qps or None]}."""
    order = list(plan)
    random.Random(seed).shuffle(order)
    scores = {}
    for i, (knobs, group, rep, expect) in enumerate(order, 1):
        if os.path.exists(ctx.flag("STOP")):
            log.info("STOP: leaving the plan after %d of %d measurements", i - 1, len(order))
            break
        log.info("Plan %d/%d: %s r%d", i, len(order), group, rep)
        row = ctx.measure_knobs(evaluator, knobs, group, rep, software, expect=expect)
        scores.setdefault(group, []).append(_qps(row))
    return scores


# ---------------------------------------------------------------------------
# calibrate
# ---------------------------------------------------------------------------

def probe_client_ceiling(ctx, software):
    """Queries per second the clients can send together (one 10 s trial at
    calibrate.client_probe_qps, service stopped; replies do not matter)."""
    config = ctx.measurement_config(software)
    config["max_sustainable_qps"] = {**config["max_sustainable_qps"], "trial_duration": 10}
    params = validate_search_config(config)
    target = int(ctx.cfg.get("calibrate", {}).get("client_probe_qps", 3000000))
    row = run_trial(config, params, target, 0, ResultStore(os.path.join(ctx.state_dir, "tmp")))
    sent = row.get("queries_sent") or 0
    return int(sent / 10) if sent else None


def calibrate(ctx, exp):
    software = exp.software
    if not ctx.ensure_software(software):
        return {"status": "skipped", "skipped": f"{software} unavailable"}
    cal = ctx.cfg.get("calibrate", {})
    if not ctx.simulate:
        ceiling = probe_client_ceiling(ctx, software)
        if ceiling:
            ctx.state["client_ceiling"] = ceiling
            if cal.get("cap_max_qps", True):
                step = int(ctx.search_config["max_sustainable_qps"]["min_qps_step"])
                ctx.state["max_qps_cap"] = (int(ceiling * 1.05) // step + 1) * step
            log.info("Clients can send about %d QPS together", ceiling)
    ctx.fingerprint()
    evaluator = ctx.open_evaluator(exp, "calibrate_results")
    base, _ = ctx.baseline(software)
    try:
        values = []
        for rep in range(1, cal.get("replicates", 3) + 1):
            row = ctx.measure_knobs(evaluator, {}, "baseline", rep, software, expect=base)
            values.append(_qps(row))
    finally:
        evaluator.restore_host()
    ctx.state.setdefault("last_calibrate", {})[software] = exp.started.isoformat()
    base, sigma = ctx.baseline(software)
    ctx.state.setdefault("noise", {})[software] = {"baseline": base, "sigma": sigma}
    return {"status": "ok", "values": values, "baseline": base, "sigma": sigma,
            "client_ceiling": ctx.state.get("client_ceiling")}


# ---------------------------------------------------------------------------
# explore (open-mode agent)
# ---------------------------------------------------------------------------

def _confirm(ctx, evaluator, software):
    """Adaptive replicates: re-measure the changes within confirm_within_sigma
    of the best until they have confirm_replicates runs."""
    cfg = ctx.cfg.get("explore", {})
    _, sigma = ctx.baseline(software)
    want = cfg.get("confirm_replicates", 3)
    groups = {}
    for row in list(evaluator.rows.values()):
        groups.setdefault(row["point_id"].split("__rep")[0], []).append(row)
    scores = {k: [_qps(r) for r in rows if _qps(r)] for k, rows in groups.items()}
    best = max((stats.mean(v) for v in scores.values() if v), default=None)
    confirmed = []
    for key, rows in groups.items():
        values = scores[key]
        while stats.needs_replicates(stats.mean(values), best, sigma, len(values), want,
                                     cfg.get("confirm_within_sigma", 2.0)):
            if os.path.exists(ctx.flag("STOP")):
                return confirmed
            n = len(rows) + 1
            while f"{key}__rep{n}" in evaluator.rows:
                n += 1
            row = evaluator.evaluate({k: rows[0][k] for k in
                                      ("label", "named_options", "host_commands",
                                       "revert_commands", "reboot_after_apply", "knobs")},
                                     f"[confirm {key}]",
                                     {"repeat_of": key, "hypothesis": "campaign confirmation"},
                                     pid=f"{key}__rep{n}")
            rows.append(row)
            if _qps(row) is None:
                break
            values.append(_qps(row))
        if len(values) >= want:
            confirmed.append({"point_id": key, "mean": stats.mean(values), "n": len(values)})
    return confirmed


def explore(ctx, exp):
    from agent_search import make_open_search, write_report
    from agent_tools import AgentError
    software = exp.software
    if not ctx.ensure_software(software):
        return {"status": "skipped", "skipped": f"{software} unavailable"}
    cfg = ctx.cfg.get("explore", {})
    max_configs = cfg.get("max_configs", 20)
    if ctx.simulate:
        return _explore_simulated(ctx, exp, max_configs)
    config = ctx.measurement_config(software)
    transcript = Transcript(os.path.join(exp.dir, "agent_transcript.jsonl"))
    built = make_open_search(
        config, ctx.server, get_adapter(software), exp.dir, exp.dir, max_configs,
        ctx.cfg.get("point_timeout", 21600), transcript, catalog=ctx.catalog,
        facts=ctx.facts, ledger=ctx.ledger(exp), validator=ctx.validator(software),
        extra_context=ctx.ledger_summary(software),
        proposals_path=os.path.join(ctx.state_dir, "knobs_proposed.jsonl"))
    if built is None:
        return {"status": "error", "error": "could not snapshot the host"}
    evaluator, tools = built
    tools.context["env"] = ctx.env
    runner = ctx.make_runner(max_configs)
    outcome = {"status": "ok"}
    try:
        try:
            runner.run(tools, transcript)
        except AgentError as e:
            log.error("Agent stopped: %s", e)
            outcome = {"status": "agent_error", "error": str(e)}
        exhausted = getattr(runner, "usage_exhausted", lambda: [])()
        if exhausted:
            ctx.pause_llm(f"usage limit reached: {exhausted}")
        outcome["confirmed"] = _confirm(ctx, evaluator, software)
    finally:
        evaluator.export()
        evaluator.restore_host()
    write_report(tools, exp.dir)
    best = tools.best()
    outcome.update(best_qps=best[1] if best else None, measurements=len(evaluator.rows),
                   recommended=(tools.report or {}).get("recommended_point_id"))
    if not evaluator.rows and outcome["status"] != "ok":
        outcome["deferred"] = True   # nothing measured (e.g. usage): try again later
    return outcome


def _explore_simulated(ctx, exp, max_configs):
    """--simulate stand-in for the agent: random knob sets from the catalog."""
    software = exp.software
    rng = random.Random(exp.id)
    names = [n for n in ctx.catalog.knobs if ctx.catalog.applicable(n, software)
             and not ctx.catalog.knobs[n].get("reboot")]
    evaluator = ctx.open_evaluator(exp, "agent_open_results")
    best = None
    for i in range(min(max_configs, 8)):
        knobs = {n: ctx.catalog.typical_value(n) for n in rng.sample(names, rng.randint(1, 6))}
        row = ctx.measure_knobs(evaluator, knobs, f"sim{i}", 1, software)
        q = _qps(row)
        best = max(best or 0, q or 0)
    _confirm(ctx, evaluator, software)
    return {"status": "ok", "best_qps": best, "simulated": True,
            "measurements": len(evaluator.rows)}


# ---------------------------------------------------------------------------
# attribute (no LLM)
# ---------------------------------------------------------------------------

def attribute(ctx, exp):
    software = exp.software
    cfg = ctx.cfg.get("attribute", {})
    best_knobs, best_mean, runs = ctx.best_knobs(software)
    if not best_knobs:
        return {"status": "skipped", "skipped": "no valid knob-tagged result to attribute yet"}
    names = [n for n in best_knobs if ctx.catalog.applicable(n, software)]
    names = names[:cfg.get("max_knobs", 8)]
    best_knobs = {n: best_knobs[n] for n in names}
    reps = cfg.get("replicates", 3)
    base, _ = ctx.baseline(software)
    plan = []
    for rep in range(1, reps + 1):
        plan.append((best_knobs, "best", rep, best_mean))
        plan.append(({}, "baseline", rep, base))
        if len(names) > 1:
            for n in names:
                plan.append(({n: best_knobs[n]}, f"add:{n}", rep, base))
                plan.append(({k: v for k, v in best_knobs.items() if k != n}, f"loo:{n}",
                             rep, best_mean))
        else:
            plan.append(({names[0]: best_knobs[names[0]]}, f"add:{names[0]}", rep, base))
    _write(exp, "plan.json", {"best_knobs": best_knobs, "plan": plan})
    evaluator = ctx.open_evaluator(exp, "attribute_results")
    try:
        scores = _measure_plan(ctx, evaluator, plan, software, exp.id)
    finally:
        evaluator.export()
        evaluator.restore_host()
    reference = stats.mean(scores.get("baseline", []))
    effects = {}
    for n in names:
        add = stats.effect(scores.get(f"add:{n}", []), scores.get("baseline", []), reference)
        loo = stats.effect(scores.get("best", []), scores.get(f"loo:{n}", []), reference)
        effects[n] = {"value": best_knobs[n],
                      "add_pct": add and round(add["effect_pct"], 1),
                      "add_ci_pct": add and [round(x, 1) if x is not None else None
                                             for x in add["ci_pct"]],
                      "loo_pct": loo and round(loo["effect_pct"], 1),
                      "loo_ci_pct": loo and [round(x, 1) if x is not None else None
                                             for x in loo["ci_pct"]],
                      "significant": bool((add and add["significant"])
                                          or (loo and loo["significant"]))}
    summary = {"best_knobs": best_knobs, "baseline": reference,
               "best": stats.mean(scores.get("best", [])), "effects": effects,
               "scores": scores, "experiment": exp.id}
    _write(exp, "attribution.json", summary)
    ctx.state.setdefault("attribution", {})[software] = summary
    return {"status": "ok", "best_qps": summary["best"], "effects": effects}


# ---------------------------------------------------------------------------
# factorial (no LLM)
# ---------------------------------------------------------------------------

def factorial(ctx, exp):
    software = exp.software
    cfg = ctx.cfg.get("factorial", {})
    shared = ctx.inbox("shared_knobs.json") or {}
    candidates = list(shared.get("knobs", {}).items()) if shared else []
    attribution = ctx.state.get("attribution", {}).get(software, {})
    ranked = sorted(attribution.get("effects", {}).items(),
                    key=lambda kv: -max(abs(kv[1].get("add_pct") or 0),
                                        abs(kv[1].get("loo_pct") or 0)))
    candidates += [(n, e["value"]) for n, e in ranked]
    factors = {}
    for name, value in candidates:
        if (name in ctx.catalog.knobs and name not in factors
                and ctx.catalog.knobs[name].get("portable")
                and ctx.catalog.applicable(name, software)
                and not ctx.catalog.knobs[name].get("reboot")):
            factors[name] = value
        if len(factors) >= cfg.get("max_factors", 8):
            break
    if len(factors) < cfg.get("min_factors", 3):
        return {"status": "skipped",
                "skipped": f"only {len(factors)} portable factor(s) known so far"}
    names = list(factors)
    design = stats.fractional_factorial(names)
    base, _ = ctx.baseline(software)
    best = attribution.get("best") or base
    plan = []
    for rep in range(1, cfg.get("replicates", 3) + 1):
        for i, row in enumerate(design):
            knobs = {n: factors[n] for n in names if row[n] == 1}
            expect = (base + best) / 2 if base and best else None
            plan.append((knobs, f"run{i:02d}", rep, expect))
    _write(exp, "design.json", {"factors": factors, "design": design})
    evaluator = ctx.open_evaluator(exp, "factorial_results")
    try:
        scores = _measure_plan(ctx, evaluator, plan, software, exp.id)
    finally:
        evaluator.export()
        evaluator.restore_host()
    responses = [scores.get(f"run{i:02d}", []) for i in range(len(design))]
    reference = stats.mean(responses[0]) or base
    main = stats.main_effects(design, responses)
    inter = stats.interaction_effects(design, responses)
    pct = (lambda v: None if v is None or not reference else round(100 * v / reference, 1))
    summary = {"factors": factors, "design": design, "responses": responses,
               "reference": reference,
               "main_effects_pct": {k: pct(v) for k, v in main.items()},
               "interactions_pct": {k: pct(v) for k, v in inter.items()},
               "experiment": exp.id}
    _write(exp, "factorial.json", summary)
    ctx.state.setdefault("factorial", {})[software] = summary
    return {"status": "ok", "main_effects_pct": summary["main_effects_pct"]}


# ---------------------------------------------------------------------------
# source (source-code agent)
# ---------------------------------------------------------------------------

def source(ctx, exp):
    from agent_tools import AgentError
    from source_tools import (SourceEvaluator, SourceTools, SourceWorkspace,
                              frozen_profile)
    software = exp.software
    cfg = ctx.cfg.get("source", {})
    if ctx.simulate:
        return {"status": "skipped", "skipped": "source needs real hosts", "deferred": False}
    if not ctx.ensure_software(software):
        return {"status": "skipped", "skipped": f"{software} unavailable"}
    adapter = get_adapter(software)
    validator = ctx.validator(software)
    ref = (cfg.get("refs") or {}).get(software)
    if not ref:
        identity = adapter.identity(ctx.server, adapter.package_service)
        ref = adapter.release_tag(identity.get("version"))
    host_knobs = (ctx.state.get("attribution", {}).get(software, {}).get("best_knobs")
                  or ctx.state.get("attribution", {}).get("bind", {}).get("best_knobs") or {})
    profile = frozen_profile(ctx.catalog, host_knobs, software, ctx.facts)
    _write(exp, "host_profile.json", profile)

    workspace = SourceWorkspace(os.path.join(ctx.state_dir, "src"), adapter, ref)
    workspace.ensure(f"campaign/{exp.id}")
    evaluator = ctx.open_evaluator(exp, "source_results", service=adapter.source_service,
                                   cls=SourceEvaluator, builds={})
    transcript = Transcript(os.path.join(exp.dir, "agent_transcript.jsonl"))
    context = {k: ctx.search_config[k] for k in ("tool", "max_sustainable_qps", "hosts")
               if k in ctx.search_config}
    context.update(env=ctx.env, software=software, base_ref=ref, server_facts=ctx.facts)
    tools = SourceTools(evaluator, workspace, profile, cfg.get("max_configs", 12)
                        + cfg.get("base_replicates", 3), cfg.get("max_builds", 10),
                        transcript, context, base_build=None)
    outcome = {"status": "ok", "base_ref": ref}
    try:
        base = tools.build("base", agent=False)
        if base["status"] != "ok":
            return {"status": "error", "error": "the unpatched source did not build",
                    "log": base.get("log", "")[-2000:]}
        tools.base_build = base["build_id"]
        check = tools.correctness_check(base["build_id"])
        if not check["ok"]:
            return {"status": "error", "error": "the unpatched source build gives "
                                                f"different answers: {check['reasons']}"}
        for rep in range(cfg.get("base_replicates", 3)):
            tools.measure(base, "from-source baseline", repeat=rep > 0, label="base")
        runner = ctx.make_runner(cfg.get("max_configs", 12), workdir=workspace.path,
                                 builtin_tools=["Read", "Grep", "Glob", "Edit", "Write"])
        try:
            runner.run(tools, transcript)
        except AgentError as e:
            log.error("Agent stopped: %s", e)
            outcome = {"status": "agent_error", "error": str(e), "base_ref": ref}
        exhausted = getattr(runner, "usage_exhausted", lambda: [])()
        if exhausted:
            ctx.pause_llm(f"usage limit reached: {exhausted}")
        recommended = (tools.report or {}).get("recommended_build_id")
        if recommended and recommended != base["build_id"]:
            _confirm_build(ctx, tools, recommended, software)
    finally:
        evaluator.export()
        tools.export_patches(os.path.join(exp.dir, "patches"))
        if tools.report:
            _write(exp, "source_report.json", tools.report)
        evaluator.restore_host()
    outcome.update(_source_summary(tools, base["build_id"]))
    return outcome


def _confirm_build(ctx, tools, build_id, software):
    want = ctx.cfg.get("explore", {}).get("confirm_replicates", 3)
    build = tools.builds[build_id]
    have = [r for r in tools.evaluator.rows.values() if r.get("build_id") == build_id]
    while len(have) < want and not os.path.exists(ctx.flag("STOP")):
        result = tools.measure(build, "campaign confirmation", repeat=bool(have))
        have.append(result)


def _source_summary(tools, base_id):
    by_build = {}
    for row in tools.evaluator.rows.values():
        q = _qps(row)
        if q:
            by_build.setdefault(row.get("build_id"), []).append(q)
    base = stats.mean(by_build.get(base_id, []))
    out = {}
    for build_id, values in by_build.items():
        e = stats.effect(values, by_build.get(base_id, []), base)
        out[build_id] = {"mean": stats.mean(values), "n": len(values),
                         "label": tools.builds.get(build_id, {}).get("label"),
                         "effect_pct": e and e["effect_pct"], "ci_pct": e and e["ci_pct"]}
    best = max(out.values(), key=lambda v: v["mean"], default={})
    return {"builds": out, "best_qps": best.get("mean"), "base_qps": base}
