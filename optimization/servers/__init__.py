"""Server adapters for the authoritative DNS servers the campaign tunes.

    from servers import get_adapter
    adapter = get_adapter("bind")      # or "knot", "nsd", or a package service name
"""
from servers.base import CUSTOM_SERVICE, SOURCE_ROOT, ServerAdapter, ServerConfigError
from servers.bind import BindAdapter
from servers.knot import KnotAdapter
from servers.nsd import NsdAdapter

ADAPTERS = {cls.name: cls for cls in (BindAdapter, KnotAdapter, NsdAdapter)}


def get_adapter(name):
    """Adapter for a software name ("bind") or a package service ("ns_bind").
    ns_custom_server runs whichever software was built last, so it does not
    name one: pass the software instead."""
    if name == CUSTOM_SERVICE:
        raise ServerConfigError(f"{CUSTOM_SERVICE} can be any software; name the "
                                "software (bind, knot, nsd) instead")
    software = name[3:] if name.startswith("ns_") else name
    if software not in ADAPTERS:
        raise ServerConfigError(f"no adapter for {name!r}; known: {sorted(ADAPTERS)}")
    return ADAPTERS[software]()


__all__ = ["ADAPTERS", "CUSTOM_SERVICE", "SOURCE_ROOT", "ServerAdapter", "ServerConfigError", "get_adapter"]
