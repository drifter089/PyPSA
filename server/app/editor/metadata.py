# SPDX-FileCopyrightText: PyPSA Contributors
# SPDX-License-Identifier: MIT
"""Native extraction, tagged JSON values, and the isolated multiport bridge."""

import hashlib
import math
import re
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd
import pypsa

from app.editor import policy
from app.editor.schemas import (
    ComponentDefinition,
    FieldDefinition,
    PortDefinition,
    SpecialValue,
)

PORT_FIELD = re.compile(r"(bus|efficiency|rate|delay|cyclic_delay)([0-9]+)$")


def encode(value):
    if isinstance(value, dict):
        return {str(k): encode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, np.ndarray)):
        return [encode(v) for v in value]
    if isinstance(value, np.generic):
        value = value.item()
    if value is None or value is pd.NA or value is pd.NaT:
        return {"kind": "unset"}
    if isinstance(value, float):
        if math.isnan(value):
            return {"kind": "unset"}
        if math.isinf(value):
            return {"kind": "positiveInfinity" if value > 0 else "negativeInfinity"}
    if isinstance(value, (str, bool, int, float)):
        return value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def decode(value):
    if isinstance(value, SpecialValue):
        return {
            "unset": float("nan"),
            "positiveInfinity": float("inf"),
            "negativeInfinity": -float("inf"),
        }[value.kind]
    return value


def snapshot_labels(n):
    return [
        str(v) if not isinstance(v, pd.Timestamp) else v.isoformat()
        for v in n.snapshots
    ]


def snapshot_id(n):
    import json

    return hashlib.sha256(json.dumps(snapshot_labels(n)).encode()).hexdigest()


@lru_cache(maxsize=1)
def software():
    root = Path(pypsa.__file__).parent
    digest = hashlib.sha256()
    # Captures local fork changes even when distribution version tags are missing.
    for path in sorted(list(root.rglob("*.py")) + list(root.rglob("*.csv"))):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    adapter = hashlib.sha256()
    for path in sorted(Path(__file__).parent.glob("*.py")):
        adapter.update(path.name.encode())
        adapter.update(path.read_bytes())
    return {
        "pypsa_version": pypsa.__version__,
        "source_digest": digest.hexdigest(),
        "adapter_version": "1",
        "adapter_digest": adapter.hexdigest(),
        "policy_version": policy.VERSION,
    }


def expand_ports(n, kind, attributes):
    """Version-pinned bridge to PyPSA's own port-definition expansion.

    Do not reproduce native defaults/types. The native helper is private and its
    compatibility is covered by port-10 and per-family round-trip tests.
    """
    from pypsa.descriptors import _update_ports_component_attrs

    if kind not in {"Link", "Process"}:
        return
    allowed = {
        "bus",
        "delay",
        "cyclic_delay",
        "efficiency" if kind == "Link" else "rate",
    }
    ports = set()
    for key in attributes:
        match = PORT_FIELD.fullmatch(key)
        if not match:
            continue
        family, index = match.groups()
        if family not in allowed or index != str(int(index)):
            raise ValueError(f"Unsupported port attribute: {kind}.{key}")
        if int(index) > 999:
            raise ValueError("Port index must be at most 999.")
        if int(index) >= 2:
            ports.add(f"bus{index}")
    if ports:
        _update_ports_component_attrs(n, where=sorted(ports), c_name=kind)
        c = n.components[kind]
        for key, row in c.defaults.iterrows():
            if row.static and key != "name" and key not in c.static:
                c.static[key] = row.default


def physical_ports(n, kind):
    if kind not in n.one_port_components | n.branch_components:
        return []
    return [f"bus{port}" for port in n.components[kind].ports]


def choice_key(kind, field):
    if kind == "GlobalConstraint" and field == "carrier_attribute":
        return "constraint_carriers"
    if f"{kind}.{field}" in policy.ENUMS:
        return f"{kind}.{field}"
    if field == "control":
        return "control"
    if re.fullmatch(r"bus\d*", field):
        return "buses"
    if field == "carrier":
        return "carriers"
    if field == "type" and kind in {"Line", "Transformer"}:
        return f"{kind}Type"
    return None


def definition(n, kind):
    c = n.components[kind]
    fields = {}
    for key, row in c.defaults.iterrows():
        status = str(row.status).strip()
        role = "output" if status.startswith("Output") else "input"
        support = "editable"
        if role == "output":
            support = "output"
        elif (kind, key) in policy.PLACEHOLDERS:
            support = "placeholder"
        elif (kind, key) in policy.DEPRECATED:
            support = "deprecated"
        elif (
            kind in policy.READ_ONLY_TYPES
            or (kind, key) in policy.UNREVIEWED
            or key.endswith("_per_period")
            or key == "investment_period"
        ):
            support = "read_only"
        native_type = str(row.type)
        modes = (["scalar"] if row.static else []) + (
            ["profile"] if row.varying else []
        )
        if "piecewise" in native_type:
            modes.append("piecewise")
        data_type = {
            "boolean": "boolean",
            "int": "integer",
            "string": "string",
            "geometry": "geometry",
        }.get(native_type, "number")
        choices = choice_key(kind, key)
        fields[key] = FieldDefinition(
            key=key,
            native_type=native_type,
            data_type=data_type,
            unit=None if pd.isna(row.unit) else str(row.unit),
            default=encode(row.default),
            description=str(row.description),
            native_status=status,
            role=role,
            native_required="required" in status,
            static=bool(row.static),
            varying=bool(row.varying),
            native_value_modes=modes,
            editor_value_modes=[m for m in modes if m != "piecewise"]
            if support == "editable"
            else [],
            support=support,
            group=policy.group_for(key),
            control="select"
            if choices
            else "toggle"
            if data_type == "boolean"
            else data_type,
            choice_set=choices,
            minimum=policy.bounds_for(kind, key)[0],
            maximum=policy.bounds_for(kind, key)[1],
            create_only=key == "name",
            rules=policy.rules_for(kind, key, c.defaults.index),
            source=f"pypsa/data/component_attrs/{c.list_name}.csv#{key}",
        )
    policy.validate_rules(fields)
    ports = []
    for field in physical_ports(n, kind):
        index = field[3:]
        coefficient = delay = cyclic_delay = None
        role = "Native bus reference; actual flow may be bidirectional."
        if kind == "Process":
            coefficient, delay, cyclic_delay = (
                f"rate{index}",
                f"delay{index}",
                f"cyclic_delay{index}",
            )
            role = "Positive rate supplies this bus for positive internal dispatch."
        elif kind == "Link":
            role = (
                "Reference input for positive dispatch."
                if index == "0"
                else "Coefficient determines supply/withdrawal for positive dispatch."
            )
            if index != "0":
                suffix = "" if index == "1" else index
                coefficient, delay, cyclic_delay = (
                    f"efficiency{suffix}",
                    f"delay{suffix}",
                    f"cyclic_delay{suffix}",
                )
        ports.append(
            PortDefinition(
                attribute=field,
                required=fields[field].native_required,
                coefficient=coefficient,
                delay=delay,
                cyclic_delay=cyclic_delay,
                reference_role=role,
            )
        )
    return ComponentDefinition(
        name=kind,
        list_name=c.list_name,
        category=None if pd.isna(c.ctype.category) else str(c.ctype.category),
        description=c.ctype.description,
        create=kind not in policy.READ_ONLY_TYPES,
        fields=fields,
        ports=ports,
        additional_ports=kind in {"Link", "Process"},
        groups=list(dict.fromkeys(f.group for f in fields.values())),
    )


def scalar_value(row, value):
    """Strict semantic dtype checking; do not let pandas coerce bad user input."""
    value = decode(value)
    if row.type == "boolean":
        if type(value) is not bool:
            raise ValueError("Expected a boolean.")
    elif row.type == "string":
        if not isinstance(value, str):
            raise ValueError("Expected a string.")
    elif row.type == "int":
        if type(value) is not int:
            raise ValueError("Expected an integer.")
    elif row.type == "geometry":
        raise ValueError("Geometry editing is not supported.")
    else:
        if type(value) not in {int, float}:
            raise ValueError("Expected a number or an allowed special value.")
        if not math.isfinite(value):
            default = row.default
            if math.isnan(value):
                if not isinstance(default, (float, np.floating)) or not math.isnan(
                    default
                ):
                    raise ValueError("This attribute does not allow unset values.")
            elif (
                not isinstance(default, (float, np.floating))
                or not math.isinf(default)
                or value != default
            ):
                raise ValueError("This attribute does not allow this infinity value.")
    return value
