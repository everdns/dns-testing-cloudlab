"""Validity gate for measurements: correct answers, untouched inputs, known binary.

max_sustainable_qps.py scores a level by the share of queries answered with
NOERROR. That alone cannot tell a fast server from one that answers wrongly,
was handed a smaller zone, or is a different binary than the row says. The
Validator adds those checks around every measurement (search.Evaluator calls
it; run_max_sustainable_qps runs its hooks while the service is up):

  reference   Once per server software, the stock (package) server's answers
              to an even sample of the query input are captured from the
              tester and checked against the zone file's A/AAAA records. The
              zone file and query-input hashes are recorded with it.
  pre/post    With the configuration under test running, the sample is sent
              again before the first trial and after the last; every answer
              must match the reference, and an idle server must answer
              nearly all of them.
  under load  During every trial, dns_probe.py runs on the name server against
              127.0.0.1 at a low rate. Unanswered probes are expected near
              the limit; wrong answers are not, at any rate.
  integrity   The zone file on the server and the query input on every client
              must hash as they did at reference time.
  identity    The binary, its hash, version and build id are recorded.

A failed check marks the row invalid (validity_ok=False). search.row_qps
ignores invalid rows, so no search or agent builds on them, and the ledger
keeps the reasons.
"""
import json
import logging
import os
import shlex
import threading

import dns_probe
from max_sustainable_qps import (get_clients, scp_to, ssh_run, start_dns_service,
                                 stop_dns_service, wait_for_dns_ready)

log = logging.getLogger("validity")

REMOTE_DIR = "/tmp/campaign_probe"
PROBE_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dns_probe.py")
DEFAULT_STATE_DIR = os.path.expanduser("~/campaign/validity")
# An idle server must answer at least this share of the probe queries.
MIN_IDLE_ANSWERED = 0.99


class ValidityError(Exception):
    pass


def _sha(server, path):
    result = ssh_run(server, f"sha256sum {shlex.quote(path)} 2>/dev/null | cut -c1-16",
                     timeout=60)
    return (result.stdout or "").strip() or None


class Validator:
    """Correctness and integrity checks for one server software on one
    environment. ``state_dir`` keeps the reference and the query samples."""

    def __init__(self, config, adapter, state_dir=DEFAULT_STATE_DIR,
                 sample_size=2000, load_sample_size=300, load_rate=50):
        self.config = config
        self.adapter = adapter
        self.server = config["hosts"]["server"]
        self.clients = get_clients(config)
        self.input_file = config["input_file"]
        self.state_dir = state_dir
        self.sample_size = sample_size
        self.load_sample_size = load_sample_size
        self.load_rate = load_rate
        self.reference_path = os.path.join(state_dir, f"reference_{adapter.name}.json")
        self.reference = self._load_reference()
        self._pushed = False

    # -- reference ----------------------------------------------------------

    def _load_reference(self):
        if not os.path.exists(self.reference_path):
            return None
        with open(self.reference_path) as f:
            return json.load(f)

    def _sample(self, size):
        return dns_probe.read_queries(self.input_file, size)

    def integrity(self):
        """Current hashes of the zone file and every client's query input."""
        return {"zone_sha": _sha(self.server, self.adapter.zone_path),
                "input_sha": {c: _sha(c, self.input_file) for c in self.clients}}

    def capture_reference(self, service=None):
        """Record the stock server's answers to the sample. Starts and stops
        ``service`` (default: the package service). Raises ValidityError if
        the answers disagree with the zone file."""
        service = service or self.adapter.package_service
        config = {**self.config, "dns_service": service}
        log.info("Capturing the %s reference answers from %s", self.adapter.name, service)
        stop_dns_service(config)
        start_dns_service(config, service)
        try:
            wait_for_dns_ready(config, timeout=300)
            responses, counts = dns_probe.query_many(self.server, self._sample(self.sample_size))
            zone_text = ssh_run(self.server, f"sudo cat {self.adapter.zone_path}",
                                timeout=120).stdout or ""
            identity = self.adapter.identity(self.server, service)
        finally:
            stop_dns_service(config, service)
        if counts["answered"] < MIN_IDLE_ANSWERED * counts["sent"]:
            raise ValidityError(f"the stock server answered only {counts['answered']}"
                                f"/{counts['sent']} reference queries")
        wrong = dns_probe.check_against_zone(responses, dns_probe.zone_expectations(zone_text))
        if wrong:
            raise ValidityError(f"the stock server's answers disagree with the zone file "
                                f"for {len(wrong)} queries, e.g. {wrong[:3]}")
        self.reference = {"software": self.adapter.name, "service": service,
                          "identity": identity, "integrity": self.integrity(),
                          "counts": counts, "responses": responses}
        os.makedirs(self.state_dir, exist_ok=True)
        with open(self.reference_path, "w") as f:
            json.dump(self.reference, f)
        self._pushed = False
        log.info("Reference captured: %d answers, zone %s", counts["answered"],
                 self.reference["integrity"]["zone_sha"])
        return self.reference

    def ensure_reference(self):
        return self.reference or self.capture_reference()

    def _push(self):
        """Copy the probe, the load sample and the reference to the server."""
        if self._pushed:
            return
        os.makedirs(self.state_dir, exist_ok=True)
        sample = os.path.join(self.state_dir, "load_sample.txt")
        with open(sample, "w") as f:
            for name, qtype in self._sample(self.load_sample_size):
                f.write(f"{name} {qtype}\n")
        ssh_run(self.server, f"mkdir -p {REMOTE_DIR}", timeout=60, check=True)
        scp_to(self.server, PROBE_SCRIPT, f"{REMOTE_DIR}/dns_probe.py")
        scp_to(self.server, sample, f"{REMOTE_DIR}/load_sample.txt")
        if self.reference:
            scp_to(self.server, self.reference_path, f"{REMOTE_DIR}/reference.json")
        self._pushed = True

    # -- per measurement ----------------------------------------------------

    def begin(self, service):
        """Hooks for one measurement of ``service``."""
        return MeasurementHooks(self, service)


class MeasurementHooks:
    """Collects the checks of one measurement (see run_max_sustainable_qps)."""

    def __init__(self, validator, service):
        self.v = validator
        self.service = service
        self.simulated = False
        self.pre = self.post = None
        self.integrity = self.identity = None
        self.load = []
        self.errors = []

    def _probe(self):
        reference = (self.v.reference or {}).get("responses") or {}
        responses, counts = dns_probe.query_many(self.v.server,
                                                 self.v._sample(self.v.sample_size))
        summary = dns_probe.compare(reference, responses) if reference else {
            "checked": counts["sent"], "answered": counts["answered"],
            "unanswered": counts["unanswered"], "mismatched": 0, "examples": [],
            "no_reference": True}
        if not reference:
            # Without a reference, at least the zone's A/AAAA data must hold.
            zone = ssh_run(self.v.server, f"sudo cat {self.v.adapter.zone_path}",
                           timeout=120).stdout or ""
            wrong = dns_probe.check_against_zone(responses, dns_probe.zone_expectations(zone))
            summary["mismatched"] = len(wrong)
            summary["examples"] = wrong[:5]
        return summary

    def on_ready(self, config, simulating):
        if simulating:
            self.simulated = True
            return
        try:
            self.integrity = self.v.integrity()
            self.identity = self.v.adapter.identity(self.v.server, self.service)
            self.v._push()
            self.pre = self._probe()
        except Exception as e:  # noqa: BLE001 - recorded as a failed check
            self.errors.append(f"pre-check failed to run: {e}")
            raise

    def trial_start(self, config, qps, trial):
        if not self.v.reference:
            return None
        duration = config.get("runtime") or config["max_sustainable_qps"]["trial_duration"]
        command = (f"cd {REMOTE_DIR} && timeout {int(duration) + 30} python3 dns_probe.py query "
                   f"--server 127.0.0.1 --names load_sample.txt --rate {self.v.load_rate} "
                   f"--duration {duration} --timeout 0.5 --out obs.json >/dev/null && "
                   f"python3 dns_probe.py compare --reference reference.json --observed obs.json")
        holder = {"qps": qps}

        def run():
            try:
                holder["result"] = ssh_run(self.v.server, command, timeout=int(duration) + 90)
            except Exception as e:  # noqa: BLE001 - reported in trial_end
                holder["error"] = str(e)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread, holder

    def trial_end(self, handle):
        if handle is None:
            return {}
        thread, holder = handle
        thread.join(timeout=300)
        result = holder.get("result")
        try:
            summary = json.loads(result.stdout) if result is not None else None
        except (json.JSONDecodeError, TypeError):
            summary = None
        if summary is None:
            self.errors.append(f"load probe at {holder['qps']} QPS produced no result: "
                               f"{holder.get('error') or (result.stderr if result else '')}"[:300])
            return {"probe_ok": False}
        summary["qps"] = holder["qps"]
        self.load.append(summary)
        return {"probe_checked": summary["checked"], "probe_answered": summary["answered"],
                "probe_mismatched": summary["mismatched"]}

    def before_stop(self, config, simulating):
        if simulating:
            return
        try:
            self.post = self._probe()
        except Exception as e:  # noqa: BLE001
            self.errors.append(f"post-check failed to run: {e}")
            raise

    def result(self):
        """The validity record for this measurement."""
        if self.simulated:
            return {"ok": True, "reasons": [], "simulated": True}
        reasons = list(self.errors)
        for name, check in (("pre-check", self.pre), ("post-check", self.post)):
            if check is None:
                if not any(r.startswith(name) for r in reasons):
                    reasons.append(f"{name} did not run")
                continue
            if check["mismatched"]:
                reasons.append(f"{name}: {check['mismatched']}/{check['answered']} "
                               f"answers differ from the reference")
            if check["checked"] and check["answered"] < MIN_IDLE_ANSWERED * check["checked"]:
                reasons.append(f"{name}: the idle server answered only "
                               f"{check['answered']}/{check['checked']} probe queries")
        load = {"trials": len(self.load),
                "checked": sum(s["checked"] for s in self.load),
                "answered": sum(s["answered"] for s in self.load),
                "mismatched": sum(s["mismatched"] for s in self.load)}
        if load["mismatched"]:
            worst = max(self.load, key=lambda s: s["mismatched"])
            reasons.append(f"under load: {load['mismatched']} wrong answers "
                           f"(most at {worst['qps']} QPS)")
        expected = (self.v.reference or {}).get("integrity") or {}
        if self.integrity and expected:
            if expected.get("zone_sha") and self.integrity["zone_sha"] != expected["zone_sha"]:
                reasons.append(f"integrity: zone file {self.v.adapter.zone_path} changed "
                               f"({expected['zone_sha']} -> {self.integrity['zone_sha']})")
            for client, sha in (expected.get("input_sha") or {}).items():
                now = self.integrity["input_sha"].get(client)
                if sha and now != sha:
                    reasons.append(f"integrity: query input on {client} changed")
        examples = [e for c in (self.pre, self.post) if c for e in c.get("examples", [])][:3]
        return {"ok": not reasons, "reasons": reasons,
                "pre": _counts(self.pre), "post": _counts(self.post), "load": load,
                "integrity": self.integrity, "identity": self.identity,
                "reference": bool(self.v.reference), "examples": examples}


def _counts(check):
    if check is None:
        return None
    return {k: check[k] for k in ("checked", "answered", "mismatched")}
