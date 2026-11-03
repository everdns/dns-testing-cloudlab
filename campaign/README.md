# Continuous optimization campaigns

`campaign.py` keeps a CloudLab environment busy with optimization experiments for as long as it exists. One daemon runs on each environment's `test_host_0`. Every measurement it makes goes into one ledger format, so a script on your laptop can compare environments.

The campaign serves two research questions:

1. **Which configuration variables matter most, on which hardware?** Every change is expressed in the knob catalog (`optimization/knobs.yaml`). The attribution and factorial steps then measure each knob's effect with replicates and confidence intervals. `analysis/importance.py` lines the effects up across environments.
2. **How far can source changes go?** The source step runs a Claude agent that patches BIND, Knot DNS or NSD. Each patch is built and checked for correct answers, then measured against an unpatched build of the same release.

## How a campaign runs

```
health check -> next step -> run it -> reset the name server -> repeat   (until ~/campaign/STOP)
```

| Step | Uses Claude | What it does |
|---|---|---|
| `calibrate` | no | Runs first and every 24 h. Measures the stock baseline 3× (giving the noise σ). Probes how fast the two load generators can send, and caps `max_qps` just above that. |
| `explore` | yes | The open-mode agent (`agent_search.py --mode open`). It can change anything on the server but must tag each change with catalog knobs. Its prompt includes this environment's history and findings from the other environments. Afterwards, every change within 2σ of the best is re-measured until it has 3 runs. |
| `attribute` | no | Takes the best valid knob set B. For each knob k it measures baseline + k (single-add) and B − k (leave-one-out), 3 replicates each, plus B and the baseline. Measurements run in a shuffled order. The output is each knob's effect as % of baseline, with a 95% bootstrap CI. |
| `factorial` | no | Picks up to 8 portable knobs: the top ones from every environment (via the inbox) plus this environment's attribution. Runs them as a 16-run resolution IV design, 3 replicates. Main effects are comparable across hardware; two-factor interactions are reported as alias chains. |
| `source` | yes | Rotates through bind, knot and nsd. Checks out the source tag matching the installed version and builds it with fixed flags. Measures that unpatched build 3×, then lets the source agent profile, patch, build, check and measure. The host stays fixed at the environment's best non-reboot knob set. |

The cycle is `explore, attribute, factorial, source` (set by `cycle` in `campaign.yaml`). When Claude usage runs out (the `agent.max_*_usage` limits), the LLM steps wait `agent.backoff_hours` and the measurement-only steps keep running.

## Rules every measurement follows

- **Validity gate** (`optimization/validity.py`):
  - Once per server software, the stock server's answers to an even sample of 2,000 queries are captured and checked against the zone file.
  - Before and after every measurement, the server's answers must match that reference.
  - During every trial, a probe on the name server checks answers at a low rate, so a server that answers wrongly under load is caught.
  - The zone file and the clients' query input must hash as they did when the reference was captured.

  A failing row stays in the ledger with its reasons, but it never counts as a result, for searches or for analysis.
- **Load-generator limit:** a row where the clients could not send the requested rate (fidelity < 99%) is marked `client_limited` and treated as a lower bound.
- **Replicates are adaptive:**
  - A single exploratory run is enough to look at.
  - A run near the best must be repeated before it counts as the best.
  - Attribution and factorial always use 3 replicates.
  - The baseline is re-measured daily, so drift shows up.
- **Source baseline:** patches are compared with the same tag built the same way (`-O2 -g -fno-omit-frame-pointer`), never with the distro package.

## Setting up an environment

Do the usual setup first (`setup_prompt`):
- Two testers.
- `deploy_zone.sh` has run.
- test_host_0 can SSH to 10.10.1.1–3.
- `optimization/agent_search.yaml` matches the NICs and queue counts.
- Claude Code is installed and logged in on test_host_0.

The nodes must run this branch (`refspec` in `cloudlab_api/config.yaml`). Then, on test_host_0:

```bash
bash /local/repository/campaign/install_campaign.sh rs620      # the env name = NS_Local's hardware type
journalctl --user -u dns-campaign -f
python3 /local/repository/campaign/campaign.py status
python3 /local/repository/campaign/campaign.py stop            # finishes the current measurement, then exits
```

On its first start the campaign:
- snapshots the pristine name server to `~/campaign/pristine.json`;
- copies `ns_software/` (including the `custom_server` service for source builds) to the server's `/local/repository`;
- fingerprints the hardware.

Knot DNS and NSD are installed on the server the first time a source step needs them. They use the BIND zone that `deploy_zone.sh` installed.

To try the scheduler without hosts, use simulate mode. A made-up response surface stands in for the server, and nothing is measured:

```bash
python3 campaign/campaign.py run --config campaign/campaign.yaml --simulate --max-experiments 6
```

To run a single step:

```bash
python3 campaign/campaign.py run --config ~/campaign/campaign.yaml --step attribute --max-experiments 1
```

## On your laptop: collect, compare, share

```bash
python3 campaign/campaign_sync.py pull       # every env's ~/campaign -> leaderboard/campaign_data/<env>/
python3 analysis/importance.py               # -> leaderboard/importance.html + shared findings
python3 campaign/campaign_sync.py push       # findings + experiment expiry -> each env's ~/campaign/inbox/
python3 campaign/campaign_sync.py status     # campaign.py status everywhere
```

`campaign_sync.py` finds the environments through the CloudLab API: each running experiment's `test_host_0`, named by NS_Local's hardware type. If you would rather list them yourself, use `campaign/envs.yaml`.

It uses `~/.ssh/cloudlab` when that file exists. `push` also writes each experiment's expiry time. A campaign whose experiment has less than `alert_hours` left writes `~/campaign/ALERT`. Extending the experiment is up to you.

Earlier batch runs can be added to the analysis:

```bash
python3 optimization/import_legacy.py
python3 analysis/importance.py --ledger leaderboard/campaign_data/legacy/ledger.jsonl
```

## Files on test_host_0 (`~/campaign/`)

| Path | Contents |
|---|---|
| `campaign.yaml` | This environment's settings (copied from the repo by the installer) |
| `ledger.jsonl` | Every measurement (schema in `optimization/ledger.py`) |
| `state.json` | Scheduler position, noise estimates, latest attribution and factorial, history |
| `experiments/<NNNN>_<step>_<sw>/` | Per-step output: `experiment.json`, `plan.json` / `design.json`, `attribution.json` / `factorial.json`, agent transcripts and reports, `patches/` for source steps, per-measurement run directories |
| `knobs_proposed.jsonl` | Techniques agents found that the catalog lacks: review them and add the good ones to `optimization/knobs.yaml` |
| `validity/` | Reference answers and query samples per server software |
| `src/<sw>/` | Source checkouts used by the source agent |
| `inbox/` | `shared_findings.md`, `shared_knobs.json` (pushed from the laptop) |
| `ALERT`, `STOP`, `expires_at`, `campaign.log` | Operator signals and the log |
