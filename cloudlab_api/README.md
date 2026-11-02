# Launching CloudLab environments

Start, inspect, extend and terminate experiments from this repo's `profile.py` through the
[CloudLab Portal API](https://gitlab.flux.utah.edu/emulab/portal-api), either from the command line or by
asking Claude Code (which uses the `cloudlab` MCP server and the `/cloudlab` skill).

## One-time setup

1. `pip install -r cloudlab_api/requirements.txt` (PyYAML only; everything else is standard library).
2. **Register the repo as a profile on the CloudLab portal** (Experiments, Create Experiment Profile, From a git repo).
   `launch.py` selects the branch with `refspec`, so the profile must be repo-backed.
3. Edit [config.yaml](config.yaml): set `project`, and `profile_name` / `profile_project` to match step 2.
4. **Create a Portal API token** and store it, readable only by you:

   ```bash
   mkdir -p ~/.config/cloudlab && install -m 600 /dev/null ~/.config/cloudlab/token
   # paste the token into ~/.config/cloudlab/token  (or: export CLOUDLAB_API_TOKEN=...)
   ```

   Get the token from the CloudLab web interface: click your name, then "Portal API Token" (the PEM
   certificate option is for the deprecated XML-RPC API and is not used here). Tokens expire:
   `launch.py refresh-token` returns a new one and revokes the old.

   `PORTAL_TOKEN` and `PORTAL_HTTP` (the portal-api client's variable names) are accepted too.
   `CLOUDLAB_API_URL` / `PORTAL_HTTP` override the base URL (default `https://boss.emulab.net:43794`).

## Command line

```bash
cd cloudlab_api
python3 launch.py profiles
python3 launch.py launch --name dns1 --num-testers 2 --server-software bind --dry-run   # shows the request
python3 launch.py launch --name dns1 --num-testers 2 --server-software bind
python3 launch.py wait <experiment_id>        # blocks until ready
python3 launch.py status <experiment_id>      # node table with ssh commands
python3 launch.py extend <experiment_id> --hours 2 --reason "long search"
python3 launch.py terminate <experiment_id>
```

Flags map onto the parameters in `profile.py`: `--num-testers`, `--server-software`, `--server-hw`,
`--tester-hw`, `--interswitch`. `--refspec` picks the git ref the nodes clone to `/local/repository`
(default `refs/heads/fully_agentic`, which carries the agent search code).
`max_duration` in `config.yaml` caps the hours a single launch or extension may request.

## With Claude Code

Open Claude Code in the repository root and approve the project MCP server `cloudlab` (from `.mcp.json`) when
prompted. Then, for example: "spin up a 2-tester BIND environment for 4 hours". Claude shows a dry run, asks
for confirmation, launches, waits, and returns the ssh commands. Extending and terminating also ask first.
Read-only calls (status, list, wait, profiles) are pre-allowed in `.claude/settings.json`.

## After the environment is up

`optimization/*.yaml` assume `NS_Local` is `10.10.1.1`, the first tester is `10.10.1.2` and the NIC is
`eno1d1`. Check these against `launch.py status` and the node's `ip a` before running a search from a tester.

## Tests

```bash
python3 -m unittest discover -s cloudlab_api
```

## Letting test_host_0 ssh to the other nodes

```bash
python3 setup_ssh_keys.py <experiment_id> [<experiment_id> ...] [--identity ~/.ssh/key] [--dry-run]
```

Creates an ed25519 key on `test_host_0` (reusing one if present), adds its public key to `~/.ssh/authorized_keys`
on every node of the experiment, records the nodes' internal addresses in `test_host_0`'s `known_hosts`, and checks
that `test_host_0` can ssh to each other node without a prompt. Safe to re-run. The machine you run it from must
already be able to ssh to the nodes (a key registered on your CloudLab account), because it works over ssh.
