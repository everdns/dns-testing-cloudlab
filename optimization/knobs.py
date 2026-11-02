"""Canonical tuning knobs (knobs.yaml): validation, tagging and rendering.

The cross-hardware question "which variables matter most, and where" needs
every measurement described in the same vocabulary. knobs.yaml defines that
vocabulary; this module

  - validates a knob selection ({name: value}) against the catalog bounds,
  - renders a selection into the open-mode change format (server config
    statements, root host commands, reboot flag) for one server software and
    one host (its client-facing interface and CPU count),
  - maps grid/bounded parameter rows onto knob names (``aliases``), and
  - describes the catalog for an agent prompt.

Baseline values ("default", false, "none") mean "leave the host as it is"
and render to nothing, so the empty selection is the baseline.
"""
import logging
import os

import yaml

import system_config
from system_config import SystemConfigError

log = logging.getLogger("knobs")

KNOBS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "knobs.yaml")
TYPES = ("bool", "int", "pair", "enum")


class KnobError(Exception):
    pass


def _parse_bool(name, value):
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("true", "yes", "on", "1"):
        return True
    if text in ("false", "no", "off", "0", "default", "none"):
        return False
    raise KnobError(f"{name}: expected true or false, got {value!r}")


def _parse_int(name, value, low, high):
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise KnobError(f"{name}: expected an integer, got {value!r}")
    if isinstance(value, float) and value != number:
        raise KnobError(f"{name}: expected an integer, got {value!r}")
    if not low <= number <= high:
        raise KnobError(f"{name}: {number} is outside [{low}, {high}]")
    return number


class Catalog:
    def __init__(self, data):
        self.prelude = data.get("prelude") or ""
        self.aliases = dict(data.get("aliases") or {})
        self.knobs = {}
        for name, spec in (data.get("knobs") or {}).items():
            if spec.get("type") not in TYPES:
                raise KnobError(f"knob {name}: type must be one of {TYPES}")
            if "baseline" not in spec:
                raise KnobError(f"knob {name}: missing 'baseline'")
            self.knobs[name] = spec
        for alias, target in self.aliases.items():
            if target not in self.knobs:
                raise KnobError(f"alias {alias} -> unknown knob {target}")

    # -- values -------------------------------------------------------------

    def spec(self, name):
        if name not in self.knobs:
            raise KnobError(f"unknown knob {name!r}; known: {', '.join(self.knobs)}")
        return self.knobs[name]

    def normalize(self, name, value):
        """The canonical form of ``value`` for knob ``name``; raises KnobError."""
        spec = self.spec(name)
        kind = spec["type"]
        if value == spec["baseline"] or str(value).strip().lower() == str(spec["baseline"]).lower():
            return spec["baseline"]
        if kind == "bool":
            return _parse_bool(name, value)
        if kind == "enum":
            for choice in spec["choices"]:
                if str(choice).lower() == str(value).strip().lower():
                    return choice
            raise KnobError(f"{name}: {value!r} is not one of {spec['choices']}")
        if kind == "int":
            if spec.get("typical") == "cpus" and str(value).strip().lower() == "cpus":
                return "cpus"
            return _parse_int(name, value, spec["min"], spec["max"])
        parts = str(value).split("/")
        if len(parts) != 2:
            raise KnobError(f"{name}: expected '<first>/<second>', got {value!r}")
        first, second = (_parse_int(name, p.strip(), lo, hi)
                         for p, lo, hi in zip(parts, spec["min"], spec["max"]))
        return f"{first}/{second}"

    def is_off(self, name, value):
        return self.normalize(name, value) == self.spec(name)["baseline"]

    def typical_value(self, name):
        spec = self.spec(name)
        return spec.get("typical", True if spec["type"] == "bool" else spec["baseline"])

    def validate(self, selection):
        """(normalized selection without baseline values, errors)."""
        if not isinstance(selection, dict):
            return {}, ["knobs must be an object mapping knob names to values"]
        normalized, errors = {}, []
        for name, value in selection.items():
            try:
                value = self.normalize(name, value)
            except KnobError as e:
                errors.append(str(e))
                continue
            if value != self.knobs[name]["baseline"]:
                normalized[name] = value
        return normalized, errors

    # -- applicability ------------------------------------------------------

    def applicable(self, name, software):
        spec = self.spec(name)
        return bool(spec.get("host") or spec.get("system_config")
                    or software in (spec.get("software") or {})
                    or software in (spec.get("host_by_software") or {}))

    def needs_reboot(self, selection):
        return any(self.knobs[n].get("reboot") for n in selection
                   if n in self.knobs and not self.is_off(n, selection[n]))

    # -- rendering ----------------------------------------------------------

    def _substitute(self, template, value, facts):
        text = str(template)
        if isinstance(value, str) and "/" in value:
            first, second = value.split("/", 1)
            text = text.replace("@first@", first).replace("@second@", second)
        text = text.replace("@value@", str(value))
        text = text.replace("@iface@", str(facts.get("iface") or ""))
        if "cpumask" in text and self.prelude:
            text = self.prelude + text
        return text

    def render(self, selection, software, facts=None):
        """{"named_options": [...], "host_commands": [...],
        "reboot_after_apply": bool} for ``selection`` on ``software``.
        ``facts`` needs "iface" for NIC knobs and "cpus" for value "cpus".
        Knobs that do not apply to ``software`` raise KnobError."""
        facts = facts or {}
        selection, errors = self.validate(selection)
        if errors:
            raise KnobError("; ".join(errors))
        options, commands = [], []
        for name, spec in self.knobs.items():  # catalog order: deterministic
            if name not in selection:
                continue
            if not self.applicable(name, software):
                raise KnobError(f"knob {name} has no implementation for {software}")
            value = selection[name]
            if value == "cpus":
                if not facts.get("cpus"):
                    raise KnobError(f"{name}=cpus needs the server's CPU count")
                value = facts["cpus"]
            if isinstance(value, bool):
                value = "true"
            if "@iface@" in str(spec) and not facts.get("iface"):
                raise KnobError(f"knob {name} needs the server's interface name")
            if spec.get("system_config"):
                try:
                    commands += system_config.render_commands(
                        {spec["system_config"]: value})
                except SystemConfigError as e:
                    raise KnobError(f"{name}: {e}")
            for template in spec.get("host") or []:
                commands.append(self._substitute(template, value, facts))
            for template in (spec.get("host_by_software") or {}).get(software, []):
                commands.append(self._substitute(template, value, facts))
            for template in (spec.get("software") or {}).get(software, []):
                options.append(self._substitute(template, value, facts))
        return {"named_options": options, "host_commands": commands,
                "reboot_after_apply": self.needs_reboot(selection)}

    # -- tagging ------------------------------------------------------------

    def from_parameters(self, overrides):
        """Knob selection for a grid/bounded row's parameter values."""
        selection = {}
        for param, value in overrides.items():
            name = self.aliases.get(param)
            if name is None:
                continue
            try:
                value = self.normalize(name, value)
            except KnobError:
                continue
            if value != self.knobs[name]["baseline"]:
                selection[name] = value
        return selection

    def describe(self, software=None):
        """The catalog as an agent sees it (only knobs that apply to
        ``software`` when given)."""
        out = []
        for name, spec in self.knobs.items():
            if software and not self.applicable(name, software):
                continue
            entry = {"name": name, "scope": spec.get("scope"), "type": spec["type"],
                     "baseline": spec["baseline"], "description": spec.get("description", "")}
            if spec["type"] == "enum":
                entry["choices"] = spec["choices"]
            elif spec["type"] in ("int", "pair"):
                entry["min"], entry["max"] = spec["min"], spec["max"]
            if spec.get("reboot"):
                entry["reboot"] = True
            out.append(entry)
        return out


def load(path=KNOBS_FILE):
    with open(path) as f:
        return Catalog(yaml.safe_load(f))
