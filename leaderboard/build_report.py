#!/usr/bin/env python3
"""Build search_results.html from data/<env>/<search>.json (+ .report.json)."""
import argparse, json, os
HERE = os.path.dirname(os.path.abspath(__file__))
ENVS = [("r320", "r320", "16 threads"), ("d710", "d710", "8 threads"), ("rs620", "rs620", "40 threads")]
SEARCHES = ["1_agent_bounded", "2_coordinate_descent", "3_agent_open"]
PARAMS = ["udp-receive-buffer", "udp-send-buffer", "max-udp-size", "udp-conntrack", "rmem",
          "netdev-max-backlog", "netdev-budget", "udp-mem", "optmem-max", "named-nofile"]

def parse(v):
    """Open-mode rows store lists as JSON strings in some files."""
    if isinstance(v, str):
        try: return json.loads(v)
        except ValueError: return [v] if v else []
    return v or []

def changes(r, search):
    """Every change a measurement applied, as (title, [lines]) groups."""
    if search == "3_agent_open":
        out = [("BIND named.conf.options", parse(r.get("named_options"))),
               ("Commands run as root on the name server", parse(r.get("host_commands")))]
        out.append(("Reboot after applying", ["yes" if r.get("reboot_after_apply") else "no"]))
        rev = parse(r.get("revert_commands"))
        if rev: out.append(("Agent-supplied revert commands", rev))
        return [(t, l) for t, l in out if l]
    return [("Parameter values (every parameter the search sets; \"default\" means left unchanged)",
             [f"{k} = {r[k]}" for k in PARAMS if k in r])]

def num(v):
    try: return float(v)
    except (TypeError, ValueError): return None

ap = argparse.ArgumentParser()
ap.add_argument("--data-dir", default="data")
ap.add_argument("--out", default="search_results.html")
ap.add_argument("--clients", type=int, default=1)
ap.add_argument("--dates", default="9–10 October 2026")
ap.add_argument("--title", default="Three searches on three machines")
ap.add_argument("--run-label", default="run 1")
args = ap.parse_args()
DATA_DIR = os.path.join(HERE, args.data_dir)

data = {}
for env, name, cpus in ENVS:
    data[env] = {"name": name, "cpus": cpus, "searches": {}}
    for s in SEARCHES:
        rows = json.load(open(f"{DATA_DIR}/{env}/{s}.json"))
        rp = f"{DATA_DIR}/{env}/{s}.report.json"
        report = json.load(open(rp)) if os.path.exists(rp) else {}
        out = []
        best = None
        for r in rows:
            f = num(r.get("min_qps_fidelity_pct"))
            if r["status"] == "ok" and num(r.get("max_qps_passed")) is not None and (f is None or f >= 99):
                if best is None or num(r["max_qps_passed"]) > num(best["max_qps_passed"]): best = r
        for r in rows:
            fid = num(r.get("min_qps_fidelity_pct"))
            ok = r["status"] == "ok" and num(r.get("max_qps_passed")) is not None
            if s == "3_agent_open":
                desc = "; ".join(parse(r.get("named_options")) + parse(r.get("host_commands")))
            else:
                desc = ", ".join(f"{k}={r[k]}" for k in PARAMS if str(r.get(k, "default")) != "default")
            out.append({"i": r["index"], "qps": int(num(r["max_qps_passed"]) or 0) if ok else None,
                        "fid": fid, "status": r["status"], "hours": (num(r.get("search_duration_s")) or 0) / 3600,
                        "desc": desc, "ceiling": r.get("hit_max_qps_ceiling") in (True, "True")})
        data[env]["searches"][s] = {"rows": out, "summary": report.get("summary", ""),
            "winner": ({"i": best["index"], "qps": int(num(best["max_qps_passed"])),
                        "hypothesis": best.get("hypothesis") or "",
                        "changes": changes(best, s)} if best else None)}

tpl = open(f"{HERE}/search_results.template.html").read()
if args.clients == 1:
    clients, note = "one load generator", "One load generator can send about 700k–780k QPS on this machine, so higher rates are capped by it."
else:
    clients, note = f"{args.clients} load generators", "Two load generators were used, but some high-rate levels were still under 99% fidelity."
html = (tpl.replace("__DATA__", json.dumps(data)).replace("__TITLE__", args.title).replace("__DATES__", args.dates)
        .replace("__CLIENTS_NOTE__", note).replace("__CLIENTS__", clients).replace("__DATADIR__", args.data_dir)
        .replace("__RUNLABEL__", args.run_label))
out = os.path.join(HERE, args.out)
open(out, "w").write(html)
print("wrote", args.out, os.path.getsize(out), "bytes")
