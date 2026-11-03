"""Source-code optimization: tools for an agent that patches a DNS server.

Config tuning stops at what the server's code allows; the d710 open-mode
report ended at "a flat profile, no hotspot a config option can remove". This
mode lets an agent change the code itself, under the same measurement and
validity rules as every other search.

The agent runs as one Claude Code session whose working directory is a git
checkout of the server's source (SourceWorkspace, on the tester), with Claude
Code's Read/Grep/Glob/Edit tools for reading and editing it. Everything that
touches the name server goes through these tools:

  profile_server     hold load on the server and record a perf profile
  build_variant      commit the working tree, build it on the server
  checkout           reset the working tree to the base or an earlier build
  correctness_check  start a build and compare its answers with the reference
  measure_variant    max sustainable QPS of a build (search.Evaluator)
  get_results, get_run_details, finish

Builds (servers/build_source.sh) all install into one prefix per software,
which ns_software/custom_server/start.sh runs (its info.yaml is rewritten
for the software being measured); each build is kept as a tarball and
re-activated before it is measured. Every measurement runs on the
environment's frozen host profile (knobs, e.g. the best valid host tuning
from the attribution phase), so the comparison is code against code. The
baseline is the unpatched source at the same tag, built the same way: never
the distro package.
"""
import hashlib
import json
import logging
import os
import re
import shlex
import subprocess
import threading
import time

from agent_tools import (AgentError, OpenEvaluator, SearchTools, _clip,
                         decode_change, encode_change)
from max_sustainable_qps import (TOOLS, client_interface_for, get_clients,
                                 reset_clients, scp_to, split_qps, ssh_run,
                                 ssh_run_many, start_dns_service, stop_dns_service,
                                 wait_for_dns_ready)
from search import row_qps
from servers import ServerConfigError

log = logging.getLogger("source_tools")

BUILD_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "servers",
                            "build_source.sh")
REMOTE_SRC = "dnsbuild-src"          # under the server user's home
BUILD_TIMEOUT = 5400
PROFILE_SECONDS = 40
MAX_PATCH_TEXT = 6000

SOURCE_SYSTEM_PROMPT = """\
You are optimizing the source code of an authoritative DNS server ({server_name}) \
for throughput: the highest max sustainable QPS, the highest query rate at which \
the server still answers nearly every query, measured by load generators on \
separate machines. Host tuning is already fixed (see <host_profile>); your lever \
is the code.

Your working directory is a git checkout of the server's source at {base_ref}. \
Read and edit it with your file tools (Read, Grep, Glob, Edit). You cannot run \
shell commands; everything that touches the name server goes through the \
dnssearch tools:
- profile_server holds load on the server and returns a perf profile (hottest \
symbols with call chains, plus cycles/instructions/cache misses) of the base build \
or of a build you made. Use it before guessing.
- build_variant commits your current edits and builds them on the server \
(minutes; incremental). A build that fails returns the compiler output.
- correctness_check starts a build and compares its answers to a sample of \
queries with the stock server's answers. Cheap; run it before measuring.
- measure_variant measures a build's max sustainable QPS (10+ minutes). It is \
the scarce resource: a fixed budget, and every call counts even if it fails.
- checkout resets the working tree to the base or to an earlier build, to start \
a new idea cleanly.
- get_results / get_run_details show what was measured; finish ends the session.

Rules:
- The server must stay a correct, general-purpose authoritative server. Answers \
must be byte-for-byte equivalent in meaning (rcode, flags, records, TTLs) for \
every query, not just the benchmark's. No special-casing of the benchmark's \
names, query types, zone or clients; no removing features (TCP, DNSSEC, zone \
transfers, updates, statistics) to save work, unless the same behaviour remains \
available and on by default.
- Every measurement is validity-checked (answers before, during and after load; \
zone and query input unchanged). validity_ok=False means the score does not count.
- Prefer changes a maintainer could accept: fewer allocations, less locking or \
atomics on the hot path, cache-friendlier data layout, batching syscalls \
(recvmmsg/sendmmsg), avoiding redundant work per query. Keep each variant to \
one idea where you can, so its effect is attributable.

How to work: profile the base build under load, read the hot code paths, form a \
hypothesis with an expected gain, make the smallest change that tests it, build, \
check correctness, then measure. The base build was measured several times; \
treat differences within that spread as noise, and repeat a promising variant \
(repeat=true) before claiming it. When the budget is used or nothing promising \
is left, call finish with the build you recommend, its patch's idea, the \
measured gain with your confidence, and what the profiles showed. Call finish \
exactly once, then end your turn."""


def _git(workdir, *args, check=True, timeout=600):
    result = subprocess.run(["git", "-C", workdir, *args], capture_output=True,
                            text=True, timeout=timeout)
    if check and result.returncode != 0:
        raise AgentError(f"git {' '.join(args)} failed: {result.stderr.strip()[-800:]}")
    return result.stdout.strip()


class SourceWorkspace:
    """A git checkout of one server's source on the tester, pinned to
    ``base_ref``. Each experiment works on its own branch from that ref."""

    def __init__(self, root, adapter, base_ref):
        self.adapter = adapter
        self.base_ref = base_ref
        self.path = os.path.join(root, adapter.name)

    def ensure(self, branch):
        """Clone if needed and start ``branch`` at the base ref (clean tree)."""
        if not os.path.isdir(os.path.join(self.path, ".git")):
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            log.info("Cloning %s at %s", self.adapter.source_repo, self.base_ref)
            subprocess.run(["git", "clone", "--depth", "1", "--branch", self.base_ref,
                            "--recurse-submodules", "--shallow-submodules",
                            self.adapter.source_repo, self.path],
                           check=True, capture_output=True, text=True, timeout=3600)
            _git(self.path, "config", "user.email", "campaign@localhost")
            _git(self.path, "config", "user.name", "dns campaign")
        _git(self.path, "fetch", "--depth", "1", "origin", "tag", self.base_ref,
             check=False, timeout=1800)
        _git(self.path, "checkout", "-f", "-B", branch, self.base_ref)
        _git(self.path, "clean", "-fdx", "-e", ".campaign-*")
        self.base_commit = _git(self.path, "rev-parse", "HEAD")

    def commit(self, label):
        """Commit the working tree; returns (commit, patch text against base)."""
        _git(self.path, "add", "-A")
        if _git(self.path, "status", "--porcelain"):
            _git(self.path, "commit", "-q", "-m", label)
        commit = _git(self.path, "rev-parse", "HEAD")
        return commit, self.patch(commit)

    def patch(self, commit):
        return _git(self.path, "diff", self.base_commit, commit)

    def checkout(self, commit):
        _git(self.path, "checkout", "-f", commit, "--", ".")
        _git(self.path, "clean", "-fd")

    def dirty(self):
        return bool(_git(self.path, "status", "--porcelain"))


def patch_sha(patch):
    return hashlib.sha1(patch.encode()).hexdigest()[:10] if patch.strip() else "base"


class SourceEvaluator(OpenEvaluator):
    """OpenEvaluator whose points also name a build: the build is activated
    on the server and ns_custom_server's info.yaml is written for this
    software before the host profile is applied, and the service under test
    is ns_custom_server."""

    def __init__(self, *args, builds=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.config = {**self.config, "dns_service": self.adapter.source_service}
        self.builds = builds if builds is not None else {}

    def _apply(self, overrides, run_dir):
        build_id = overrides["build_id"]
        result = ssh_run(self.server,
                         f"bash ~/{REMOTE_SRC}/build_source.sh activate "
                         f"{self.adapter.name} {shlex.quote(build_id)}", timeout=600)
        if result.returncode != 0:
            raise ServerConfigError(f"could not activate build {build_id}: "
                                    f"{(result.stderr or result.stdout).strip()[-500:]}")
        self.adapter.install_custom_server(self.server)
        super()._apply(overrides, run_dir)

    def ledger_extra(self, overrides):
        build = self.builds.get(overrides.get("build_id"), {})
        return {"build_id": overrides.get("build_id"),
                "patch_sha": build.get("patch_sha"), "commit": build.get("commit"),
                "base_ref": build.get("base_ref")}

    def change_for(self, overrides):
        change = super().change_for(overrides)
        change["build_id"] = overrides.get("build_id")
        return change


class SourceTools(SearchTools):
    """The tools of a source-optimization session (see module docstring)."""

    def __init__(self, evaluator, workspace, host_profile, max_configs, max_builds,
                 transcript, context, base_build, profile_qps=None, dry_run=False):
        super().__init__([], evaluator, context, max_configs, transcript,
                         evaluator.server, dry_run)
        self.workspace = workspace
        self.adapter = evaluator.adapter
        self.host_profile = host_profile   # {"knobs": {...}, "named_options", "host_commands"}
        self.max_builds = max_builds
        self.builds = evaluator.builds
        self.base_build = base_build
        self.profile_qps = profile_qps
        self.system_prompt = (SOURCE_SYSTEM_PROMPT
                              .replace("{server_name}", self.adapter.name)
                              .replace("{base_ref}", workspace.base_ref))
        self._handlers = {
            "profile_server": self.profile_server,
            "build_variant": self.build_variant,
            "checkout": self.checkout,
            "correctness_check": self.correctness_check,
            "measure_variant": self.measure_variant,
            "get_results": self.get_results,
            "get_run_details": self.get_run_details,
            "finish": self.finish,
        }

    # -- builds -------------------------------------------------------------

    def builds_made(self):
        return sum(1 for b in self.builds.values() if b.get("agent"))

    def _overrides(self, build_id, label):
        p = self.host_profile
        change = encode_change(label, p.get("named_options", []), p.get("host_commands", []),
                               [], False, p.get("knobs", {}))
        return {**change, "build_id": build_id}

    def build(self, label, agent=True):
        """Commit and build the working tree. Returns the build record."""
        commit, patch = self.workspace.commit(label)
        sha = patch_sha(patch)
        build_id = f"{self.workspace.base_ref}-{sha}"
        if build_id in self.builds and self.builds[build_id].get("status") == "ok":
            return self.builds[build_id]
        record = {"build_id": build_id, "label": label, "commit": commit,
                  "patch_sha": sha, "base_ref": self.workspace.base_ref,
                  "agent": agent, "patch": patch}
        self.builds[build_id] = record
        server = self.evaluator.server
        src = f"{REMOTE_SRC}/{self.adapter.name}"
        started = time.time()
        try:
            ssh_run(server, f"mkdir -p ~/{src}", timeout=60, check=True)
            scp_to(server, BUILD_SCRIPT, f"~/{REMOTE_SRC}/build_source.sh")
            rsync = subprocess.run(
                ["rsync", "-a", "--exclude", ".git", "-e",
                 "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new",
                 f"{self.workspace.path}/", f"{server}:{src}/"],
                capture_output=True, text=True, timeout=1800)
            if rsync.returncode != 0:
                raise AgentError(f"rsync failed: {rsync.stderr.strip()[-500:]}")
            result = ssh_run(server, f"bash ~/{REMOTE_SRC}/build_source.sh build "
                                     f"{self.adapter.name} ~/{src} {shlex.quote(build_id)} 2>&1",
                             timeout=BUILD_TIMEOUT)
        except subprocess.TimeoutExpired:
            record.update(status="failed", log="build timed out")
            return record
        record["build_s"] = round(time.time() - started, 1)
        if result.returncode != 0:
            record.update(status="failed", log=(result.stdout or "")[-4000:])
        else:
            record.update(status="ok")
        log.info("Build %s: %s in %ss", build_id, record["status"], record["build_s"])
        return record

    # -- tool definitions ---------------------------------------------------

    def definitions(self):
        obj = lambda props, req=(): {"type": "object", "properties": props,  # noqa: E731
                                     "required": list(req), "additionalProperties": False}
        build_id = {"type": "string", "description": "build_id from build_variant "
                                                     f"(the base build is {self.base_build})"}
        return [
            {"name": "profile_server",
             "description": "Hold load on the server running a build and return a perf "
                            "profile: hottest symbols (self time) with call chains, and "
                            "cycles/instructions/cache-miss counters. About a minute.",
             "input_schema": obj({"build_id": build_id,
                                  "load_fraction": {"type": "number", "description":
                                      "Load as a fraction of the base build's max QPS "
                                      "(default 0.8)"}}, ["build_id"])},
            {"name": "build_variant",
             "description": "Commit your current edits and build them on the name "
                            f"server. At most {self.max_builds} builds. Returns the "
                            "build_id, or the compiler output if it failed.",
             "input_schema": obj({"label": {"type": "string", "description":
                                            "Short name for this variant"},
                                  "hypothesis": {"type": "string", "description":
                                            "What the change does and the gain you expect"}},
                                 ["label", "hypothesis"])},
            {"name": "checkout",
             "description": "Reset the working tree to the base source or to an earlier "
                            "build's code (uncommitted edits are lost).",
             "input_schema": obj({"build_id": build_id}, ["build_id"])},
            {"name": "correctness_check",
             "description": "Start a build and compare its answers to a sample of "
                            "queries with the stock server's answers. About a minute.",
             "input_schema": obj({"build_id": build_id}, ["build_id"])},
            {"name": "measure_variant",
             "description": "Measure a build's max sustainable QPS on the frozen host "
                            "profile (10+ minutes, one measurement from the budget even "
                            "if it fails). A build already measured is rejected unless "
                            "repeat=true.",
             "input_schema": obj({"build_id": build_id,
                                  "hypothesis": {"type": "string"},
                                  "repeat": {"type": "boolean"}},
                                 ["build_id", "hypothesis"])},
            {"name": "get_results",
             "description": "Every build and measurement so far, with the budget.",
             "input_schema": obj({})},
            {"name": "get_run_details",
             "description": "Per-level and per-trial results of one measurement, and "
                            "its build's patch.",
             "input_schema": obj({"point_id": {"type": "string"}}, ["point_id"])},
            {"name": "finish",
             "description": "End the session with your report. Call exactly once.",
             "input_schema": obj({"recommended_build_id": {"type": "string", "description":
                                      "The build you recommend, or empty if none beat the base"},
                                  "summary": {"type": "string"}},
                                 ["recommended_build_id", "summary"])},
        ]

    def initial_prompt(self):
        rows = [self._row_view(r) for r in self.evaluator.rows.values()]
        return (
            "<measurement_setup>\n"
            f"{json.dumps(self.context, indent=2, default=str)}\n"
            "</measurement_setup>\n\n"
            "<host_profile>\n"
            f"{json.dumps(self.host_profile.get('knobs', {}), indent=2)}\n"
            "</host_profile>\n\n"
            f"<base_measurements count=\"{len(rows)}\">\n"
            + "\n".join(json.dumps(r) for r in rows) + "\n</base_measurements>\n\n"
            f"Budget: {self.max_configs} measurements ({self.remaining()} left), "
            f"{self.max_builds} builds. The base build is {self.base_build}.\n\n"
            "Start by profiling the base build under load, then plan your variants."
        )

    # -- views --------------------------------------------------------------

    def _row_view(self, row):
        entry = {"point_id": row["point_id"], "build_id": row.get("build_id")}
        for field in ("status", "error", "max_qps_passed", "hit_max_qps_ceiling",
                      "min_qps_fidelity_pct", "validity_ok", "validity_reasons",
                      "repeat_of", "hypothesis"):
            value = row.get(field, "")
            if value not in ("", None):
                entry[field] = _clip(value) if isinstance(value, str) else value
        return entry

    def _build_view(self, build, patch=False):
        view = {k: build.get(k) for k in ("build_id", "label", "status", "build_s")}
        lines = (build.get("patch") or "").splitlines()
        view["patch_lines"] = len(lines)
        if patch:
            view["patch"] = _clip(build.get("patch") or "", MAX_PATCH_TEXT)
        return view

    def _budget(self):
        budget = super()._budget()
        budget["builds_used"] = self.builds_made()
        budget["builds_remaining"] = max(0, self.max_builds - self.builds_made())
        return budget

    # -- tools --------------------------------------------------------------

    def _need_build(self, build_id):
        build = self.builds.get(build_id)
        if build is None:
            raise AgentError(f"no build {build_id!r}; known: {sorted(self.builds)}")
        if build.get("status") != "ok":
            raise AgentError(f"build {build_id} did not build successfully")
        return build

    def build_variant(self, label, hypothesis):
        if self.report is not None:
            raise AgentError("the session has already finished")
        if self.builds_made() >= self.max_builds:
            raise AgentError("the build budget is used up")
        if not self.workspace.dirty():
            raise AgentError("the working tree has no changes since the last build")
        log.info("Agent hypothesis: %s", hypothesis)
        record = self.build(label)
        record["hypothesis"] = hypothesis
        self.transcript.record("build", **{k: v for k, v in record.items() if k != "patch"})
        if record["status"] != "ok":
            return {"build_id": record["build_id"], "status": "failed",
                    "log_tail": record.get("log", "")[-3000:], **self._budget()}
        return {**self._build_view(record), **self._budget()}

    def checkout(self, build_id):
        build = self.builds.get(build_id)
        if build is None:
            raise AgentError(f"no build {build_id!r}")
        self.workspace.checkout(build["commit"])
        return f"Working tree reset to {build_id} ({build['label']})."

    def _with_service(self, build_id, label, action):
        """Apply build + host profile, start the service, run ``action``,
        then stop and restore. Returns action's result."""
        ev = self.evaluator
        overrides = self._overrides(build_id, label)
        run_dir = os.path.join(ev.output_dir, "grid_search", "probes", f"{label}-{build_id}")
        try:
            ev._apply(overrides, run_dir)
            config = ev.config
            stop_dns_service(config)
            start_dns_service(config, config["dns_service"])
            wait_for_dns_ready(config, timeout=300)
            return action(config)
        except Exception as e:  # noqa: BLE001 - reported to the agent
            raise AgentError(f"{label} failed: {e}") from e
        finally:
            try:
                stop_dns_service(ev.config, ev.config["dns_service"])
            finally:
                ev._restore(overrides, run_dir)

    def correctness_check(self, build_id):
        self._need_build(build_id)
        validator = self.evaluator.validator
        if validator is None:
            raise AgentError("no validator configured for this session")

        def check(config):
            hooks = validator.begin(config["dns_service"])
            hooks.on_ready(config, False)
            hooks.before_stop(config, False)
            return hooks.result()

        result = self._with_service(build_id, "correctness", check)
        return {"build_id": build_id, "ok": result["ok"], "reasons": result["reasons"],
                "pre": result["pre"], "identity": result["identity"],
                "examples": result.get("examples", [])}

    def _base_qps(self):
        values = [row_qps(r) for r in self.evaluator.rows.values()
                  if r.get("build_id") == self.base_build]
        values = [v for v in values if v]
        return max(values) if values else self.profile_qps

    def profile_server(self, build_id, load_fraction=0.8):
        self._need_build(build_id)
        base = self._base_qps()
        if not base:
            raise AgentError("no base measurement yet to size the load from")
        if not 0.05 <= load_fraction <= 1.5:
            raise AgentError("load_fraction must be between 0.05 and 1.5")
        qps = int(base * load_fraction)
        process = self.adapter.process

        def profile(config):
            tool = TOOLS[config["tool"]]()
            clients = get_clients(config)
            config = {**config, "runtime": PROFILE_SECONDS}
            commands = {host: tool.build_command(
                            {**config, "client_interface": client_interface_for(config, host)},
                            share)
                        for host, share in zip(clients, split_qps(qps, len(clients)))}
            load = threading.Thread(target=ssh_run_many, args=(commands,),
                                    kwargs={"timeout": PROFILE_SECONDS + 120}, daemon=True)
            load.start()
            time.sleep(8)
            perf = ("P=$(ls /usr/lib/linux-tools/*/perf 2>/dev/null | tail -1); "
                    "[ -x \"$P\" ] || P=perf; "
                    "command -v $P >/dev/null 2>&1 || sudo apt-get install -y -q "
                    "linux-tools-common linux-tools-$(uname -r) >/dev/null 2>&1; "
                    f"pid=$(pidof {process} | cut -d' ' -f1); "
                    "sudo $P stat -e cycles,instructions,cache-misses,context-switches,"
                    "cpu-migrations -p $pid -- sleep 5 2>&1 | grep -vE '^\\s*$'; "
                    "sudo $P record -q -F 499 -g -p $pid -o /tmp/campaign.perf -- sleep 15 "
                    ">/dev/null 2>&1; "
                    "echo '=== self time by symbol'; "
                    "sudo $P report -i /tmp/campaign.perf --stdio --no-children "
                    "--percent-limit 0.7 --sort dso,symbol -g graph,0.5,caller "
                    "2>/dev/null | grep -v '^#' | grep -v '^$' | head -220")
            result = ssh_run(self.evaluator.server, perf, timeout=300)
            load.join(timeout=PROFILE_SECONDS + 150)
            reset_clients(config, tool)
            return result.stdout or result.stderr

        output = self._with_service(build_id, "profile", profile)
        self.transcript.record("profile", build_id=build_id, qps=qps)
        return f"[perf profile of {build_id} at {qps} QPS]\n{_clip(output, 11000)}"

    def measure_variant(self, build_id, hypothesis, repeat=False):
        if self.report is not None:
            raise AgentError("the session has already finished")
        if self.stop_reason:
            raise AgentError(f"no more measurements: {self.stop_reason}. Call finish.")
        if self.remaining() == 0:
            raise AgentError("the measurement budget is used up; call finish")
        build = self._need_build(build_id)
        return self.measure(build, hypothesis, repeat)

    def measure(self, build, hypothesis, repeat=False, label=None):
        build_id = build["build_id"]
        base_id = f"{label or build['label']}__{build_id}"
        base_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", base_id)
        pid = base_id
        if base_id in self.evaluator.rows:
            if not repeat:
                raise AgentError(f"{build_id} was already measured; pass repeat=true")
            n = 2
            while f"{base_id}__rep{n}" in self.evaluator.rows:
                n += 1
            pid = f"{base_id}__rep{n}"
        extra = {"iteration": self.used() + 1, "hypothesis": hypothesis,
                 "build_id": build_id}
        if pid != base_id:
            extra["repeat_of"] = base_id
        row = self.evaluator.evaluate(self._overrides(build_id, build["label"]),
                                      f"[source {self.used() + 1}/{self.max_configs}]",
                                      extra, pid=pid)
        return {**self._row_view(row), "levels": self._levels(row), **self._budget()}

    def get_results(self, sort_by="order"):
        return {**self._budget(),
                "builds": [self._build_view(b) for b in self.builds.values()],
                "measurements": [self._row_view(r) for r in self.evaluator.rows.values()]}

    def get_run_details(self, point_id):
        row = self.evaluator.rows.get(point_id)
        if row is None:
            raise AgentError(f"no measurement {point_id!r}; see get_results")
        levels, trials = self._run_files(row)
        build = self.builds.get(row.get("build_id"), {})
        return {**self._row_view(row), "levels": levels,
                "trials": [{k: t.get(k) for k in ("target_qps", "trial", "answer_rate_pct",
                                                  "qps_fidelity_pct", "probe_mismatched")}
                           for t in trials],
                "build": self._build_view(build, patch=True) if build else None}

    def finish(self, recommended_build_id, summary):
        if self.report is not None:
            return "already finished"
        if recommended_build_id and recommended_build_id not in self.builds:
            raise AgentError(f"no build {recommended_build_id!r}")
        build = self.builds.get(recommended_build_id) or {}
        scores = [row_qps(r) for r in self.evaluator.rows.values()
                  if r.get("build_id") == recommended_build_id]
        scores = [s for s in scores if s]
        self.report = {"recommended_build_id": recommended_build_id or None,
                       "recommended_patch": build.get("patch"),
                       "recommended_max_qps_passed": max(scores) if scores else None,
                       "base_build": self.base_build, "summary": summary,
                       **self._budget()}
        log.info("Agent finished. Recommended build: %s", recommended_build_id or "(none)")
        return "Session finished. End your turn now."

    def export_patches(self, out_dir):
        """Write every built variant's patch to out_dir/<build_id>.patch."""
        os.makedirs(out_dir, exist_ok=True)
        for build in self.builds.values():
            if build.get("patch"):
                with open(os.path.join(out_dir, f"{build['build_id']}.patch"), "w") as f:
                    f.write(build["patch"])
        with open(os.path.join(out_dir, "builds.json"), "w") as f:
            json.dump(list(self.builds.values()), f, indent=1, default=str)


def frozen_profile(catalog, knob_selection, software, facts):
    """Render the host profile a source session measures on. Knobs that need
    a reboot are left out (a reboot per measurement would dominate)."""
    selection = {k: v for k, v in (knob_selection or {}).items()
                 if k in catalog.knobs and not catalog.knobs[k].get("reboot")
                 and catalog.applicable(k, software)}
    rendered = catalog.render(selection, software, facts)
    return {"knobs": selection, "named_options": rendered["named_options"],
            "host_commands": rendered["host_commands"]}


__all__ = ["SourceEvaluator", "SourceTools", "SourceWorkspace", "frozen_profile",
           "patch_sha", "decode_change"]
