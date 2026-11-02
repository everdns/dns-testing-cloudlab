"""Broad snapshot and restore of the name server's host state, for the
open-ended agent search (agent_search.py --mode open).

In open mode the agent may run any root command on the name server, so the
fixed list of knobs in system_config.py cannot undo its changes. Instead,
snapshot() records nearly all tunable state before the search starts:

  sysctls     every writable file under /proc/sys
  sysfs       writable tuning files: IRQ affinity, RPS/XPS, NIC mtu/txqlen/
              gro_flush_timeout, CPU online/governor/frequency/C-states,
              intel_pstate, THP, and writable module parameters
  nic         per interface: ethtool rings, channels, coalescing, offloads,
              RSS indirection/hash key, ntuple rules, UDP/TCP flow hashing,
              root qdisc
  services    which services are running, and the active tuned profile
  firewall    iptables-save / ip6tables-save, nft ruleset (drift only)
  modules     loaded kernel modules
  files       config files (grub, sysctl.d, modprobe.d, systemd units,
              limits, bind, ufw, udev, netplan, tuned): a hash per file, and a
              tarball of them kept on the server
  cmdline     /proc/cmdline

restore() takes a fresh snapshot, compares it with the baseline, and only
writes back what differs. Config files come first, so a changed kernel command
line is put back by update-grub and a reboot. diff() lists whatever is still
different afterwards, and the agent sees that list as residual drift.
"""
import json
import logging
import shlex
import subprocess
import time

from max_sustainable_qps import is_local, ssh_run

log = logging.getLogger(__name__)

BASELINE_TARBALL = "/var/tmp/agent_baseline.tgz"
SNAPSHOT_TIMEOUT = 300
RESTORE_TIMEOUT = 900
DEFAULT_REBOOT_TIMEOUT = 900

# Config paths saved in the tarball and hashed for drift.
CONFIG_PATHS = [
    "/etc/default/grub", "/etc/default/grub.d", "/etc/sysctl.conf",
    "/etc/sysctl.d", "/etc/modprobe.d", "/etc/modules-load.d",
    "/etc/systemd/system", "/etc/systemd/system.conf", "/etc/security/limits.conf",
    "/etc/security/limits.d", "/etc/bind", "/etc/knot", "/etc/nsd", "/etc/ufw",
    "/etc/default/irqbalance", "/etc/default/named", "/etc/udev/rules.d",
    "/etc/netplan", "/etc/tuned",
]
GRUB_PREFIXES = ("/etc/default/grub",)
INITRAMFS_PREFIXES = ("/etc/modprobe.d", "/etc/modules-load.d")

# Services started and stopped by the measurement itself, or on demand by the
# system; their state is not compared.
IGNORED_SERVICES = ("named", "bind9", "knot", "nsd", "systemd-", "packagekit", "fwupd",
                    "apt-", "unattended", "snapd", "man-db", "user@",
                    "sudo", "ssh", "polkit", "udisks", "motd", "cloud-")

REMOTE_SNAPSHOT = r'''
import glob, hashlib, json, os, re, subprocess

def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout

def read(path):
    try:
        with open(path) as f:
            return " ".join(f.read().split())
    except Exception:
        return None

def writable(path):
    try:
        return bool(os.stat(path).st_mode & 0o200)
    except Exception:
        return False

VOLATILE_SYSCTL = re.compile(
    r"^/proc/sys/(kernel/ns_last_pid|kernel/random/|fs/(dentry|inode|file-nr)|"
    r"vm/drop_caches|vm/compact_memory|net/netfilter/nf_conntrack_count|"
    r".*/stable_secret|kernel/sched_domain/|fs/binfmt_misc/|kernel/hung_task_)")

sysctls = {}
for root, _, files in os.walk("/proc/sys"):
    for name in files:
        path = os.path.join(root, name)
        if VOLATILE_SYSCTL.match(path) or not writable(path):
            continue
        value = read(path)
        if value is not None:
            sysctls[path] = value

sysfs_globs = [
    "/proc/irq/*/smp_affinity_list",
    "/sys/class/net/*/queues/rx-*/rps_cpus",
    "/sys/class/net/*/queues/rx-*/rps_flow_cnt",
    "/sys/class/net/*/queues/tx-*/xps_cpus",
    "/sys/class/net/*/mtu",
    "/sys/class/net/*/tx_queue_len",
    "/sys/class/net/*/gro_flush_timeout",
    "/sys/class/net/*/napi_defer_hard_irqs",
    "/sys/class/net/*/threaded",
    "/sys/devices/system/cpu/cpu*/online",
    "/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor",
    "/sys/devices/system/cpu/cpu*/cpufreq/scaling_min_freq",
    "/sys/devices/system/cpu/cpu*/cpufreq/scaling_max_freq",
    "/sys/devices/system/cpu/cpu*/cpufreq/energy_performance_preference",
    "/sys/devices/system/cpu/cpu*/power/energy_perf_bias",
    "/sys/devices/system/cpu/cpu*/cpuidle/state*/disable",
    "/sys/devices/system/cpu/intel_pstate/*",
    "/sys/devices/system/cpu/cpufreq/boost",
    "/sys/kernel/mm/transparent_hugepage/enabled",
    "/sys/kernel/mm/transparent_hugepage/defrag",
    "/sys/kernel/mm/transparent_hugepage/khugepaged/defrag",
    "/sys/module/*/parameters/*",
]
sysfs = {}
for pattern in sysfs_globs:
    for path in glob.glob(pattern):
        if os.path.islink(os.path.dirname(path)) and "/sys/class/net/" not in path:
            continue
        if not writable(path):
            continue
        value = read(path)
        if value is None:
            continue
        # Multiple-choice files show the active choice in brackets.
        chosen = re.search(r"\[([^\]]+)\]", value)
        sysfs[path] = chosen.group(1) if chosen else value

def section(text, header):
    """Lines after the 'Current hardware settings:' style header."""
    out, on = {}, False
    for line in text.splitlines():
        if line.strip().startswith(header):
            on = True
            continue
        if on and ":" in line:
            key, _, value = line.partition(":")
            out[key.strip()] = value.strip()
    return out

nic = {}
for iface in sorted(os.listdir("/sys/class/net")):
    if iface == "lo" or not os.path.exists(f"/sys/class/net/{iface}/device"):
        continue
    entry = {}
    entry["rings"] = section(sh(f"ethtool -g {iface} 2>/dev/null"), "Current hardware settings")
    entry["channels"] = section(sh(f"ethtool -l {iface} 2>/dev/null"), "Current hardware settings")
    coalesce = {}
    for line in sh(f"ethtool -c {iface} 2>/dev/null").splitlines():
        m = re.match(r"\s*Adaptive RX:\s*(\S+)\s+TX:\s*(\S+)", line)
        if m:
            coalesce["adaptive-rx"], coalesce["adaptive-tx"] = m.groups()
            continue
        m = re.match(r"\s*([a-z0-9-]+):\s*(\S+)\s*$", line)
        if m and m.group(2) != "n/a":
            coalesce[m.group(1)] = m.group(2)
    entry["coalesce"] = coalesce
    features = {}
    for line in sh(f"ethtool -k {iface} 2>/dev/null").splitlines():
        m = re.match(r"\s*([a-z0-9-]+):\s*(on|off)(\s*\[fixed\])?\s*$", line)
        if m and not m.group(3):
            features[m.group(1)] = m.group(2)
    entry["features"] = features
    rss = sh(f"ethtool -x {iface} 2>/dev/null")
    entry["rss_table"] = " ".join(
        " ".join(l.split(":", 1)[1].split()) for l in rss.splitlines()
        if re.match(r"\s*\d+:", l))
    key = re.search(r"RSS hash key:\s*\n\s*(\S+)", rss)
    entry["rss_hkey"] = key.group(1) if key else ""
    entry["ntuple_rules"] = sorted(re.findall(r"Filter:\s*(\d+)", sh(f"ethtool -n {iface} 2>/dev/null")))
    flow = {}
    for proto in ("udp4", "tcp4", "udp6", "tcp6"):
        text = sh(f"ethtool -n {iface} rx-flow-hash {proto} 2>/dev/null")
        letters = ""
        for needle, letter in (("L2DA", "m"), ("VLAN tag", "v"), ("L3 proto", "t"),
                               ("IP SA", "s"), ("IP DA", "d"),
                               ("L4 bytes 0 & 1", "f"), ("L4 bytes 2 & 3", "n")):
            if needle in text:
                letters += letter
        if letters:
            flow[proto] = letters
    entry["rx_flow_hash"] = flow
    entry["qdisc"] = " ".join(sh(f"tc qdisc show dev {iface} root 2>/dev/null").split()[:3])
    nic[iface] = entry

services = sorted(line.split()[0] for line in sh(
    "systemctl list-units --type=service --state=running --no-legend --plain "
    "2>/dev/null").splitlines() if line.strip())

def clean_rules(text):
    lines = [re.sub(r"\[\d+:\d+\]", "", l).rstrip() for l in text.splitlines()
             if l.strip() and not l.startswith("#")]
    return "\n".join(lines)

files = {}
for top in CONFIG_PATHS_PLACEHOLDER:
    paths = []
    if os.path.isdir(top):
        for root, _, names in os.walk(top):
            paths += [os.path.join(root, n) for n in names]
    elif os.path.exists(top):
        paths = [top]
    for path in paths:
        try:
            if os.path.islink(path):
                files[path] = "link:" + os.readlink(path)
            else:
                with open(path, "rb") as f:
                    files[path] = hashlib.sha256(f.read()).hexdigest()[:16]
        except Exception:
            pass

print(json.dumps({
    "sysctls": sysctls,
    "sysfs": sysfs,
    "nic": nic,
    "services": services,
    "tuned_profile": sh("tuned-adm active 2>/dev/null").strip(),
    "iptables": clean_rules(sh("iptables-save 2>/dev/null")),
    "ip6tables": clean_rules(sh("ip6tables-save 2>/dev/null")),
    "nft": sh("nft list ruleset 2>/dev/null").strip(),
    "modules": sorted(l.split()[0] for l in sh("lsmod").splitlines()[1:] if l.strip()),
    "files": files,
    "cmdline": read("/proc/cmdline"),
    "kernel": os.uname().release,
    "boot_id": read("/proc/sys/kernel/random/boot_id"),
}))
'''


class HostStateError(Exception):
    """The server could not be read or reached."""


def _heredoc(interpreter, script):
    return f"{interpreter} <<'HOST_STATE_EOF'\n{script}\nHOST_STATE_EOF"


def _run(server, command, timeout):
    try:
        return ssh_run(server, command, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise HostStateError(f"timed out after {timeout}s on {server}")
    except OSError as e:
        raise HostStateError(f"could not run on {server}: {e}")


def snapshot(server, save_files=False):
    """Record the host state of ``server``. With ``save_files``, also save the
    config files to BASELINE_TARBALL on the server for restore()."""
    script = REMOTE_SNAPSHOT.replace("CONFIG_PATHS_PLACEHOLDER", repr(CONFIG_PATHS))
    result = _run(server, _heredoc("sudo python3 -", script), SNAPSHOT_TIMEOUT)
    if result.returncode != 0:
        raise HostStateError(f"snapshot failed on {server} (rc={result.returncode}): "
                             f"{(result.stderr or '').strip()[-2000:]}")
    try:
        snap = json.loads(result.stdout)
    except ValueError as e:
        raise HostStateError(f"snapshot output from {server} is not JSON: {e}")
    if save_files:
        paths = " ".join(shlex.quote(p.lstrip("/")) for p in CONFIG_PATHS)
        save = (f"cd / && sudo tar -czpf {BASELINE_TARBALL} --ignore-failed-read "
                f"$(for p in {paths}; do [ -e \"$p\" ] && echo \"$p\"; done)")
        result = _run(server, save, SNAPSHOT_TIMEOUT)
        if result.returncode != 0:
            raise HostStateError(f"could not save config files on {server}: "
                                 f"{(result.stderr or '').strip()}")
    log.info("Host snapshot of %s: %d sysctls, %d sysfs files, %d NICs, %d config files",
             server, len(snap["sysctls"]), len(snap["sysfs"]), len(snap["nic"]),
             len(snap["files"]))
    return snap


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------

def _cmdline_params(cmdline):
    """Kernel parameters, without BOOT_IMAGE and in a fixed order: update-grub
    may reorder them."""
    return " ".join(sorted(p for p in (cmdline or "").split()
                           if not p.startswith("BOOT_IMAGE=")))


def needs_reboot(baseline, current):
    """True if only a reboot can put back the kernel or its command line."""
    return (_cmdline_params(baseline["cmdline"]) != _cmdline_params(current["cmdline"])
            or baseline.get("kernel") != current.get("kernel"))


def _service_relevant(name):
    return not any(name.startswith(prefix) for prefix in IGNORED_SERVICES)


def _flatten(snap):
    """Every comparable item of a snapshot as {key: value}."""
    flat = {}
    for path, value in snap["sysctls"].items():
        flat[f"sysctl {path[len('/proc/sys/'):].replace('/', '.')}"] = value
    for path, value in snap["sysfs"].items():
        flat[f"sysfs {path}"] = value
    for iface, entry in snap["nic"].items():
        for group in ("rings", "channels", "coalesce", "features", "rx_flow_hash"):
            for key, value in entry[group].items():
                flat[f"nic {iface} {group} {key}"] = value
        for key in ("rss_table", "rss_hkey", "ntuple_rules", "qdisc"):
            flat[f"nic {iface} {key}"] = entry[key]
    for name in snap["services"]:
        if _service_relevant(name):
            flat[f"service {name}"] = "running"
    for key in ("tuned_profile", "iptables", "ip6tables", "nft", "kernel"):
        flat[key] = snap.get(key)
    flat["cmdline"] = _cmdline_params(snap["cmdline"])
    for name in snap["modules"]:
        flat[f"module {name}"] = "loaded"
    for path, digest in snap["files"].items():
        flat[f"file {path}"] = digest
    return flat


def diff(baseline, current):
    """{key: (baseline value, current value)} for everything that differs.
    A missing value is None."""
    a, b = _flatten(baseline), _flatten(current)
    return {key: (a.get(key), b.get(key))
            for key in sorted(set(a) | set(b)) if a.get(key) != b.get(key)}


def summarize(drift, limit=40, width=120):
    """Drift as short human-readable lines."""
    lines = []
    for key, (was, now) in list(drift.items())[:limit]:
        text = f"{key}: {was!r} -> {now!r}"
        lines.append(text if len(text) <= width else text[:width] + "...")
    if len(drift) > limit:
        lines.append(f"... and {len(drift) - limit} more")
    return lines


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------

def _write(path, value):
    return f"printf '%s\\n' {shlex.quote(value)} > {shlex.quote(path)}"


def _nic_commands(iface, want, have):
    q = shlex.quote(iface)
    commands = []
    for group, flag, keys in (("rings", "-G", {"RX": "rx", "RX Mini": "rx-mini",
                                                "RX Jumbo": "rx-jumbo", "TX": "tx"}),
                              ("channels", "-L", {"RX": "rx", "TX": "tx",
                                                  "Other": "other",
                                                  "Combined": "combined"})):
        if want[group] != have[group]:
            args = " ".join(f"{keys[k]} {v}" for k, v in want[group].items()
                            if k in keys and v.isdigit())
            if args:
                commands.append(f"ethtool {flag} {q} {args}")
    changed = {k: v for k, v in want["coalesce"].items() if have["coalesce"].get(k) != v}
    if changed:
        # Adaptive first: some drivers ignore usecs while adaptive is on.
        order = sorted(changed, key=lambda k: not k.startswith("adaptive"))
        commands.append(f"ethtool -C {q} " + " ".join(f"{k} {changed[k]}" for k in order))
    changed = {k: v for k, v in want["features"].items() if have["features"].get(k) != v}
    for key, value in changed.items():
        commands.append(f"ethtool -K {q} {key} {value}")
    if want["rss_table"] != have["rss_table"]:
        commands.append(f"ethtool -X {q} default")
    if want["rss_hkey"] and want["rss_hkey"] != have["rss_hkey"]:
        commands.append(f"ethtool -X {q} hkey {want['rss_hkey']}")
    for rule in set(have["ntuple_rules"]) - set(want["ntuple_rules"]):
        commands.append(f"ethtool -N {q} delete {rule}")
    for proto, letters in want["rx_flow_hash"].items():
        if have["rx_flow_hash"].get(proto) != letters:
            commands.append(f"ethtool -N {q} rx-flow-hash {proto} {letters}")
    if want["qdisc"] != have["qdisc"]:
        commands.append(f"tc qdisc del dev {q} root")
    return commands


def _live_commands(baseline, current):
    """Commands that put the live (non-file) state back."""
    commands = []
    # Modules first, so their sysctls and parameters exist.
    for name in sorted(set(baseline["modules"]) - set(current["modules"])):
        commands.append(f"modprobe {shlex.quote(name)}")
    # CPUs back online next, so their per-CPU files exist.
    for path, value in baseline["sysfs"].items():
        if path.endswith("/online") and current["sysfs"].get(path) != value:
            commands.append(_write(path, value))
    # NIC settings before sysfs, so per-queue files exist for every channel.
    for iface, want in baseline["nic"].items():
        have = current["nic"].get(iface)
        if have:
            commands.extend(_nic_commands(iface, want, have))
    for path, value in baseline["sysctls"].items():
        if current["sysctls"].get(path) != value:
            commands.append(_write(path, value))
    for path, value in baseline["sysfs"].items():
        if not path.endswith("/online") and current["sysfs"].get(path) != value:
            commands.append(_write(path, value))
    if baseline["tuned_profile"] != current["tuned_profile"]:
        profile = baseline["tuned_profile"].rpartition(":")[2].strip()
        commands.append(f"tuned-adm profile {shlex.quote(profile)}" if profile
                        else "tuned-adm off")
    was = {s for s in baseline["services"] if _service_relevant(s)}
    now = {s for s in current["services"] if _service_relevant(s)}
    for name in sorted(was - now):
        commands.append(f"systemctl start {shlex.quote(name)}")
    for name in sorted(now - was):
        commands.append(f"systemctl stop {shlex.quote(name)}")
    # iptables here is iptables-nft, so the nft ruleset holds every rule.
    if baseline["nft"] != current["nft"]:
        commands.append("nft flush ruleset")
        if baseline["nft"]:
            commands.append(f"printf '%s\\n' {shlex.quote(baseline['nft'])} | nft -f -")
    else:
        for name in ("iptables", "ip6tables"):
            if baseline[name] != current[name]:
                commands.append(f"printf '%s\\n' {shlex.quote(baseline[name])} "
                                f"| {name}-restore")
    for name in sorted(set(current["modules"]) - set(baseline["modules"])):
        commands.append(f"modprobe -r {shlex.quote(name)}")
    return commands


def _file_commands(baseline, current):
    """Commands that put config files back; and whether grub was updated."""
    changed = {path for path in set(baseline["files"]) | set(current["files"])
               if baseline["files"].get(path) != current["files"].get(path)}
    if not changed:
        return [], False
    commands = [f"rm -f {shlex.quote(path)}"
                for path in sorted(changed - set(baseline["files"]))]
    commands.append(f"tar -xzpf {BASELINE_TARBALL} -C /")
    commands.append("systemctl daemon-reload")
    commands.append("udevadm control --reload 2>/dev/null || true")
    grub = any(p.startswith(GRUB_PREFIXES) for p in changed)
    if grub:
        commands.append("update-grub")
    if any(p.startswith(INITRAMFS_PREFIXES) for p in changed):
        commands.append("update-initramfs -u")
    return commands, grub


def _run_root_script(server, commands, timeout=RESTORE_TIMEOUT):
    """Run ``commands`` as root, each on its own; return the failures."""
    if not commands:
        return []
    lines = []
    for i, command in enumerate(commands):
        lines.append(f"( {command} ) >/dev/null 2>/tmp/host_state_err "
                     f"|| {{ echo \"FAILED {i}: $(tail -c 300 /tmp/host_state_err)\"; }}")
    result = _run(server, _heredoc("sudo bash", "\n".join(lines)), timeout)
    failures = []
    for line in (result.stdout or "").splitlines():
        if line.startswith("FAILED "):
            index, _, error = line[len("FAILED "):].partition(":")
            failures.append(f"{commands[int(index)][:200]}: {error.strip()}")
    return failures


def restore(server, baseline, reboot_timeout=DEFAULT_REBOOT_TIMEOUT):
    """Put ``server`` back to ``baseline``. Returns (residual drift, errors):
    what still differs afterwards, and the restore commands that failed.
    Raises HostStateError only if the server cannot be read or reached."""
    log.info("Restoring host state on %s", server)
    errors = []
    current = snapshot(server)

    commands, _ = _file_commands(baseline, current)
    errors += _run_root_script(server, commands)
    if needs_reboot(baseline, current):
        log.info("Kernel or its command line changed; rebooting %s", server)
        reboot_and_wait(server, reboot_timeout, kernel=baseline.get("kernel"))
        current = snapshot(server)

    live_errors = _run_root_script(server, _live_commands(baseline, current))
    current = snapshot(server)
    # A second pass catches values that depended on another being set first.
    # Only its failures are reported: the first pass's were retried.
    second = _live_commands(baseline, current)
    if second:
        live_errors = _run_root_script(server, second)
        current = snapshot(server)
    errors += live_errors

    drift = diff(baseline, current)
    if drift:
        log.warning("Residual drift on %s after restore (%d item(s)):\n  %s",
                    server, len(drift), "\n  ".join(summarize(drift, limit=15)))
    return drift, errors


# ---------------------------------------------------------------------------
# Reboot
# ---------------------------------------------------------------------------

def _boot_id(server):
    """The server's boot id, or None if it does not answer."""
    try:
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
             "-o", "StrictHostKeyChecking=accept-new", server,
             "cat /proc/sys/kernel/random/boot_id"],
            capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def reboot_and_wait(server, timeout=DEFAULT_REBOOT_TIMEOUT, kernel=None):
    """Reboot ``server`` and wait until it is back up. With ``kernel`` (a
    `uname -r` release), boot that kernel once via grub-reboot, so a newer
    installed kernel does not replace the one the search started on."""
    if is_local(server):
        raise HostStateError("refusing to reboot: the name server is this machine")
    before = _boot_id(server)
    if before is None:
        raise HostStateError(f"{server} does not answer over SSH")
    if kernel:
        k = shlex.quote(kernel)
        pin = (f"cfg=/boot/grub/grub.cfg; "
               f"sub=$(grep -o \"gnulinux-advanced-[0-9a-f-]*\" $cfg | head -1); "
               f"id=$(grep -o \"gnulinux-\"{k}\"-advanced-[0-9a-f-]*\" $cfg | head -1); "
               f"[ -n \"$sub\" ] && [ -n \"$id\" ] && sudo grub-reboot \"$sub>$id\"")
        if _run(server, pin, 60).returncode != 0:
            log.warning("No GRUB entry for kernel %s; booting the default", kernel)
    log.info("Rebooting %s", server)
    subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", server,
         "sudo systemctl reboot"],
        capture_output=True, timeout=30)
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(10)
        now = _boot_id(server)
        if now and now != before:
            break
    else:
        raise HostStateError(f"{server} did not come back within {timeout}s of a reboot")
    _run(server, "systemctl is-system-running --wait >/dev/null 2>&1; true", 600)
    running = _run(server, "uname -r", 60).stdout.strip()
    if kernel and running != kernel:
        log.warning("%s booted kernel %s, not %s", server, running, kernel)
    log.info("%s is back up (kernel %s)", server, running)
