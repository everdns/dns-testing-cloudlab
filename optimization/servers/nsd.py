"""NSD: "key: value" lines in nsd.conf's server: clause."""
import os

from servers.base import REPO_ROOT, ServerAdapter, merge_key_lines


class NsdAdapter(ServerAdapter):
    name = "nsd"
    process = "nsd"
    binary = "/usr/local/sbin/nsd"   # ns_software/nsd/install.sh builds into /usr/local
    version_flag = "-v"
    config_path = "/etc/nsd/nsd.conf"
    base_config_file = os.path.join(REPO_ROOT, "ns_software", "nsd", "nsd.conf")
    zone_path = "/etc/nsd/db.workbench.lan"
    check_command = "sudo nsd-checkconf {path}"
    statement_help = ("nsd.conf server: clause settings as \"key: value\" (e.g. "
                      "\"server-count: 16\"); they replace the same key")
    forbidden = ("ip-address", "interface", "port", "zonesdir", "pidfile",
                 "username", "chroot")
    source_repo = "https://github.com/NLnetLabs/nsd.git"
    tag_format = "NSD_{major}_{minor}_{patch}_REL"
    software_dir = "/etc/nsd"
    custom_args = "-c /etc/nsd/nsd.conf"
    custom_stop_package = "pkill -x nsd"
    custom_prepare = "mkdir -p /var/db/nsd"

    def check_statement(self, statement):
        merge_key_lines("", "server", [statement], "\t", self.forbidden)
        return statement.strip()

    def render(self, base_text, statements):
        return merge_key_lines(base_text, "server", statements, "\t", self.forbidden)
