"""Tests for the campaign methodology pieces that run without CloudLab hosts.

    cd optimization && python3 -m unittest test_campaign -v
"""
import itertools
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "campaign"))

import dns_probe  # noqa: E402
import knobs  # noqa: E402
import ledger  # noqa: E402
import stats  # noqa: E402
import validity  # noqa: E402
from search import row_qps  # noqa: E402
from servers import ServerConfigError, get_adapter  # noqa: E402


def name_bytes(name):
    return dns_probe.encode_name(name)


def response(query, answers, flags=0x8400, authority=()):
    """A response to ``query`` (bytes) with RRs given as (owner_bytes, type,
    ttl, rdata_bytes); owner b"\\xc0\\x0c" points at the question name."""
    qid = struct.unpack("!H", query[:2])[0]
    end = 12
    while query[end] != 0:
        end += 1 + query[end]
    question = query[12:end + 5]
    out = struct.pack("!HHHHHH", qid, flags, 1, len(answers), len(authority), 0) + question
    for owner, rtype, ttl, rdata in list(answers) + list(authority):
        out += owner + struct.pack("!HHIH", rtype, 1, ttl, len(rdata)) + rdata
    return out


class FakeServer:
    """UDP server answering every query with handler(query) -> bytes or None."""

    def __init__(self, handler):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.handler = handler
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        while True:
            try:
                data, addr = self.sock.recvfrom(4096)
            except OSError:
                return
            reply = self.handler(data)
            if reply:
                self.sock.sendto(reply, addr)

    def close(self):
        self.sock.close()


class DnsProbeTest(unittest.TestCase):
    def handler(self, query):
        qname = dns_probe.read_name(query, 12)[0]
        if qname.startswith("a."):
            return response(query, [(b"\xc0\x0c", 1, 3600, bytes([10, 0, 0, 1]))])
        if qname.startswith("ns."):
            # NS rdata uses a compression pointer back to the question name.
            return response(query, [(b"\xc0\x0c", 2, 3600, b"\x03ns1\xc0\x0f")])
        if qname.startswith("txt."):
            return response(query, [(b"\xc0\x0c", 16, 60, b"\x05hello")])
        return None  # drop: unanswered

    def setUp(self):
        self.server = FakeServer(self.handler)

    def tearDown(self):
        self.server.close()

    def test_query_parse_and_compare(self):
        queries = [("a.example.", "A"), ("ns.example.", "NS"), ("txt.example.", "TXT"),
                   ("lost.example.", "A")]
        results, counts = dns_probe.query_many("127.0.0.1", queries, timeout=0.3,
                                               port=self.server.port)
        self.assertEqual(counts["answered"], 3)
        self.assertEqual(results["a.example. A"]["answer"], ["a.example. 3600 A 10.0.0.1"])
        self.assertEqual(results["ns.example. NS"]["answer"],
                         ["ns.example. 3600 NS ns1.example."])
        self.assertEqual(results["txt.example. TXT"]["answer"], ['txt.example. 60 TXT "hello"'])
        self.assertTrue(results["a.example. A"]["aa"])
        self.assertIsNone(results["lost.example. A"])

        reference = json.loads(json.dumps(results))
        self.assertEqual(dns_probe.compare(reference, results)["mismatched"], 0)
        reference["a.example. A"]["answer"] = ["a.example. 3600 A 10.0.0.2"]
        reference["lost.example. A"] = reference["txt.example. TXT"]
        summary = dns_probe.compare(reference, results)
        self.assertEqual(summary["mismatched"], 1)
        self.assertEqual(summary["unanswered"], 1)

    def test_zone_expectations(self):
        zone = ("$TTL 3600\n010-000-000-001.workbench.lan.  IN  A  10.0.0.1\n"
                "010-000-000-002.workbench.lan.  IN  AAAA  2001:db8::a00:2\n"
                "010-000-000-003.workbench.lan.  IN  TXT  \"x\"\n")
        expected = dns_probe.zone_expectations(zone)
        self.assertEqual(set(expected), {"010-000-000-001.workbench.lan. A",
                                         "010-000-000-002.workbench.lan. AAAA"})
        good = {"010-000-000-001.workbench.lan. A": {
            "rcode": "NOERROR", "answer": ["010-000-000-001.workbench.lan. 3600 A 10.0.0.1"]}}
        bad = {"010-000-000-001.workbench.lan. A": {
            "rcode": "NOERROR", "answer": []}}
        self.assertEqual(dns_probe.check_against_zone(good, expected), [])
        self.assertEqual(dns_probe.check_against_zone(bad, expected),
                         ["010-000-000-001.workbench.lan. A"])

    def test_reads_repo_zone_and_input(self):
        repo = os.path.dirname(HERE)
        zone = os.path.join(repo, "zone_file_defaults", "db.workbench.lan")
        with open(zone) as f:
            self.assertGreater(len(dns_probe.zone_expectations(f.read())), 1000)


class KnobsTest(unittest.TestCase):
    def setUp(self):
        self.catalog = knobs.load()
        self.facts = {"iface": "eth1", "cpus": 8}

    def test_every_knob_renders_valid_shell(self):
        for name in self.catalog.knobs:
            for software in ("bind", "knot", "nsd"):
                if not self.catalog.applicable(name, software):
                    continue
                rendered = self.catalog.render({name: self.catalog.typical_value(name)},
                                               software, self.facts)
                for command in rendered["host_commands"]:
                    check = subprocess.run(["bash", "-n", "-c", command],
                                           capture_output=True, text=True)
                    self.assertEqual(check.returncode, 0, f"{name}/{software}: {check.stderr}")

    def test_bounds_and_baseline(self):
        _, errors = self.catalog.validate({"rmem_max": 10})
        self.assertTrue(errors)
        selection, errors = self.catalog.validate({"rmem_max": "default", "gro_off": False})
        self.assertEqual((selection, errors), ({}, []))
        empty = self.catalog.render({}, "bind", self.facts)
        self.assertEqual(empty["named_options"] + empty["host_commands"], [])

    def test_software_specific_and_aliases(self):
        r = self.catalog.render({"workers": "cpus", "udp_rcvbuf": 16777216}, "nsd", self.facts)
        self.assertIn("server-count: 8", r["named_options"])
        with self.assertRaises(knobs.KnobError):
            self.catalog.render({"message_compression_off": True}, "knot", self.facts)
        self.assertTrue(self.catalog.render({"mitigations_off": True}, "bind",
                                            self.facts)["reboot_after_apply"])
        self.assertEqual(self.catalog.from_parameters(
            {"rmem": "default", "udp-receive-buffer": 4194304, "udp-conntrack": "notrack"}),
            {"udp_rcvbuf": 4194304, "conntrack": "notrack"})


class LedgerTest(unittest.TestCase):
    def test_round_trip_and_usable(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ledger.jsonl")
            led = ledger.Ledger(path, env="r320", software="bind", phase="attribute")
            row = {"point_id": "p", "status": "ok", "max_qps_passed": "300000",
                   "min_qps_fidelity_pct": "99.5", "hit_max_qps_ceiling": "False"}
            led.record(row, knobs={"rps": "all-cpus"}, validity={"ok": True, "reasons": []})
            led.record({**row, "point_id": "q", "min_qps_fidelity_pct": "90"})
            led.record({**row, "point_id": "r"}, validity={"ok": False, "reasons": ["x"]})
            with open(path, "a") as f:
                f.write('{"truncated": \n')
            records = ledger.read(path)
            self.assertEqual(len(records), 3)
            self.assertEqual(records[0]["max_qps_passed"], 300000)
            self.assertEqual([ledger.usable(r) for r in records], [True, False, False])
            self.assertTrue(records[1]["client_limited"])

    def test_invalid_rows_do_not_score(self):
        self.assertEqual(row_qps({"status": "ok", "max_qps_passed": "5"}), 5)
        self.assertIsNone(row_qps({"status": "ok", "max_qps_passed": "5",
                                   "validity_ok": False}))
        self.assertIsNone(row_qps({"status": "ok", "max_qps_passed": "5",
                                   "validity_ok": "False"}))


class StatsTest(unittest.TestCase):
    def test_resolution_iv(self):
        factors = list("abcdefgh")
        design = stats.fractional_factorial(factors)
        self.assertEqual(len(design), 16)
        columns = {f: [row[f] for row in design] for f in factors}
        for f in factors:
            self.assertEqual(sum(columns[f]), 0)
        # Main effects are orthogonal to every two-factor interaction.
        for f in factors:
            for a, b in itertools.combinations(factors, 2):
                inter = [x * y for x, y in zip(columns[a], columns[b])]
                self.assertEqual(sum(c * i for c, i in zip(columns[f], inter)), 0)
        self.assertEqual(len(stats.fractional_factorial(list("abc"))), 8)

    def test_main_effects_recover_planted(self):
        factors = list("abcde")
        design = stats.fractional_factorial(factors)
        planted = {"a": 10, "b": 0, "c": -4, "d": 0, "e": 2}
        responses = [[100 + sum(planted[f] / 2 * row[f] for f in factors)] for row in design]
        effects = stats.main_effects(design, responses)
        for f in factors:
            self.assertAlmostEqual(effects[f], planted[f])

    def test_replicate_rule_and_effect(self):
        self.assertTrue(stats.needs_replicates(98, 100, 2, have=1))
        self.assertFalse(stats.needs_replicates(90, 100, 2, have=1))
        self.assertFalse(stats.needs_replicates(101, 100, 2, have=3))
        e = stats.effect([110, 111, 109], [100, 101, 99])
        self.assertAlmostEqual(e["effect_pct"], 10.0)
        self.assertTrue(e["significant"])
        self.assertAlmostEqual(stats.noise_sigma({"a": [1, 3], "b": [5]}), 2 ** 0.5)


class AdapterTest(unittest.TestCase):
    def test_render_and_protect(self):
        nsd = get_adapter("ns_nsd")
        text = nsd.render(nsd.load_base(), ["server-count: 8", "reuseport: no"])
        self.assertIn("\tserver-count: 8", text)
        self.assertNotIn("server-count: 40", text)
        with self.assertRaises(ServerConfigError):
            nsd.render(nsd.load_base(), ["port: 5353"])
        knot = get_adapter("knot")
        self.assertIn("    udp-workers: 4", knot.render(knot.load_base(), ["server.udp-workers: 4"]))
        bind = get_adapter("bind")
        self.assertEqual(bind.release_tag("BIND 9.20.15-1+ubuntu"), "v9.20.15")
        self.assertEqual(nsd.release_tag("NSD version 4.14.1"), "NSD_4_14_1_REL")
        with self.assertRaises(ServerConfigError):
            bind.render(bind.load_base(), ["listen-on { any; };"])

    def test_custom_server_info(self):
        def keys(text):
            return [line.split(":", 1)[0] for line in text.splitlines()
                    if line.strip() and not line.startswith("#")]
        repo_info = os.path.join(os.path.dirname(HERE), "ns_software", "custom_server",
                                 "info.yaml")
        with open(repo_info) as f:
            expected = keys(f.read())
        for software in ("bind", "knot", "nsd"):
            info = get_adapter(software).custom_server_info()
            self.assertEqual(keys(info), expected, software)
            self.assertIn(f"prefix: /opt/dnsbuild/{software}/prefix", info)
        with self.assertRaises(ServerConfigError):
            get_adapter("ns_custom_server")


class ValidityResultTest(unittest.TestCase):
    def hooks(self, reference=True):
        v = validity.Validator.__new__(validity.Validator)
        v.adapter = get_adapter("bind")
        v.reference = {"integrity": {"zone_sha": "aaa", "input_sha": {"c": "111"}}} \
            if reference else None
        return validity.MeasurementHooks(v, "ns_bind")

    def test_verdicts(self):
        ok = {"checked": 100, "answered": 100, "mismatched": 0, "examples": []}
        h = self.hooks()
        h.pre = h.post = ok
        h.integrity = {"zone_sha": "aaa", "input_sha": {"c": "111"}}
        self.assertTrue(h.result()["ok"])

        h.load = [{"qps": 500000, "checked": 50, "answered": 40, "mismatched": 2}]
        self.assertIn("under load", h.result()["reasons"][0])

        h = self.hooks()
        h.pre, h.post = ok, {**ok, "answered": 50}
        h.integrity = {"zone_sha": "bbb", "input_sha": {"c": "111"}}
        reasons = h.result()["reasons"]
        self.assertTrue(any("zone file" in r for r in reasons))
        self.assertTrue(any("idle server answered only" in r for r in reasons))

        h = self.hooks()
        self.assertFalse(h.result()["ok"])  # checks that never ran do not pass


class KnobSpaceTest(unittest.TestCase):
    def setUp(self):
        import knob_space
        self.ks = knob_space
        self.catalog = knobs.load()
        self.facts = {"iface": "eth1", "cpus": 8}
        self.good = [
            {"knob": "rmem_max", "values": [16777216, "default", 67108864],
             "min": 4194304, "max": 134217728, "rationale": "helped in explore"},
            {"knob": "gro_off", "values": [True, False], "rationale": "r"},
            {"knob": "workers", "values": ["cpus", 4], "min": 2, "max": 8, "rationale": "r"},
            {"knob": "netdev_budget", "values": ["600/4000", "default"],
             "min": [300, 2000], "max": [3000, 16000], "rationale": "r"},
        ]

    def build(self, entries, budget=24):
        return self.ks.build_space(self.catalog, "bind", self.facts, entries, 8, budget)

    def test_accepts_and_normalizes(self):
        params, errors = self.build(self.good)
        self.assertEqual(errors, [])
        by_name = {p["name"]: p for p in params}
        self.assertEqual(by_name["rmem_max"]["values"], [16777216, "default", 67108864])
        self.assertEqual(by_name["gro_off"]["values"], ["true", "false"])
        self.assertEqual(by_name["workers"]["values"], [8, 4])
        self.assertEqual(by_name["gro_off"]["agent"]["choices"], ["false", "true"])
        self.assertIn("helped in explore", by_name["rmem_max"]["agent"]["description"])

    def test_rejects(self):
        cases = [
            [{"knob": "mitigations_off", "values": [True, False], "rationale": "r"}],
            [{"knob": "rmem_max", "values": [16777216, "default"], "min": 10,
              "max": 67108864, "rationale": "r"}],                       # below catalog min
            [{"knob": "workers", "values": [16, 4], "min": 2, "max": 8,
              "rationale": "r"}],                                         # outside planned max
            [{"knob": "gro_off", "values": [True], "rationale": "r"}],     # one value
            [{"knob": "rx_ring", "values": ["max", "default"], "rationale": "r"}],  # 1 param
        ]
        for entries in cases:
            _, errors = self.build(entries)
            self.assertTrue(errors, entries)
        _, errors = self.build(self.good, budget=4)                       # pass > budget
        self.assertTrue(any("budget" in e for e in errors))

    def test_bounded_validation(self):
        from agent_tools import build_search_space, validate_configuration
        params, _ = self.build(self.good)
        space = build_search_space(params)
        check = self.ks.value_check(self.catalog, "bind", self.facts)
        ok, errors = validate_configuration(
            space, {"rmem_max": 33554432, "gro_off": "true", "workers": 6,
                    "netdev_budget": "default"}, check)
        self.assertEqual(errors, [])
        self.assertEqual(self.catalog.validate(ok)[0],
                         {"rmem_max": 33554432, "gro_off": True, "workers": 6})
        _, errors = validate_configuration(
            space, {"rmem_max": 268435456, "gro_off": "true", "workers": 6,
                    "netdev_budget": "default"}, check)
        self.assertTrue(errors)


class SimulatedCampaignTest(unittest.TestCase):
    def test_plan_space_grid_and_bounded_tools(self):
        import yaml
        import campaign as campaign_mod
        import phases
        from agent_tools import SearchTools, Transcript, build_search_space
        with tempfile.TemporaryDirectory() as d:
            cfg = {"env": "simtest", "state_dir": d,
                   "search_config": os.path.join(HERE, "agent_search.yaml"),
                   "grid_config": os.path.join(HERE, "grid_search.yaml"),
                   "cycle": ["explore", "plan_space", "grid"],
                   "explore": {"max_configs": 4},
                   "grid": {"max_measurements": 12, "max_passes": 1}}
            path = os.path.join(d, "campaign.yaml")
            with open(path, "w") as f:
                yaml.safe_dump(cfg, f)
            ctx = campaign_mod.Campaign(path, simulate=True)
            self.assertEqual(ctx.run(max_experiments=4), 0)
            steps = [(h["step"], h["status"]) for h in ctx.state["history"]]
            self.assertEqual(steps, [("calibrate", "ok"), ("explore", "ok"),
                                     ("plan_space", "ok"), ("grid", "ok")])
            space = ctx.search_space("bind")
            self.assertTrue(space["parameters"])
            grid_rows = [r for r in ledger.read(ctx.ledger_path) if r["phase"] == "grid"]
            self.assertTrue(grid_rows)
            self.assertTrue(all(ledger.usable(r) for r in grid_rows))

            # bounded: SearchTools over the planned space, measuring through KnobPoints
            exp = campaign_mod.Experiment(ctx, "bounded", "bind")
            evaluator = ctx.open_evaluator(exp, "agent_results")
            points = phases.KnobPoints(ctx, evaluator, "bind", budget=2)
            tools = SearchTools(build_search_space(space["parameters"]), points, {}, 2,
                                Transcript(os.path.join(exp.dir, "t.jsonl")), ctx.server,
                                check=__import__("knob_space").value_check(
                                    ctx.catalog, "bind", ctx.facts))
            start = {p["name"]: p["values"][0] for p in space["parameters"]}
            text, is_error = tools.call("measure_configuration",
                                        {"configuration": start, "hypothesis": "h"})
            self.assertFalse(is_error, text)
            self.assertEqual(json.loads(text)["configuration"], start)
            row = next(iter(evaluator.rows.values()))
            self.assertEqual(json.loads(row["knobs"]), ctx.catalog.validate(start)[0])

    def test_calibrate_explore_attribute(self):
        import yaml
        import campaign as campaign_mod
        with tempfile.TemporaryDirectory() as d:
            cfg = {"env": "simtest", "state_dir": d,
                   "search_config": os.path.join(HERE, "agent_search.yaml"),
                   "cycle": ["explore", "attribute"],
                   "explore": {"max_configs": 4}, "attribute": {"replicates": 2, "max_knobs": 3}}
            path = os.path.join(d, "campaign.yaml")
            with open(path, "w") as f:
                yaml.safe_dump(cfg, f)
            ctx = campaign_mod.Campaign(path, simulate=True)
            self.assertEqual(ctx.run(max_experiments=3), 0)
            steps = [h["step"] for h in ctx.state["history"]]
            self.assertEqual(steps, ["calibrate", "explore", "attribute"])
            records = ledger.read(ctx.ledger_path)
            phases = {r["phase"] for r in records}
            self.assertEqual(phases, {"calibrate", "explore", "attribute"})
            self.assertTrue(all(ledger.usable(r) for r in records))
            attribution = ctx.state["attribution"]["bind"]
            self.assertTrue(attribution["effects"])
            groups = {r["replicate_group"] for r in records if r["phase"] == "attribute"}
            self.assertIn("baseline", groups)
            self.assertIn("best", groups)


if __name__ == "__main__":
    unittest.main()
