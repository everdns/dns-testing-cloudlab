#!/usr/bin/env python3
"""Continuous optimization campaign for one environment (runs on test_host_0).

The batch scripts (optimization/run_searches.sh) run three searches and stop.
A campaign keeps the environment busy for as long as it exists and turns every
measurement into a ledger record that the cross-hardware analysis can use:

    health check -> next step -> run it -> reset the server -> repeat

Steps (phases.py):

  calibrate  stock baseline x3 (noise), client send-rate ceiling; runs first and
             whenever it is due (calibrate.every_hours)
  explore    the open-mode Claude agent, tagging every change with catalog
             knobs; candidates near the best are then re-measured
  attribute  no LLM: for the best valid knob set B, measure baseline + k and
             B - k for every knob k, with replicates, in shuffled order
  factorial  no LLM: two-level fractional factorial over the top portable knobs
             (this environment's and, via the inbox, the other environments')
  source     the source-code agent on bind / knot / nsd in turn, on the
             environment's best host profile, against a from-source baseline

The cycle (campaign.yaml ``cycle``) repeats until ``touch ~/campaign/STOP``.
State lives in ~/campaign (state.json, ledger.jsonl, experiments/, inbox/,
campaign.log); after a crash or reboot the next start resets the server and
continues with the next step. When Claude usage runs out, LLM steps are
deferred and the measurement-only steps keep the hardware busy.

    python3 campaign.py run --config ~/campaign/campaign.yaml
    python3 campaign.py run --config ... --simulate --max-experiments 6   # no hosts
    python3 campaign.py run --config ... --step attribute --max-experiments 1
    python3 campaign.py status --config ~/campaign/campaign.yaml
    python3 campaign.py stop --config ~/campaign/campaign.yaml
"""
import argparse
import copy
import datetime as dt
import json
import logging
import os
import random
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
OPTIMIZATION = os.path.join(REPO, "optimization")
sys.path.insert(0, OPTIMIZATION)
sys.path.insert(0, HERE)

import yaml  # noqa: E402

import host_state  # noqa: E402
import knobs as knobs_mod  # noqa: E402
import ledger as ledger_mod  # noqa: E402
import searches_reset  # noqa: E402
import stats  # noqa: E402
import validity  # noqa: E402
from agent_tools import OpenEvaluator, decode_change, encode_change  # noqa: E402
from max_sustainable_qps import get_clients, ssh_run  # noqa: E402
from servers import get_adapter  # noqa: E402

log = logging.getLogger("campaign")

LLM_STEPS = ("explore", "source")
STEPS = ("calibrate", "explore", "attribute", "factorial", "source")
HEALTH_RETRY_S = 600
EXIT_RESTORE_FAILED = 3


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


def parse_time(text):
    if not text:
        return None
    if isinstance(text, dt.datetime):
        return text if text.tzinfo else text.replace(tzinfo=dt.timezone.utc)
    value = dt.datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)


def knob_key(knobs):
    """Canonical string for a knob selection (groups replicates)."""
    return json.dumps(knobs or {}, sort_keys=True)


class SimEvaluator(OpenEvaluator):
    """--simulate: nothing touches a host. Each point's max QPS comes from a
    made-up response surface over its knobs (a baseline, a hidden effect per
    knob, one interaction and 2% noise), so the scheduler, ledger and
    statistics can be exercised end to end on a laptop."""

    def __init__(self, *args, env="sim", **kwargs):
        super().__init__(*args, **kwargs)
        rng = random.Random(f"surface-{env}")
        self.base = rng.choice([150000, 300000, 450000])
        self.effects = {name: rng.choice([0.0, 0.0, 0.02, 0.05, 0.15, 0.3, -0.05])
                        for name in sorted(knobs_mod.load().knobs)}

    def surface(self, knobs, pid):
        value = self.base
        for name in knobs:
            value *= 1 + self.effects.get(name, 0.0)
        if "irq_pin" in knobs and "rps" in knobs:
            value *= 0.95
        value *= 1 + random.Random(pid).gauss(0, 0.02)
        return max(10000, int(value))

    def evaluate(self, overrides, label, extra=None, pid=None):
        knobs = decode_change(overrides)["knobs"]
        self.config["simulate_max_qps"] = self.surface(knobs, pid or label)
        return super().evaluate(overrides, label, extra, pid)

    def _apply(self, overrides, run_dir):
        pass

    def _restore(self, overrides, run_dir):
        return {}

    def restore_host(self):
        return [], []


class Experiment:
    def __init__(self, ctx, step, software):
        ctx.state["experiment_seq"] = ctx.state.get("experiment_seq", 0) + 1
        self.seq = ctx.state["experiment_seq"]
        self.step = step
        self.software = software
        self.id = f"{self.seq:04d}_{step}_{software}"
        self.dir = os.path.join(ctx.state_dir, "experiments", self.id)
        os.makedirs(self.dir, exist_ok=True)
        self.started = utcnow()

    def save(self, **fields):
        path = os.path.join(self.dir, "experiment.json")
        data = {"id": self.id, "step": self.step, "software": self.software,
                "started": self.started.isoformat(), **fields}
        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)
        return data


class Campaign:
    def __init__(self, config_path, simulate=False):
        self.config_path = os.path.abspath(os.path.expanduser(config_path))
        with open(self.config_path) as f:
            self.cfg = yaml.safe_load(f)
        self.env = self.cfg.get("env")
        if not self.env or self.env == "CHANGE_ME":
            raise SystemExit(f"{self.config_path}: set env (the hardware type)")
        self.simulate = simulate
        self.state_dir = os.path.expanduser(self.cfg.get("state_dir", "~/campaign"))
        if simulate:
            self.state_dir = os.path.join(self.state_dir, "simulate")
        os.makedirs(self.state_dir, exist_ok=True)
        self.state_path = os.path.join(self.state_dir, "state.json")
        self.ledger_path = os.path.join(self.state_dir, "ledger.jsonl")
        self.state = self._load_state()

        search_path = self.cfg.get("search_config", "../optimization/agent_search.yaml")
        if not os.path.isabs(search_path):
            search_path = os.path.normpath(os.path.join(HERE, search_path))
        with open(search_path) as f:
            self.search_config = yaml.safe_load(f)
        self.server = self.search_config["hosts"]["server"]
        self.clients = get_clients(self.search_config)
        self.catalog = knobs_mod.load()
        self.facts = self.state.get("facts") or {}
        self._validators = {}

    # -- state --------------------------------------------------------------

    def _load_state(self):
        if os.path.exists(self.state_path):
            with open(self.state_path) as f:
                return json.load(f)
        return {"created": utcnow().isoformat(), "step_index": 0, "history": [],
                "software_index": 0, "source_index": 0}

    def save_state(self):
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.state, f, indent=2, default=str)
        os.replace(tmp, self.state_path)

    def flag(self, name):
        return os.path.join(self.state_dir, name)

    def alert(self, message):
        log.error("ALERT: %s", message)
        with open(self.flag("ALERT"), "a") as f:
            f.write(f"{utcnow().isoformat()} {message}\n")

    # -- measurement building blocks ----------------------------------------

    def measurement_config(self, software, service=None):
        config = copy.deepcopy(self.search_config)
        adapter = get_adapter(software)
        config["dns_service"] = service or adapter.package_service
        cap = self.state.get("max_qps_cap")
        if cap:
            section = config["max_sustainable_qps"]
            section["max_qps"] = min(int(section["max_qps"]), cap)
        return config

    def ledger(self, exp, phase=None):
        section = self.search_config["max_sustainable_qps"]
        return ledger_mod.Ledger(
            self.ledger_path, env=self.env, hardware=self.facts,
            software=exp.software, phase=phase or exp.step, experiment_id=exp.id,
            min_qps_fidelity_pct=section.get("min_qps_fidelity_pct", 99.0),
            simulated=self.simulate or None)

    def validator(self, software):
        if self.simulate or not self.cfg.get("validate", True):
            return None
        if software not in self._validators:
            v = validity.Validator(self.measurement_config(software), get_adapter(software),
                                   state_dir=os.path.join(self.state_dir, "validity"))
            v.ensure_reference()
            self._validators[software] = v
        return self._validators[software]

    def open_evaluator(self, exp, results_name, service=None, cls=None, **kwargs):
        """An OpenEvaluator (host_state restore) for this experiment; in
        --simulate a SimEvaluator."""
        adapter = get_adapter(exp.software)
        config = self.measurement_config(exp.software, service)
        common = dict(adapter=adapter, validator=self.validator(exp.software),
                      ledger=self.ledger(exp), catalog=self.catalog, **kwargs)
        timeout = self.cfg.get("point_timeout", 21600)
        if self.simulate:
            return SimEvaluator(config, self.server, adapter.load_base(), {}, exp.dir,
                                results_name, timeout, env=self.env, **common)
        baseline = host_state.snapshot(self.server, save_files=True)
        cls = cls or OpenEvaluator
        return cls(config, self.server, adapter.load_base(), baseline, exp.dir,
                   results_name, timeout, **common)

    def warm_start(self, evaluator, expect):
        """Start the next QPS search near ``expect`` (0.8x, ramp x1.25), or
        from the configured defaults when nothing is known."""
        section = evaluator.config["max_sustainable_qps"]
        default = self.search_config["max_sustainable_qps"]
        step = int(section["min_qps_step"])
        if expect:
            section["initial_qps"] = max(step, int(0.8 * expect) // step * step)
            section["ramp_factor"] = 1.25
        else:
            section["initial_qps"] = default["initial_qps"]
            section["ramp_factor"] = default.get("ramp_factor", 2.0)

    def measure_knobs(self, evaluator, knobs, group, rep, software, hypothesis="",
                      expect=None):
        """Render ``knobs`` through the catalog and measure them once."""
        rendered = self.catalog.render(knobs, software, self.facts)
        overrides = encode_change(group, rendered["named_options"],
                                  rendered["host_commands"], [],
                                  rendered["reboot_after_apply"], knobs)
        if self.cfg.get("attribute", {}).get("warm_start", True):
            self.warm_start(evaluator, expect)
        return evaluator.evaluate(overrides, f"[{group} r{rep}]",
                                  {"replicate_group": group, "hypothesis": hypothesis},
                                  pid=f"{group}__r{rep}")

    # -- reading the ledger ---------------------------------------------------

    def records(self, software=None, phases=None):
        """Usable ledger records of this environment."""
        out = []
        for r in ledger_mod.read(self.ledger_path):
            if r.get("env") != self.env or not ledger_mod.usable(r):
                continue
            if software and r.get("software") != software:
                continue
            if phases and r.get("phase") not in phases:
                continue
            if r.get("build_id") not in (None, "", "package") and r.get("phase") != "source":
                continue
            out.append(r)
        return out

    def by_knobs(self, records):
        groups = {}
        for r in records:
            if r.get("phase") == "source":
                continue
            groups.setdefault(knob_key(r.get("knobs")), []).append(r["max_qps_passed"])
        return groups

    def baseline(self, software):
        """(mean, sigma) of the stock configuration from recent calibrations."""
        records = self.records(software, phases=("calibrate",))[-9:]
        values = [r["max_qps_passed"] for r in records]
        groups = {}
        for r in self.records(software):
            groups.setdefault((r.get("experiment_id"), r.get("replicate_group")
                               or knob_key(r.get("knobs"))), []).append(r["max_qps_passed"])
        return stats.mean(values), stats.noise_sigma(groups)

    def best_knobs(self, software, min_runs=1):
        """(knobs, mean, runs) of the best non-empty knob set with at least
        ``min_runs`` usable runs, or (None, None, 0)."""
        best = (None, None, 0)
        for key, values in self.by_knobs(self.records(software)).items():
            knobs = json.loads(key)
            if not knobs or len(values) < min_runs:
                continue
            m = stats.mean(values)
            if best[1] is None or m > best[1]:
                best = (knobs, m, len(values))
        return best

    def inbox(self, name):
        path = os.path.join(self.state_dir, "inbox", name)
        if not os.path.exists(path):
            return None
        with open(path) as f:
            return json.load(f) if name.endswith(".json") else f.read()

    def ledger_summary(self, software, limit=12):
        """Text block for an agent prompt: what this environment already knows."""
        base, sigma = self.baseline(software)
        rows = []
        for key, values in self.by_knobs(self.records(software)).items():
            rows.append((stats.mean(values), len(values), json.loads(key)))
        rows.sort(key=lambda r: -r[0])
        lines = [f"Stock baseline: {base and round(base)} QPS; run-to-run sigma: "
                 f"{sigma and round(sigma)} QPS."]
        lines += [f"- {round(m)} QPS (n={n}): {json.dumps(k, sort_keys=True)}"
                  for m, n, k in rows[:limit]]
        attribution = self.state.get("attribution", {}).get(software)
        if attribution:
            lines.append("Attribution of the best configuration (single-add / "
                         "leave-one-out effect, % of baseline):")
            for name, e in attribution.get("effects", {}).items():
                lines.append(f"- {name}: add {e.get('add_pct')}, loo {e.get('loo_pct')}")
        text = ("<campaign_history env=\"%s\">\nValid measurements of this environment "
                "so far, best first (knob sets; earlier sessions).\n%s\n"
                "</campaign_history>" % (self.env, "\n".join(lines)))
        findings = self.inbox("shared_findings.md")
        if findings:
            text += ("\n\n<other_environments>\nFindings on other hardware. Test the "
                     "ones that may transfer; do not assume they do.\n"
                     f"{findings[:6000]}\n</other_environments>")
        return text

    # -- environment upkeep ---------------------------------------------------

    def fingerprint(self):
        if self.simulate:
            self.facts = {"iface": "sim0", "cpus": 16, "cpu_model": "simulated"}
        else:
            self.facts = ledger_mod.fingerprint(self.server, self.clients[0]) or self.facts
        self.state["facts"] = self.facts
        self.save_state()

    def deploy_repo(self):
        """Copy this checkout's ns_software/ and service scripts to the name
        server's /local/repository, so it has the source-build services."""
        if self.simulate:
            return
        result = subprocess.run(
            ["rsync", "-a", "--rsync-path", "sudo rsync",
             "-e", "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new",
             "--include", "ns_software/***", "--include", "*.sh", "--exclude", "*",
             f"{REPO}/", f"{self.server}:/local/repository/"],
            capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            log.warning("Could not deploy ns_software to %s: %s", self.server,
                        result.stderr.strip()[-300:])

    def expiry_check(self):
        text = None
        path = self.flag("expires_at")
        if os.path.exists(path):
            with open(path) as f:
                text = f.read().strip()
        expires = parse_time(text or self.cfg.get("expires_at"))
        if not expires:
            return
        hours = (expires - utcnow()).total_seconds() / 3600
        self.state["hours_left"] = round(hours, 1)
        if hours < self.cfg.get("alert_hours", 24) and not self.state.get("expiry_alerted"):
            self.alert(f"the experiment expires in {hours:.1f} h ({expires.isoformat()}); "
                       "extend it (cloudlab_extend) or let the campaign end")
            self.state["expiry_alerted"] = True

    def health_check(self):
        """True if the server and clients answer SSH and the tester has disk."""
        if self.simulate:
            return True
        problems = []
        for host in [self.server, *self.clients]:
            try:
                if ssh_run(host, "true", timeout=30).returncode != 0:
                    problems.append(f"{host} does not accept SSH")
            except subprocess.TimeoutExpired:
                problems.append(f"{host} SSH timed out")
        free_gb = shutil.disk_usage(self.state_dir).free / 2**30
        if free_gb < 1:
            problems.append(f"only {free_gb:.1f} GB free in {self.state_dir}")
        for p in problems:
            log.warning("Health: %s", p)
        return not problems

    def pristine_path(self):
        return self.flag("pristine.json")

    def snapshot_pristine(self):
        if self.simulate or os.path.exists(self.pristine_path()):
            return
        log.info("Snapshotting the pristine name server state")
        if searches_reset.snapshot(self.server, self.pristine_path()) != 0:
            raise SystemExit("could not snapshot the name server")

    def reset_server(self):
        """Back to the pristine host and every used software's base config.
        Returns the searches_reset exit code."""
        if self.simulate:
            return 0
        used = sorted(set(self.state.get("software_used", [])) | {"bind"})
        config = self.measurement_config(self.cfg.get("software", ["bind"])[0])
        rc = searches_reset.reset(self.server, config, self.pristine_path(),
                                  adapters=[get_adapter(s) for s in used])
        if rc == EXIT_RESTORE_FAILED:
            self.alert("the name server could not be restored; the campaign stopped. "
                       "Reboot or reinstall it, then restart the campaign")
        return rc

    def ensure_software(self, software):
        """Make sure ``software`` is installed on the server with the
        campaign's zone. Returns False if it cannot be."""
        used = self.state.setdefault("software_used", [])
        if self.simulate or software in used:
            if software not in used:
                used.append(software)
            return True
        adapter = get_adapter(software)
        have = ssh_run(self.server, f"test -x {adapter.binary}", timeout=60).returncode == 0
        if not have:
            if not self.cfg.get("source", {}).get("install_missing", True):
                log.warning("%s is not installed on %s", software, self.server)
                return False
            log.info("Installing %s on %s", software, self.server)
            result = ssh_run(self.server, f"bash /local/repository/ns_software/{software}/install.sh",
                             timeout=3600)
            if result.returncode != 0:
                log.error("Installing %s failed: %s", software, (result.stderr or "")[-500:])
                return False
        if software != "bind":
            # The campaign's zone is the one deploy_zone.sh installed for BIND.
            ssh_run(self.server, "cd /local/repository && sudo ./update_zone_files.sh "
                                 f"/etc/bind/db.workbench.lan {software}", timeout=300)
            ssh_run(self.server, f"bash /local/repository/stop_dns_service.sh ns_{software}",
                    timeout=120)
        used.append(software)
        self.save_state()
        return True

    # -- usage ----------------------------------------------------------------

    def llm_available(self):
        until = parse_time(self.state.get("llm_paused_until"))
        return until is None or utcnow() >= until

    def pause_llm(self, why):
        hours = self.cfg.get("agent", {}).get("backoff_hours", 2)
        until = utcnow() + dt.timedelta(hours=hours)
        self.state["llm_paused_until"] = until.isoformat()
        log.warning("LLM steps paused until %s: %s", until.isoformat(), why)

    def make_runner(self, max_configs, **kwargs):
        from agent_runner import make_runner
        agent = self.cfg.get("agent", {})
        usage = {w: agent[k] / 100 for w, k in (("five_hour", "max_session_usage"),
                                                ("seven_day", "max_weekly_usage"))
                 if agent.get(k)}
        return make_runner(agent.get("backend", "claude-code"), model=agent.get("model"),
                           effort=agent.get("effort", "high"),
                           max_turns=agent.get("max_turns") or 14 * max_configs + 30,
                           claude_bin=agent.get("claude_bin", "claude"),
                           tool_timeout_s=self.cfg.get("point_timeout", 21600),
                           usage_limits=usage, **kwargs)

    # -- scheduling -----------------------------------------------------------

    def calibrate_due(self, software):
        last = parse_time(self.state.get("last_calibrate", {}).get(software))
        hours = self.cfg.get("calibrate", {}).get("every_hours", 24)
        return last is None or utcnow() - last >= dt.timedelta(hours=hours)

    def current_software(self):
        software = self.cfg.get("software") or ["bind"]
        return software[self.state.get("software_index", 0) % len(software)]

    def next_step(self):
        """(step, software) to run next, or (None, None) if only deferred LLM
        steps remain."""
        software = self.current_software()
        if self.calibrate_due(software):
            return "calibrate", software
        cycle = self.cfg.get("cycle") or ["explore", "attribute", "factorial", "source"]
        for offset in range(len(cycle)):
            index = (self.state.get("step_index", 0) + offset) % len(cycle)
            step = cycle[index]
            if step in LLM_STEPS and not self.llm_available():
                continue
            if step == "source":
                if not self.cfg.get("source", {}).get("enabled", True):
                    continue
                rotation = self.cfg["source"].get("rotation") or ["bind"]
                target = rotation[self.state.get("source_index", 0) % len(rotation)]
            else:
                target = software
            self.state["step_index"] = index
            return step, target
        return None, None

    def advance(self, step):
        cycle = self.cfg.get("cycle") or []
        self.state["step_index"] = (self.state.get("step_index", 0) + 1) % max(1, len(cycle))
        if step == "source":
            self.state["source_index"] = self.state.get("source_index", 0) + 1
        if cycle and self.state["step_index"] == 0:
            self.state["cycle"] = self.state.get("cycle", 0) + 1
            self.state["software_index"] = self.state.get("software_index", 0) + 1

    def run_step(self, step, software):
        import phases
        exp = Experiment(self, step, software)
        self.state["current"] = {"id": exp.id, "started": exp.started.isoformat()}
        self.save_state()
        log.info("===== %s: %s on %s =====", exp.id, step, software)
        outcome = {"status": "error"}
        try:
            outcome = getattr(phases, step)(self, exp) or {"status": "ok"}
        except SystemExit:
            raise
        except Exception as e:  # noqa: BLE001 - one failed step must not end the campaign
            log.exception("%s failed", exp.id)
            outcome = {"status": "error", "error": str(e)}
        record = exp.save(finished=utcnow().isoformat(), **outcome)
        self.state.setdefault("history", []).append(
            {k: record.get(k) for k in ("id", "step", "software", "status", "finished",
                                        "best_qps", "skipped", "error")})
        self.state["history"] = self.state["history"][-200:]
        self.state.pop("current", None)
        self.save_state()
        log.info("===== %s finished: %s =====", exp.id, outcome.get("status"))
        return outcome

    def run(self, max_experiments=None, only_step=None):
        self.fingerprint()
        self.snapshot_pristine()
        self.deploy_repo()
        if self.state.get("current"):
            log.warning("Previous run stopped during %s; resetting the server",
                        self.state["current"].get("id"))
            self.state.pop("current", None)
        if self.reset_server() == EXIT_RESTORE_FAILED:
            return EXIT_RESTORE_FAILED
        done = 0
        failures = 0
        while max_experiments is None or done < max_experiments:
            if os.path.exists(self.flag("STOP")):
                log.info("STOP file found; ending the campaign")
                return 0
            self.expiry_check()
            if not self.health_check():
                failures += 1
                if failures == 6:
                    self.alert("health checks have failed for an hour; waiting")
                time.sleep(HEALTH_RETRY_S)
                continue
            failures = 0
            if only_step:
                step, software = only_step, self.current_software()
                if step == "source":
                    rotation = self.cfg.get("source", {}).get("rotation") or ["bind"]
                    software = rotation[self.state.get("source_index", 0) % len(rotation)]
            else:
                step, software = self.next_step()
            if step is None:
                log.info("Only LLM steps are left and Claude usage is paused; sleeping")
                time.sleep(HEALTH_RETRY_S)
                continue
            outcome = self.run_step(step, software)
            if not only_step and step != "calibrate" and not outcome.get("deferred"):
                self.advance(step)
            self.save_state()
            done += 1
            if self.reset_server() == EXIT_RESTORE_FAILED:
                return EXIT_RESTORE_FAILED
        return 0


def status(ctx):
    s = ctx.state
    print(f"env {ctx.env}  state {ctx.state_dir}")
    print(f"cycle {s.get('cycle', 0)}  step_index {s.get('step_index')}  "
          f"hours_left {s.get('hours_left')}  llm_paused_until {s.get('llm_paused_until')}")
    if s.get("current"):
        print(f"running: {s['current']}")
    for software in sorted(set(s.get("software_used", [])) | {"bind"}):
        base, sigma = ctx.baseline(software)
        knobs, best, runs = ctx.best_knobs(software)
        print(f"{software}: baseline {base and round(base)}  sigma {sigma and round(sigma)}  "
              f"best {best and round(best)} (n={runs}) {json.dumps(knobs) if knobs else ''}")
    print("recent:")
    for h in s.get("history", [])[-10:]:
        print(f"  {h.get('id')}: {h.get('status')} {h.get('best_qps') or ''} "
              f"{h.get('skipped') or h.get('error') or ''}")
    if os.path.exists(ctx.flag("ALERT")):
        print("ALERT:\n" + open(ctx.flag("ALERT")).read())
    if os.path.exists(ctx.flag("STOP")):
        print("STOP requested")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=["run", "status", "stop"])
    parser.add_argument("--config", default=os.path.expanduser("~/campaign/campaign.yaml"))
    parser.add_argument("--simulate", action="store_true",
                        help="No hosts: a made-up response surface stands in for the "
                             "server (state under <state_dir>/simulate)")
    parser.add_argument("--max-experiments", type=int,
                        help="Stop after this many steps (default: run until STOP)")
    parser.add_argument("--step", choices=STEPS,
                        help="Run only this step (with --max-experiments)")
    args = parser.parse_args()

    ctx = Campaign(args.config, simulate=args.simulate)
    if args.action == "status":
        status(ctx)
        return 0
    if args.action == "stop":
        open(ctx.flag("STOP"), "w").close()
        print(f"{ctx.flag('STOP')} written; the campaign stops after the current measurement")
        return 0

    handlers = [logging.StreamHandler(),
                logging.FileHandler(os.path.join(ctx.state_dir, "campaign.log"))]
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if os.path.exists(ctx.flag("STOP")) and args.max_experiments is None:
        os.unlink(ctx.flag("STOP"))
    return ctx.run(args.max_experiments, args.step)


if __name__ == "__main__":
    sys.exit(main())
