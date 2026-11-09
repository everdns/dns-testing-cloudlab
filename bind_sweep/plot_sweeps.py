#!/usr/bin/env python3
"""Plot max sustainable QPS against BIND release date for both sweeps.

Reads results/bind-sweep-run1/<hw>/summary.csv (sweep 1) and
results/bind-sweep-restart/<hw>/summary.csv (sweep 2, DNS server restarted before every
trial) and writes bind_sweep/qps_by_release_sweeps.{html,png}: one panel per hardware type,
colour = hardware + sweep, marker = major BIND version. Failed builds and 0 QPS results
are left out.

    python3 bind_sweep/plot_sweeps.py
"""
import csv
import datetime as dt
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
RESULTS = REPO / "results"
OUT = HERE / "qps_by_release_sweeps"

HARDWARE = ["rs620", "d710", "r320"]
SWEEPS = [("run1", "sweep 1"), ("restart", "sweep 2")]
# Light-mode colours: hardware pair (sweep 1, sweep 2), validated pairwise for CVD and normal vision.
COLORS = {
    "rs620": ("#2a78d6", "#c77dff"),
    "d710": ("#eb6834", "#1baf7a"),
    "r320": ("#e87ba4", "#008300"),
}
BRANCH_MARKERS = {"9.16": "o", "9.17": "s", "9.18": "^", "9.19": "D", "9.20": "x", "9.21": "P"}


def vkey(v):
    return tuple(int(x) for x in v.split("."))


def load():
    """rows: one per release with {hw}_{n}: max QPS or None for each sweep n (1/2)."""
    rows = {}
    for hw in HARDWARE:
        for n, (run, _) in enumerate(SWEEPS, 1):
            with open(RESULTS / f"bind-sweep-{run}" / hw / "summary.csv") as f:
                for r in csv.DictReader(f):
                    row = rows.setdefault(r["version"], {
                        "v": r["version"], "date": r["release_date"],
                        "branch": ".".join(r["version"].split(".")[:2])})
                    ok = r["status"] == "ok" and int(r["max_qps_passed"] or 0) > 0
                    row[f"{hw}_{n}"] = int(r["max_qps_passed"]) if ok else None
    keys = [f"{hw}_{n}" for hw in HARDWARE for n in (1, 2)]
    out = [r for r in rows.values() if any(r.get(k) for k in keys)]
    for r in out:
        for k in keys:
            r.setdefault(k, None)
    return sorted(out, key=lambda r: (r["date"], vkey(r["v"])))


def write_png(rows):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as md
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D

    fig, axes = plt.subplots(len(HARDWARE), 1, figsize=(12, 11), dpi=130, sharex=True)
    for ax, hw in zip(axes, HARDWARE):
        top = 0
        for n, color in zip((1, 2), COLORS[hw]):
            for branch, mk in BRANCH_MARKERS.items():
                pts = [r for r in rows if r["branch"] == branch and r[f"{hw}_{n}"]]
                if not pts:
                    continue
                xs = [dt.date.fromisoformat(r["date"]) for r in pts]
                ys = [r[f"{hw}_{n}"] for r in pts]
                top = max(top, max(ys))
                ax.plot(xs, ys, color=color, lw=1.8, ls="-" if n == 1 else (0, (5, 3)),
                        marker=mk, ms=5.5, mec=color if mk in "xP" else "#fcfcfb",
                        mew=2 if mk in "xP" else 1.2)
        step = 100000 if top > 400000 else 50000
        ax.set_ylim(0, (int(top * 1.08 / step) + 1) * step)
        ax.yaxis.set_major_formatter(lambda v, p: f"{int(v / 1000)}k" if v else "0")
        ax.set_ylabel("Max sustainable QPS")
        ax.set_title(hw, loc="left", fontsize=12, fontweight="bold")
        ax.grid(axis="y", color="#e7e6e2")
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
        ax.legend(handles=[Line2D([0], [0], color=c, lw=2.2, ls="-" if n == 1 else (0, (5, 3)),
                                  label=f"{hw} {name}")
                           for n, (c, (_, name)) in enumerate(zip(COLORS[hw], SWEEPS), 1)],
                  loc="upper right", frameon=False, fontsize=9)
    axes[-1].set_xlim(dt.date(2021, 7, 1), dt.date(2026, 12, 1))
    axes[-1].xaxis.set_major_locator(md.YearLocator())
    axes[-1].xaxis.set_major_formatter(md.DateFormatter("%Y"))
    axes[-1].set_xlabel("BIND release date")
    fig.legend(handles=[Line2D([0], [0], color="#52514e", marker=mk, ls="none", ms=6,
                               mec="#52514e", label=b) for b, mk in BRANCH_MARKERS.items()],
               loc="upper left", bbox_to_anchor=(0.01, 0.955), ncol=len(BRANCH_MARKERS),
               frameon=False, title="BIND major version", title_fontsize=9, alignment="left")
    fig.suptitle("Max sustainable QPS by BIND release date: sweep 1 (solid) vs sweep 2 (dashed, "
                 "server restarted before every trial)", x=0.01, ha="left", fontsize=12, y=0.995)
    fig.tight_layout(rect=(0, 0, 1, 0.91))
    fig.savefig(f"{OUT}.png")


HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BIND QPS Sweeps Compared</title>
<style>
:root{--surface-1:#fcfcfb;--text-primary:#0b0b0b;--text-secondary:#52514e;--grid:#e7e6e2;--axis:#c9c8c2;
 --rs620-1:#2a78d6;--rs620-2:#c77dff;--d710-1:#eb6834;--d710-2:#1baf7a;--r320-1:#e87ba4;--r320-2:#008300;color-scheme:light}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--surface-1:#1a1a19;--text-primary:#f0efec;--text-secondary:#c3c2b7;--grid:#2b2b29;--axis:#4a4944;
 --rs620-1:#3987e5;--rs620-2:#7e46c9;--d710-1:#d95926;--d710-2:#199e70;--r320-1:#d55181;--r320-2:#008300;color-scheme:dark}}
:root[data-theme="dark"]{--surface-1:#1a1a19;--text-primary:#f0efec;--text-secondary:#c3c2b7;--grid:#2b2b29;--axis:#4a4944;
 --rs620-1:#3987e5;--rs620-2:#7e46c9;--d710-1:#d95926;--d710-2:#199e70;--r320-1:#d55181;--r320-2:#008300;color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:var(--surface-1);color:var(--text-primary);font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:1000px;margin:0 auto;padding:24px 16px}
h1{font-size:20px;margin:0 0 4px;font-weight:600}
.sub{color:var(--text-secondary);margin:0 0 14px}
.legend{display:flex;flex-wrap:wrap;gap:6px 18px;margin:0 0 6px;color:var(--text-secondary)}
.legend span{display:inline-flex;align-items:center;gap:6px}
.legend svg{width:34px;height:12px;overflow:visible}
.wrap{position:relative}
svg.chart{width:100%;height:auto;display:block;overflow:visible}
text{fill:var(--text-secondary);font-size:12px}
.ptitle{font-weight:600;font-size:13px;fill:var(--text-primary)}
#tip{position:absolute;pointer-events:none;background:var(--surface-1);border:1px solid var(--axis);border-radius:6px;padding:8px 10px;
 font-size:12px;box-shadow:0 2px 8px rgba(0,0,0,.15);display:none;white-space:nowrap;z-index:2}
#tip b{color:var(--text-primary)}#tip .r{display:flex;gap:6px;align-items:center;color:var(--text-primary)}
#tip .r i{width:14px;height:0;border-top:3px solid;display:inline-block}
button{margin-top:12px;background:none;border:1px solid var(--axis);color:var(--text-primary);border-radius:6px;padding:4px 10px;font:inherit;cursor:pointer}
table{border-collapse:collapse;width:100%;margin-top:12px;display:none;font-variant-numeric:tabular-nums}
th,td{padding:4px 8px;text-align:right;border-bottom:1px solid var(--grid)}th:first-child,td:first-child,th:nth-child(2),td:nth-child(2){text-align:left}
.note{color:var(--text-secondary);font-size:12px;margin-top:12px}
</style>
</head>
<body>
<main>
<h1>Max sustainable QPS by BIND release date, sweep 1 vs sweep 2</h1>
<p class="sub">Sweep 1: the DNS server started once per run. Sweep 2: the server was restarted before every trial. One panel per hardware type, one point per release.</p>
<div class="legend" id="legend"></div>
<div class="wrap"><svg class="chart" id="chart" role="img" aria-label="Three panels, one per hardware type, of max sustainable QPS against BIND release date for sweep 1 and sweep 2"></svg><div id="tip"></div></div>
<button id="tbtn" type="button">Show table</button>
<table id="tbl"></table>
<p class="note" id="note"></p>
</main>
<script>
const ROWS = __ROWS__, EXCLUDED = __EXCLUDED__;
const HW = ["rs620","d710","r320"];
const SW = [[1,"sweep 1",""],[2,"sweep 2 (restart each trial)","7 4"]];
const BRANCHES=[...new Set(ROWS.map(r=>r.branch))].sort((a,b)=>a.split(".")[1]-b.split(".")[1]);
const MARK={"9.16":"circle","9.17":"square","9.18":"triangle","9.19":"diamond","9.20":"cross","9.21":"plus"};
const W=960,PH=250,TOP=34,M={l:64,r:24,b:34};
const H=HW.length*PH+8;
const svg=document.getElementById("chart"),tip=document.getElementById("tip");
svg.setAttribute("viewBox",`0 0 ${W} ${H}`);
const css=n=>getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const t=d=>Date.parse(d);
const x0=t("2021-07-01"),x1=t("2026-12-01");
const X=d=>M.l+(t(d)-x0)/(x1-x0)*(W-M.l-M.r);
const ns="http://www.w3.org/2000/svg";
const el=(n,a,p=svg,txt)=>{const e=document.createElementNS(ns,n);for(const k in a)e.setAttribute(k,a[k]);if(txt!=null)e.textContent=txt;p.appendChild(e);return e};
const ymax={},ystep={};
HW.forEach(h=>{const top=Math.max(...ROWS.flatMap(r=>[r[h+"_1"],r[h+"_2"]]).filter(v=>v));
 const step=top>400000?100000:50000;ystep[h]=step;ymax[h]=(Math.floor(top*1.08/step)+1)*step});
const Yf=(h,oy)=>v=>oy+PH-M.b-v/ymax[h]*(PH-M.b-TOP);
function marker(m,cx,cy,col,p=svg){
 const ring=css("--surface-1"),s=4;
 if(m==="circle")return el("circle",{cx,cy,r:3.5,fill:col,stroke:ring,"stroke-width":2},p);
 if(m==="square")return el("rect",{x:cx-s+.5,y:cy-s+.5,width:2*s-1,height:2*s-1,fill:col,stroke:ring,"stroke-width":2},p);
 if(m==="triangle")return el("polygon",{points:`${cx},${cy-s-1} ${cx+s+1},${cy+s} ${cx-s-1},${cy+s}`,fill:col,stroke:ring,"stroke-width":2,"stroke-linejoin":"round"},p);
 if(m==="diamond")return el("polygon",{points:`${cx},${cy-s-1} ${cx+s+1},${cy} ${cx},${cy+s+1} ${cx-s-1},${cy}`,fill:col,stroke:ring,"stroke-width":2,"stroke-linejoin":"round"},p);
 const d=m==="plus"?`M${cx-s},${cy}H${cx+s}M${cx},${cy-s}V${cy+s}`:`M${cx-s+1},${cy-s+1}L${cx+s-1},${cy+s-1}M${cx+s-1},${cy-s+1}L${cx-s+1},${cy+s-1}`;
 return el("path",{d,stroke:col,"stroke-width":2.5,"stroke-linecap":"round",fill:"none"},p);
}
function draw(){
 svg.innerHTML="";
 HW.forEach((h,pi)=>{
  const oy=pi*PH,Y=Yf(h,oy);
  el("text",{x:M.l,y:oy+18,class:"ptitle"},svg,h);
  SW.forEach(([n,name,dash],si)=>{
   const lx=M.l+70+si*210,col=css(`--${h}-${n}`);
   el("line",{x1:lx,x2:lx+26,y1:oy+14,y2:oy+14,stroke:col,"stroke-width":2.5,"stroke-dasharray":dash,"stroke-linecap":"round"});
   el("text",{x:lx+32,y:oy+18},svg,name);
  });
  for(let v=0;v<=ymax[h];v+=ystep[h]){
   el("line",{x1:M.l,x2:W-M.r,y1:Y(v),y2:Y(v),stroke:css("--grid"),"stroke-width":1});
   el("text",{x:M.l-8,y:Y(v)+4,"text-anchor":"end"},svg,v?v/1000+"k":"0");
  }
  el("line",{x1:M.l,x2:W-M.r,y1:Y(0),y2:Y(0),stroke:css("--axis")});
  for(let y=2022;y<=2026;y++){
   const d=`${y}-01-01`;el("line",{x1:X(d),x2:X(d),y1:Y(0),y2:Y(0)+5,stroke:css("--axis")});
   el("text",{x:X(d),y:Y(0)+20,"text-anchor":"middle"},svg,y);
  }
  if(pi===0)el("text",{x:14,y:oy+PH/2,transform:`rotate(-90 14 ${oy+PH/2})`,"text-anchor":"middle"},svg,"Max sustainable QPS");
  SW.forEach(([n,name,dash])=>BRANCHES.forEach(b=>{
   const pts=ROWS.filter(r=>r.branch===b&&r[h+"_"+n]);
   if(!pts.length)return;
   const col=css(`--${h}-${n}`);
   if(pts.length>1)el("polyline",{points:pts.map(r=>`${X(r.date)},${Y(r[h+"_"+n])}`).join(" "),fill:"none",stroke:col,"stroke-width":2,"stroke-dasharray":dash,"stroke-linejoin":"round","stroke-linecap":"round"});
   pts.forEach(r=>marker(MARK[b],X(r.date),Y(r[h+"_"+n]),col));
  }));
  el("line",{id:"cross"+pi,x1:0,x2:0,y1:oy+TOP,y2:Y(0),stroke:css("--axis"),"stroke-dasharray":"3 3",visibility:"hidden"});
  const hit=el("rect",{x:M.l,y:oy+TOP,width:W-M.l-M.r,height:PH-M.b-TOP,fill:"transparent"});
  hit.addEventListener("pointermove",ev=>{
   const b=svg.getBoundingClientRect(),px=(ev.clientX-b.left)*W/b.width;
   let best=0,bd=1e9;ROWS.forEach((r,i)=>{const d=Math.abs(X(r.date)-px);if(d<bd){bd=d;best=i}});
   const r=ROWS[best],same=ROWS.filter(q=>q.date===r.date);
   HW.forEach((_,k)=>document.getElementById("cross"+k).setAttribute("visibility","hidden"));
   const c=document.getElementById("cross"+pi);c.setAttribute("x1",X(r.date));c.setAttribute("x2",X(r.date));c.setAttribute("visibility","visible");
   tip.innerHTML=`<div style="color:${css("--text-secondary")}">${h} · released ${r.date}</div>`+same.map(q=>
    `<div style="margin-top:6px"><b>BIND ${q.v}</b></div>`+SW.map(([n,name,dash])=>
     `<div class="r"><i style="border-top-style:${dash?"dashed":"solid"};border-color:${css(`--${h}-${n}`)}"></i>${name}: ${q[h+"_"+n]!=null?q[h+"_"+n].toLocaleString()+" QPS":"n/a"}</div>`).join("")).join("");
   tip.style.display="block";
   const wb=svg.parentElement.getBoundingClientRect(),tx=ev.clientX-wb.left+14;
   tip.style.left=Math.min(tx,wb.width-tip.offsetWidth-4)+"px";tip.style.top=Math.max(0,ev.clientY-wb.top-20)+"px";
  });
  hit.addEventListener("pointerleave",()=>{tip.style.display="none";document.getElementById("cross"+pi).setAttribute("visibility","hidden")});
 });
}
function drawKeys(){
 const lg=document.getElementById("legend");
 lg.innerHTML=HW.map(h=>SW.map(([n,name,dash])=>`<span><svg viewBox="0 0 34 12"><line x1="1" x2="33" y1="6" y2="6" stroke="${css(`--${h}-${n}`)}" stroke-width="2.5" stroke-dasharray="${dash}" stroke-linecap="round"/></svg>${h} ${name.split(" (")[0]}</span>`).join("")).join("")+
  `<span style="margin-left:6px">BIND major version:</span>`+BRANCHES.map(b=>`<span><svg class="key" viewBox="0 0 34 12" data-b="${b}"></svg>${b}</span>`).join("");
 document.querySelectorAll("svg.key").forEach(k=>marker(MARK[k.dataset.b],17,6,css("--text-secondary"),k));
}
document.getElementById("tbl").innerHTML="<thead><tr><th>Version</th><th>Released</th>"+HW.map(h=>`<th>${h} s1</th><th>${h} s2</th>`).join("")+"</tr></thead><tbody>"+
 ROWS.map(r=>`<tr><td>${r.v}</td><td>${r.date}</td>`+HW.map(h=>[1,2].map(n=>`<td>${r[h+"_"+n]!=null?r[h+"_"+n].toLocaleString():"–"}</td>`).join("")).join("")+"</tr>").join("")+"</tbody>";
document.getElementById("note").textContent=`${ROWS.length} releases plotted. Left out (failed to build or verify, or measured 0 QPS in a sweep): ${EXCLUDED.join(", ")}. Each panel has its own y scale.`;
const tb=document.getElementById("tbtn"),tbl=document.getElementById("tbl");
tb.onclick=()=>{const s=tbl.style.display!=="table";tbl.style.display=s?"table":"none";tb.textContent=s?"Hide table":"Show table"};
draw();drawKeys();
matchMedia("(prefers-color-scheme: dark)").addEventListener("change",()=>{draw();drawKeys()});
</script>
</body>
</html>
"""


def write_html(rows, all_versions):
    shown = {r["v"] for r in rows}
    excluded = sorted((v for v in all_versions if v not in shown), key=vkey)
    html = HTML.replace("__ROWS__", json.dumps(rows)).replace("__EXCLUDED__", json.dumps(excluded))
    Path(f"{OUT}.html").write_text(html)


def main():
    rows = load()
    all_versions = set()
    for hw in HARDWARE:
        for run, _ in SWEEPS:
            with open(RESULTS / f"bind-sweep-{run}" / hw / "summary.csv") as f:
                all_versions |= {r["version"] for r in csv.DictReader(f)}
    write_html(rows, all_versions)
    write_png(rows)
    print(f"{len(rows)} releases plotted -> {OUT}.html, {OUT}.png")


if __name__ == "__main__":
    main()
