"""BIND: named.conf.options statements, through bind_config.py."""
import bind_config
from bind_config import BindConfigError
from servers.base import ServerAdapter, ServerConfigError


class BindAdapter(ServerAdapter):
    name = "bind"
    process = "named"
    binary = "/usr/sbin/named"
    version_flag = "-v"
    config_path = bind_config.INSTALLED_OPTIONS_PATH
    base_config_file = bind_config.BASE_OPTIONS_FILE
    zone_path = "/etc/bind/db.workbench.lan"
    statement_help = ("named.conf.options statements, each complete and ending "
                      "in ';' (e.g. \"reuseport yes;\"); they replace a base "
                      "statement with the same keyword")
    forbidden = tuple(sorted(bind_config.FORBIDDEN))
    source_repo = "https://gitlab.isc.org/isc-projects/bind9.git"
    tag_format = "v{version}"

    def check_statement(self, statement):
        try:
            return bind_config.check_raw_statement(statement)
        except BindConfigError as e:
            raise ServerConfigError(str(e)) from e

    def render(self, base_text, statements):
        try:
            return bind_config.render_raw_options(base_text, statements)
        except BindConfigError as e:
            raise ServerConfigError(str(e)) from e

    def install(self, server, text, dry_run=False):
        try:
            bind_config.install_options(server, text, dry_run=dry_run)
        except BindConfigError as e:
            raise ServerConfigError(str(e)) from e

