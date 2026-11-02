"""Server adapters for the authoritative DNS servers the campaign tunes.

    from servers import get_adapter
    adapter = get_adapter("bind")      # or "knot", "nsd", or a service name
"""
from servers.base import SOURCE_ROOT, ServerAdapter, ServerConfigError
from servers.bind import BindAdapter
from servers.knot import KnotAdapter
from servers.nsd import NsdAdapter

ADAPTERS = {cls.name: cls for cls in (BindAdapter, KnotAdapter, NsdAdapter)}


def get_adapter(name):
    """Adapter for a software name ("bind") or a service ("ns_bind_src")."""
    software = name
    if software.startswith("ns_"):
        software = software[3:]
    if software.endswith("_src"):
        software = software[:-4]
    if software not in ADAPTERS:
        raise ServerConfigError(f"no adapter for {name!r}; known: {sorted(ADAPTERS)}")
    return ADAPTERS[software]()


__all__ = ["ADAPTERS", "SOURCE_ROOT", "ServerAdapter", "ServerConfigError", "get_adapter"]
