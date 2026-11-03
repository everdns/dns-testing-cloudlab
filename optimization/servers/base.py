"""What the searches need to know about one authoritative DNS server.

A ServerAdapter covers the differences between BIND, Knot DNS and NSD:

  - its main config file on the name server, the repo copy that is the base
    configuration, how free-form statements are merged into it, and the
    command that validates the result before the service is restarted;
  - the service names start_dns_service.sh knows: ``ns_<name>`` for the
    installed (package) server and ``ns_custom_server`` for a source build in
    source_prefix (see build_source.sh and source_tools.py). The custom
    server's start/stop scripts read ns_software/custom_server/info.yaml,
    which custom_server_info() renders for this software;
  - where its upstream source lives and how to read its version, binary and
    zone file, for the validity checks and the ledger.

Statements are server-specific: named.conf.options statements for BIND,
"section.key: value" lines for Knot, "key: value" lines in the server:
clause for NSD. knobs.yaml renders into the same forms.
"""
import logging
import os
import re
import shlex
import subprocess
import tempfile

from max_sustainable_qps import scp_to, ssh_run

log = logging.getLogger("servers")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SOURCE_ROOT = "/opt/dnsbuild"
CUSTOM_SERVICE = "ns_custom_server"
CUSTOM_INFO_PATH = "/local/repository/ns_software/custom_server/info.yaml"


class ServerConfigError(Exception):
    pass


class ServerAdapter:
    name = ""               # ns_software/<name>
    process = ""            # process name, for perf and pidof
    binary = ""             # installed (package) server binary
    version_flag = "-V"
    config_path = ""        # installed config file the statements go into
    base_config_file = ""   # repo copy: the base configuration
    zone_path = ""          # installed zone file for workbench.lan
    check_command = ""      # validates the installed config; {path} = config_path
    statement_help = ""     # how statements look, for the agent prompt
    forbidden = ()          # keys an agent may not set
    source_repo = ""        # upstream git repository
    tag_format = "v{version}"  # git tag for a release version
    # ns_software/custom_server/info.yaml values for a source build
    software_dir = ""            # config directory
    custom_args = ""             # arguments of the server binary
    custom_options_file = ""     # sourced before start; its $OPTIONS are appended
    custom_options_default = ""  # $OPTIONS when there is no options file
    custom_stop_package = ""     # stops the package server
    custom_prepare = ""          # creates runtime directories

    # -- services ----------------------------------------------------------

    @property
    def package_service(self):
        return f"ns_{self.name}"

    @property
    def source_service(self):
        return CUSTOM_SERVICE

    @property
    def source_prefix(self):
        """Install prefix of the active source build on the name server."""
        return f"{SOURCE_ROOT}/{self.name}/prefix"

    def service_binary(self, service):
        if service == self.source_service:
            return f"{self.source_prefix}/sbin/{self.process}"
        return self.binary

    def identity_command(self, service):
        """Shell that prints binary=, sha=, version= and build_id= lines."""
        b = shlex.quote(self.service_binary(service))
        build_id = (f"$(cat {self.source_prefix}/BUILD_ID 2>/dev/null)"
                    if service == self.source_service else "package")
        return (f"b=$(readlink -f {b} 2>/dev/null || echo {b}); echo binary=$b; "
                f"echo sha=$(sha256sum $b 2>/dev/null | cut -c1-16); "
                f"echo version=$($b {self.version_flag} 2>&1 | head -1); "
                f"echo build_id={build_id}")

    def custom_server_info(self):
        """ns_software/custom_server/info.yaml for this software's source
        build (one value per line: the scripts read it with sed)."""
        values = {
            "software_dir": self.software_dir,
            "plotting_name": f"{self.name} (custom build)",
            "software": self.name,
            "prefix": self.source_prefix,
            "binary": f"sbin/{self.process}",
            "args": self.custom_args,
            "options_file": self.custom_options_file,
            "options_default": self.custom_options_default,
            "stop_package": self.custom_stop_package,
            "prepare": self.custom_prepare,
            "kill_pattern": f"^{SOURCE_ROOT}/",
        }
        header = "# Written by optimization/servers for the active source build.\n"
        return header + "".join(f"{k}: {v}\n" for k, v in values.items())

    def install_custom_server(self, server):
        """Point ns_custom_server at this software's source build."""
        result = ssh_run(server, f"sudo tee {CUSTOM_INFO_PATH} >/dev/null <<'EOF'\n"
                                 f"{self.custom_server_info()}EOF", timeout=60)
        if result.returncode != 0:
            raise ServerConfigError(f"could not write {CUSTOM_INFO_PATH}: "
                                    f"{(result.stderr or '').strip()[-300:]}")

    def identity(self, server, service):
        result = ssh_run(server, self.identity_command(service), timeout=60)
        out = {}
        for line in (result.stdout or "").splitlines():
            k, _, v = line.partition("=")
            if k in ("binary", "sha", "version", "build_id"):
                out[k] = v.strip()
        return out

    def release_tag(self, version_text):
        """Upstream git tag for the version string a binary prints."""
        match = re.search(r"(\d+)\.(\d+)\.(\d+)", version_text or "")
        if not match:
            raise ServerConfigError(f"no version number in {version_text!r}")
        major, minor, patch = match.groups()
        return self.tag_format.format(version=f"{major}.{minor}.{patch}",
                                      major=major, minor=minor, patch=patch)

    # -- configuration -----------------------------------------------------

    def load_base(self):
        with open(self.base_config_file) as f:
            return f.read()

    def check_statement(self, statement):
        raise NotImplementedError

    def render(self, base_text, statements):
        """The base config with ``statements`` merged in; raises
        ServerConfigError for a statement that is malformed or protected."""
        raise NotImplementedError

    def install(self, server, text, dry_run=False):
        """Stage ``text`` as the config, validate it, and put it in place.
        The old file stays if validation fails."""
        if dry_run:
            print(text)
            return
        staged = f"/tmp/campaign.{self.name}.conf.candidate"
        with tempfile.NamedTemporaryFile("w", delete=False) as fh:
            fh.write(text)
            local = fh.name
        try:
            scp_to(server, local, staged)
        except subprocess.CalledProcessError as e:
            raise ServerConfigError(f"could not copy the config to {server}: "
                                    f"{e.stderr or e}") from e
        finally:
            os.unlink(local)
        check = self.check_command.format(path=staged)
        result = ssh_run(server, f"{check} && sudo cp {staged} {self.config_path}",
                         timeout=120)
        if result.returncode != 0:
            raise ServerConfigError(
                f"{self.name} rejected the config (rc={result.returncode}): "
                f"{(result.stderr or result.stdout or '').strip()[-1500:]}")

    def restore_base(self, server, base_text):
        log.info("Restoring the base %s config on %s", self.name, server)
        self.install(server, base_text)

    def read_installed(self, server):
        return ssh_run(server, f"sudo cat {self.config_path}", timeout=60).stdout or ""


def merge_key_lines(text, section, statements, indent, forbidden, separator=":"):
    """Line-based merge for "key: value" configs (Knot, NSD).

    Each statement "key: value" replaces the line with the same key inside
    the top-level ``section`` block, or is appended to that block (which is
    created at the end if missing)."""
    pairs = []
    for statement in statements:
        key, sep, value = statement.strip().partition(separator)
        key = key.strip()
        if not sep or not key or not value.strip() or not re.fullmatch(r"[A-Za-z0-9-]+", key):
            raise ServerConfigError(f"expected 'key{separator} value', got {statement!r}")
        if key in forbidden:
            raise ServerConfigError(f"refusing to set protected option: {key}")
        pairs.append((key, value.strip()))

    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines)
                  if re.match(rf"^{re.escape(section)}\s*:\s*$", line)), None)
    if start is None:
        lines += ["", f"{section}:"]
        start = len(lines) - 1
    end = start + 1
    while end < len(lines) and (not lines[end].strip() or lines[end][0] in " \t"):
        end += 1
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    body = lines[start + 1:end]
    for key, value in pairs:
        new = f"{indent}{key}{separator} {value}"
        for i, line in enumerate(body):
            if re.match(rf"^\s+{re.escape(key)}\s*{separator}", line):
                body[i] = new
                break
        else:
            body.append(new)
    return "\n".join(lines[:start + 1] + body + lines[end:]) + "\n"
