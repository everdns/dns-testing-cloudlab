"""Knot DNS: "section.key: value" lines in knot.conf (server, xdp, database)."""
import os

from servers.base import REPO_ROOT, ServerAdapter, ServerConfigError, merge_key_lines

SECTIONS = ("server", "xdp", "database")


class KnotAdapter(ServerAdapter):
    name = "knot"
    process = "knotd"
    binary = "/usr/sbin/knotd"
    version_flag = "-V"
    config_path = "/etc/knot/knot.conf"
    base_config_file = os.path.join(REPO_ROOT, "ns_software", "knot", "knot.conf")
    zone_path = "/etc/knot/db.workbench.lan"
    check_command = "sudo knotc -c {path} conf-check"
    statement_help = ("knot.conf settings as \"section.key: value\", with "
                      f"section one of {', '.join(SECTIONS)} (e.g. "
                      "\"server.udp-workers: 16\"); they replace the same key")
    forbidden = ("listen", "rundir", "user", "storage")
    source_repo = "https://gitlab.nic.cz/knot/knot-dns.git"
    tag_format = "v{version}"
    software_dir = "/etc/knot"
    custom_args = "-c /etc/knot/knot.conf -d"
    custom_stop_package = "systemctl stop knot"
    custom_prepare = "mkdir -p /run/knot && chown knot:knot /run/knot"

    def _split(self, statement):
        head, sep, value = statement.strip().partition(":")
        section, dot, key = head.strip().partition(".")
        if not sep or not dot or section not in SECTIONS:
            raise ServerConfigError(
                f"expected 'section.key: value' with section in {SECTIONS}, "
                f"got {statement!r}")
        return section, f"{key.strip()}: {value.strip()}"

    def check_statement(self, statement):
        section, line = self._split(statement)
        merge_key_lines("", section, [line], "    ", self.forbidden)
        return statement.strip()

    def render(self, base_text, statements):
        text = base_text
        for statement in statements:
            section, line = self._split(statement)
            text = merge_key_lines(text, section, [line], "    ", self.forbidden)
        return text
