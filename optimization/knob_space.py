"""Per-environment search spaces of knobs.yaml knobs for the grid and bounded
searches.

The parameters in grid_search.yaml and agent_search.yaml are the same on every
machine and cover only BIND options and a few sysctls. A planned space
replaces them for one environment: a Claude session (SpacePlanTools) reads what
the open-mode agent found there, the hardware facts and read-only diagnostics
of the name server, and picks the knobs worth searching, with grid values and
bounds that fit that hardware (worker counts up to the core count, NIC queue
counts up to what the NIC offers, buffer sizes around what helped, ...).

The space is saved in the ``parameters:`` format of those files, with catalog
knob names as parameter names:

  - name: rmem_max
    values: [16777216, default, 67108864]   # grid values; the first is the start
    agent: {type: int, min: 4194304, max: 134217728, allow_default: true,
            description: "..."}
    rationale: "why this knob and these values, on this hardware"

so grid_search.run_coordinate_descent and agent_tools.SearchTools (bounded
mode, with check=value_check(...)) run on it unchanged. The campaign's
plan_space, grid and bounded steps (campaign/phases.py) use this module.
"""
import json
import logging
import subprocess
import threading

import yaml

from agent_tools import (AgentError, MAX_TOOL_OUTPUT, PROBE_TIMEOUT, SERVER_PROBES,
                         _clip)
from knobs import KnobError
from max_sustainable_qps import ssh_run

log = logging.getLogger("knob_space")

MIN_PARAMETERS = 2
MAX_VALUES = 4


def _baseline_value(spec):
    """How a parameter of the space writes the knob's baseline."""
    if spec["type"] == "bool":
        return "false"
    if spec["type"] == "enum":
        return str(spec["baseline"])
    return "default"


def _within(catalog, name, value, low, high):
    number = catalog.normalize(name, value)
    if not low <= number <= high:
        raise KnobError(f"{name}: {number} is outside the planned bounds [{low}, {high}]")
    return number


def build_parameter(catalog, software, facts, entry):
    """One planned parameter (``entry``: knob, values, min/max or choices,
    rationale) as a validated ``parameters:`` item. Raises KnobError."""
    name = str(entry.get("knob") or "")
    spec = catalog.spec(name)
    if not catalog.applicable(name, software):
        raise KnobError(f"{name}: no implementation for {software}")
    if spec.get("reboot"):
        raise KnobError(f"{name}: needs a reboot, which grid and bounded searches "
                        "do not do; leave it out")
    kind = spec["type"]
    baseline = _baseline_value(spec)
    description = spec.get("description", "")
    agent = {"description": description}

    if kind == "bool":
        agent.update(type="enum", choices=["false", "true"])
    elif kind == "enum":
        choices = []
        for choice in entry.get("choices") or spec["choices"]:
            value = str(catalog.normalize(name, choice))
            if value not in choices:
                choices.append(value)
        if baseline not in choices:
            choices.insert(0, baseline)
        agent.update(type="enum", choices=choices)
    else:
        low, high = entry.get("min"), entry.get("max")
        if low is None or high is None:
            raise KnobError(f"{name}: give min and max (the bounded agent's range)")
        if kind == "int":
            low, high = catalog.normalize(name, low), catalog.normalize(name, high)
            if low == spec["baseline"] or high == spec["baseline"]:
                raise KnobError(f"{name}: min and max must be numbers")
            if low > high:
                raise KnobError(f"{name}: min {low} is above max {high}")
        else:
            try:
                low = [int(x) for x in low]
                high = [int(x) for x in high]
            except (TypeError, ValueError):
                raise KnobError(f"{name}: min and max are two-number lists for a pair")
            if len(low) != 2 or len(high) != 2:
                raise KnobError(f"{name}: min and max are two-number lists for a pair")
            for i in range(2):
                if not spec["min"][i] <= low[i] <= high[i] <= spec["max"][i]:
                    raise KnobError(f"{name}: bounds {low}..{high} must be ordered and "
                                    f"within the catalog's {spec['min']}..{spec['max']}")
        agent.update(type=kind, min=low, max=high, allow_default=True)

    values = []
    for raw in entry.get("values") or []:
        if raw == "cpus" and facts.get("cpus"):
            raw = facts["cpus"]
        if catalog.is_off(name, raw):
            value = baseline
        elif kind == "bool":
            value = "true"
        elif kind == "enum":
            value = str(catalog.normalize(name, raw))
            if value not in agent["choices"]:
                raise KnobError(f"{name}: value {value!r} is not among the choices "
                                f"{agent['choices']}")
        elif kind == "int":
            value = _within(catalog, name, raw, agent["min"], agent["max"])
        else:
            value = catalog.normalize(name, raw)
            parts = [int(p) for p in value.split("/")]
            if not all(agent["min"][i] <= parts[i] <= agent["max"][i] for i in range(2)):
                raise KnobError(f"{name}: {value} is outside the planned bounds")
        if value != baseline:
            catalog.render({name: value}, software, facts)   # raises KnobError
        if value not in values:
            values.append(value)
    if not 2 <= len(values) <= MAX_VALUES:
        raise KnobError(f"{name}: give 2 to {MAX_VALUES} distinct grid values "
                        f"(got {values})")
    rationale = str(entry.get("rationale") or "").strip()
    if rationale:
        agent["description"] = f"{description}. On this hardware: {rationale}"
    return {"name": name, "values": values, "agent": agent, "rationale": rationale}


def build_space(catalog, software, facts, entries, max_parameters, grid_budget):
    """(parameters, errors) for a planned space. The coordinate descent's
    first pass (the start plus every other grid value once) must fit
    ``grid_budget`` measurements."""
    parameters, errors, seen = [], [], set()
    for entry in entries or []:
        if not isinstance(entry, dict):
            errors.append(f"each parameter must be an object, got {entry!r}")
            continue
        try:
            parameter = build_parameter(catalog, software, facts, entry)
        except KnobError as e:
            errors.append(str(e))
            continue
        if parameter["name"] in seen:
            errors.append(f"{parameter['name']}: listed twice")
            continue
        seen.add(parameter["name"])
        parameters.append(parameter)
    if not MIN_PARAMETERS <= len(entries or []) <= max_parameters:
        errors.append(f"give {MIN_PARAMETERS} to {max_parameters} parameters "
                      f"(got {len(entries or [])})")
    first_pass = 1 + sum(len(p["values"]) - 1 for p in parameters)
    if first_pass > grid_budget:
        errors.append(f"one coordinate-descent pass needs {first_pass} measurements; "
                      f"the grid budget is {grid_budget}. Drop values or parameters.")
    return parameters, errors


def value_check(catalog, software, facts):
    """``check`` for agent_tools.validate_configuration on a knob space."""
    def check(name, value):
        if not catalog.is_off(name, value):
            catalog.render({name: value}, software, facts)
    return check


def save(path, document):
    with open(path, "w") as f:
        f.write("# Search space planned by the campaign's plan_space step "
                "(knob_space.py).\n")
        yaml.safe_dump(document, f, sort_keys=False, width=100)


def load(path):
    with open(path) as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Planning session
# ---------------------------------------------------------------------------

PLAN_SYSTEM_PROMPT = """\
You are planning the next two searches of a performance-tuning campaign on an \
authoritative DNS name server ({software} on Linux). The goal is the highest \
max sustainable QPS: the highest query rate at which the server still answers \
nearly every query, measured by load generators on separate client machines.

An open-mode agent has already explored this server freely. Its results are \
below. Two narrower searches follow, both over the parameters you choose:
- a grid search by coordinate descent: it starts from every parameter's FIRST \
grid value, then sweeps one parameter at a time through its other grid values, \
keeping a change only if it beats the current point;
- a bounded agent search: another agent picks any values within your min/max \
bounds (or among your choices), using your grid values as suggestions.

Your job is to choose the parameters and their values so these searches spend \
their small budgets where this hardware can gain:
- Pick knobs that the open-mode results, attribution and other environments \
suggest matter, plus knobs whose best value is still uncertain. Leave out \
knobs that showed no effect here unless there is a hardware reason to retest.
- Make values plausible for THIS hardware. Use the facts and the read-only \
diagnostics (inspect_server): core and NUMA layout for worker counts, the \
NIC's maximum channels and ring sizes, memory for buffer sizes, which C-states \
exist, and so on. Do not propose values the hardware cannot take.
- Put the value the best open-mode configuration used FIRST, so the \
coordinate descent starts at the best known point and tests alternatives \
around it. Include the baseline (default/false) as a grid value when you want \
to know whether the knob matters at all here.
- Bounds should bracket the plausible optimum, not the catalog's whole range.

Measurements take a long time, so the budgets are small. Call inspect_server \
as needed, then call submit_search_space once. If it is rejected, fix the \
problems it lists and submit again.
"""


class SpacePlanTools:
    """Tools of the plan_space session: read-only server diagnostics and
    submit_search_space. ``report`` holds the accepted space."""

    def __init__(self, catalog, software, facts, context_text, static_parameters,
                 budgets, transcript, server, max_parameters=8):
        self.catalog = catalog
        self.software = software
        self.facts = facts or {}
        self.context_text = context_text
        self.static_parameters = static_parameters or []
        self.budgets = budgets
        self.transcript = transcript
        self.server = server
        self.max_parameters = max_parameters
        self.system_prompt = PLAN_SYSTEM_PROMPT.format(software=software)
        self.report = None
        self.stop_reason = None
        self._lock = threading.Lock()

    @property
    def done(self):
        return self.report is not None

    def _catalog_view(self):
        return [entry for entry in self.catalog.describe(self.software)
                if not entry.get("reboot")]

    def definitions(self):
        parameter = {
            "type": "object",
            "properties": {
                "knob": {"type": "string", "enum": [e["name"] for e in self._catalog_view()]},
                "values": {"type": "array", "minItems": 2, "maxItems": MAX_VALUES,
                           "items": {"type": ["string", "integer", "boolean"]},
                           "description": "Grid values, the starting value first. "
                                          "'default' (or false) is the baseline."},
                "min": {"description": "int knobs: lowest value the bounded agent may "
                                       "use; pair knobs: [first, second]"},
                "max": {"description": "int knobs: highest value; pair knobs: "
                                       "[first, second]"},
                "choices": {"type": "array", "items": {"type": "string"},
                            "description": "enum knobs: the choices the bounded agent "
                                           "may use (default: all)"},
                "rationale": {"type": "string",
                              "description": "Why this knob and these values on this "
                                             "hardware (shown to the bounded agent)"},
            },
            "required": ["knob", "values", "rationale"],
            "additionalProperties": False,
        }
        return [
            {
                "name": "inspect_server",
                "description": "Run a fixed read-only diagnostic on the name server. "
                               "Probes: " + "; ".join(
                                   f"{name}: {desc}"
                                   for name, (desc, _) in SERVER_PROBES.items()),
                "input_schema": {
                    "type": "object",
                    "properties": {"probe": {"type": "string", "enum": list(SERVER_PROBES)}},
                    "required": ["probe"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "submit_search_space",
                "description": "Submit the parameters for the grid and bounded "
                               f"searches ({MIN_PARAMETERS} to {self.max_parameters}). "
                               "Rejected submissions list what to fix.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "parameters": {"type": "array", "items": parameter},
                        "notes": {"type": "string",
                                  "description": "What you expect the searches to "
                                                 "find, and what you left out and why"},
                    },
                    "required": ["parameters", "notes"],
                    "additionalProperties": False,
                },
            },
        ]

    def initial_prompt(self):
        return (
            f"<hardware>\n{json.dumps(self.facts, indent=1, default=str)}\n</hardware>\n\n"
            f"{self.context_text}\n\n"
            "<current_static_parameters>\nThe fixed parameters in grid_search.yaml / "
            "agent_search.yaml (the same on every machine), for reference:\n"
            f"{json.dumps(self.static_parameters, default=str)}\n"
            "</current_static_parameters>\n\n"
            "<knob_catalog>\nKnobs you may use (catalog bounds; reboot knobs are "
            f"excluded):\n{json.dumps(self._catalog_view(), indent=1, default=str)}\n"
            "</knob_catalog>\n\n"
            f"<budgets>\n{json.dumps(self.budgets, indent=1)}\n</budgets>\n\n"
            "Plan the search space for this environment, then call submit_search_space."
        )

    def call(self, name, arguments):
        if not isinstance(arguments, dict):
            return "tool input must be a JSON object", True
        with self._lock:
            self.transcript.record("tool_call", tool=name, input=arguments)
            try:
                if name == "inspect_server":
                    result, is_error = self.inspect_server(**arguments), False
                elif name == "submit_search_space":
                    result, is_error = self.submit_search_space(**arguments), False
                else:
                    result, is_error = f"unknown tool {name!r}", True
            except TypeError as e:
                result, is_error = f"bad arguments for {name}: {e}", True
            except AgentError as e:
                result, is_error = str(e), True
            text = result if isinstance(result, str) else json.dumps(result, indent=1,
                                                                     default=str)
            text = _clip(text, MAX_TOOL_OUTPUT)
            self.transcript.record("tool_result", tool=name, is_error=is_error, text=text)
        return text, is_error

    def inspect_server(self, probe):
        if probe not in SERVER_PROBES:
            raise AgentError(f"unknown probe {probe!r}; choose from {list(SERVER_PROBES)}")
        try:
            result = ssh_run(self.server, SERVER_PROBES[probe][1], timeout=PROBE_TIMEOUT)
        except (subprocess.TimeoutExpired, OSError) as e:
            raise AgentError(f"probe {probe} failed: {e}")
        return f"[{probe}, rc={result.returncode}]\n{result.stdout or ''}{result.stderr or ''}"

    def submit_search_space(self, parameters, notes):
        if self.report is not None:
            return "already submitted"
        built, errors = build_space(self.catalog, self.software, self.facts, parameters,
                                    self.max_parameters, self.budgets["grid_measurements"])
        if errors:
            raise AgentError("search space rejected:\n- " + "\n- ".join(errors))
        self.report = {"software": self.software, "notes": notes, "parameters": built}
        log.info("Planned search space: %s", ", ".join(
            f"{p['name']}={p['values']}" for p in built))
        return "Search space accepted. End your turn now."
