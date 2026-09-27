# SPDX-FileCopyrightText: PyPSA Contributors
# SPDX-License-Identifier: MIT
"""Native, request-local transactions; no solving and no reduced-model rebuild."""

import math

import pandas as pd

from app.editor import metadata, policy, presets
from app.editor.catalog import field_state, has_piecewise
from app.editor.schemas import (
    AddPort,
    ApplyPreset,
    Connect,
    Create,
    Delete,
    Diagnostic,
    Disconnect,
    RemovePort,
    Reset,
    SetProfile,
    SetSnapshots,
    SetValue,
)


def clear_binding(c, name, field):
    if field in c.dynamic:
        c.dynamic[field] = c.dynamic[field].drop(columns=[name], errors="ignore")
    if has_piecewise(c, field, name):
        frame = c.piecewise[field]
        c.piecewise[field] = frame.loc[:, frame.columns.get_level_values(0) != name]


def check_field(
    n, kind, name, key, value, analysis, asset=None, reset=False, definition=None
):
    definition = definition or metadata.definition(n, kind)
    if key not in definition.fields:
        raise ValueError(f"Unknown attribute: {kind}.{key}")
    field = definition.fields[key]
    if field.support != "editable" or key == "name":
        raise ValueError(f"Attribute is not editable: {kind}.{key} ({field.support})")
    c = n.components[kind]
    asset = c.static.loc[name].to_dict() if asset is None else asset
    state = field_state(field, asset, {"analysis": analysis, "multi_investment": False})
    if not state.editable:
        raise ValueError(
            f"Attribute is inactive or overridden: {kind}.{key}; {state.reasons}"
        )
    if has_piecewise(c, key, name) and not reset:
        raise ValueError(
            "Reset imported piecewise data explicitly before replacing it."
        )
    result = metadata.scalar_value(c.defaults.loc[key], value)
    if (field.minimum is not None and result < field.minimum) or (
        field.maximum is not None and result > field.maximum
    ):
        raise ValueError(f"Value is outside declared bounds for {key}.")
    enum = policy.ENUMS.get(field.choice_set)
    if enum and result not in enum:
        raise ValueError(f"Expected one of {enum} for {key}.")
    return result


def create(n, operation, analysis):
    c = n.components[operation.component]
    if operation.name in c.static.index:
        raise ValueError("Asset already exists.")
    if c.name in policy.READ_ONLY_TYPES:
        raise ValueError(f"Creation of {c.name} is not supported.")
    metadata.expand_ports(n, c.name, operation.attributes)
    c = n.components[c.name]
    asset = c.defaults.default.to_dict()
    asset.update(
        {key: metadata.decode(value) for key, value in operation.attributes.items()}
    )
    definition = metadata.definition(n, c.name)
    attrs = {
        key: check_field(
            n,
            c.name,
            operation.name,
            key,
            value,
            analysis,
            asset=asset,
            definition=definition,
        )
        for key, value in operation.attributes.items()
    }
    n.add(c.name, operation.name, **attrs)


def set_snapshots(n, operation, limits):
    from app.schemas import NetworkModel

    # Reuse the existing weighting/uniqueness validation.
    NetworkModel(
        snapshots=operation.snapshots,
        snapshot_kind=operation.snapshot_kind,
        snapshot_weightings=operation.weightings,
    )
    if len(operation.snapshots) > limits["max_snapshots"]:
        raise ValueError("Snapshot limit exceeded.")
    snapshots = pd.Index(operation.snapshots, name="snapshot")
    if operation.snapshot_kind == "datetime":
        snapshots = pd.DatetimeIndex(
            pd.to_datetime(operation.snapshots), name="snapshot"
        )
        if (
            snapshots.tz is not None
            or not snapshots.is_unique
            or not snapshots.is_monotonic_increasing
        ):
            raise ValueError(
                "Datetime snapshots must be unique, increasing, and timezone-naive."
            )
    if not n.snapshots.equals(snapshots):
        for c in n.components:
            if any(
                not frame.empty
                for key, frame in c.dynamic.items()
                if key in c.defaults.index
                and str(c.defaults.at[key, "status"]).strip().startswith("Input")
            ):
                raise ValueError(
                    "Replace existing input profiles with scalars before changing the snapshot axis."
                )
        count = sum(
            len(c.static)
            for c in n.components
            if c.name not in {"LineType", "TransformerType"}
        )
        if count * len(snapshots) > limits["max_cells"]:
            raise ValueError("Snapshot change exceeds the cell limit.")
        n.set_snapshots(snapshots)
    for key, values in operation.weightings.items():
        if any(not math.isfinite(value) for value in values):
            raise ValueError("Snapshot weights must be finite.")
        n.snapshot_weightings[key] = values


def apply_one(n, operation, analysis, limits):
    if isinstance(operation, SetSnapshots):
        set_snapshots(n, operation, limits)
        return
    if operation.component not in n.all_components:
        raise ValueError("Unknown component type.")
    c = n.components[operation.component]
    if c.name in policy.READ_ONLY_TYPES:
        raise ValueError(f"{c.name} is read-only.")
    if isinstance(operation, Create):
        create(n, operation, analysis)
        return
    if operation.name not in c.static.index:
        raise ValueError("Asset does not exist.")
    name = operation.name
    if isinstance(operation, Delete):
        # References are checked after the complete batch, allowing explicit
        # cascades in either order. Never silently delete other assets.
        n.remove(c.name, name)
    elif isinstance(operation, (Connect, Disconnect)):
        if operation.port not in metadata.physical_ports(n, c.name):
            raise ValueError("This is not an enabled physical port.")
        bus = operation.bus if isinstance(operation, Connect) else ""
        if bus and bus not in n.buses.index:
            raise ValueError("Target bus does not exist.")
        c.static.at[name, operation.port] = bus
    elif isinstance(operation, AddPort):
        if c.name not in {"Link", "Process"}:
            raise ValueError("Only Link/Process support additional ports.")
        if operation.bus not in n.buses.index:
            raise ValueError("Target bus does not exist.")
        if analysis != "optimize" and (
            operation.delay != 0 or not operation.cyclic_delay
        ):
            raise ValueError("Transport delay configuration requires optimization.")
        key = f"bus{operation.index}"
        if key in c.static and c.static.at[name, key]:
            raise ValueError("Port is already connected; reconnect it explicitly.")
        metadata.expand_ports(n, c.name, [key])
        coefficient = (
            f"efficiency{operation.index}"
            if c.name == "Link"
            else f"rate{operation.index}"
        )
        c.static.at[name, key] = operation.bus
        for field, value in {
            coefficient: operation.coefficient,
            f"delay{operation.index}": operation.delay,
            f"cyclic_delay{operation.index}": operation.cyclic_delay,
        }.items():
            if analysis != "optimize" and field != coefficient:
                continue
            result = check_field(n, c.name, name, field, value, analysis)
            clear_binding(c, name, field)
            c.static.at[name, field] = result
    elif isinstance(operation, RemovePort):
        key = f"bus{operation.index}"
        if c.name not in {"Link", "Process"} or key not in c.static:
            raise ValueError("Additional port does not exist.")
        families = [
            "bus",
            "rate" if c.name == "Process" else "efficiency",
            "delay",
            "cyclic_delay",
            "p",
        ]
        for family in families:
            field = f"{family}{operation.index}"
            clear_binding(c, name, field)
            if field in c.static:
                c.static.at[name, field] = c.defaults.at[field, "default"]
    elif isinstance(operation, SetProfile):
        if operation.snapshot_id != metadata.snapshot_id(n):
            raise ValueError("Profile snapshot identity is stale.")
        if len(operation.values) != len(n.snapshots):
            raise ValueError("Profile length must match snapshots.")
        if (
            operation.field not in c.defaults.index
            or not c.defaults.at[operation.field, "varying"]
        ):
            raise ValueError("This attribute does not accept time series.")
        definition = metadata.definition(n, c.name)
        values = [
            check_field(
                n, c.name, name, operation.field, value, analysis, definition=definition
            )
            for value in operation.values
        ]
        if any(isinstance(v, float) and math.isinf(v) for v in values):
            raise ValueError("Profiles may not contain infinity.")
        clear_binding(c, name, operation.field)
        c.dynamic[operation.field][name] = pd.Series(values, index=n.snapshots)
    elif isinstance(operation, (SetValue, Reset)):
        field = operation.field
        if field not in c.defaults.index:
            raise ValueError(
                "Unknown field. Add additional ports explicitly before setting their fields."
            )
        reset = isinstance(operation, Reset)
        value = c.defaults.at[field, "default"] if reset else operation.value
        # Normalize numpy defaults through the same scalar validator.
        if reset and hasattr(value, "item"):
            value = value.item()
        result = check_field(n, c.name, name, field, value, analysis, reset=reset)
        if not c.defaults.at[field, "static"]:
            raise ValueError("This field accepts only a profile.")
        clear_binding(c, name, field)
        c.static.at[name, field] = result


def apply(n, operations, analysis, limits):
    from app import network

    diagnostics, extensions = [], []
    for index, operation in enumerate(operations):
        try:
            expanded = (
                presets.expand(operation, n)
                if isinstance(operation, ApplyPreset)
                else [operation]
            )
            for item in expanded:
                apply_one(n, item, analysis, limits)
                network.bounded(n, limits)
            if isinstance(operation, ApplyPreset):
                extensions.append(
                    {
                        "preset": operation.preset,
                        "version": 1,
                        "name": operation.name,
                        "buses": operation.buses,
                        "assets": [
                            {"component": item.component, "name": item.name}
                            for item in expanded
                        ],
                    }
                )
        except (ValueError, TypeError, KeyError) as exc:
            diagnostics.append(
                Diagnostic(
                    severity="error",
                    code="edit-rejected",
                    message=str(exc),
                    operation=index,
                    component=getattr(operation, "component", None),
                    name=getattr(operation, "name", None),
                    field=getattr(operation, "field", getattr(operation, "port", None)),
                )
            )
            break
    return diagnostics, extensions


def invalidate_solution(n):
    """Restore output defaults rather than delete required native columns."""
    for c in n.components:
        for field, row in c.defaults.iterrows():
            if not str(row.status).strip().startswith("Output"):
                continue
            if field in c.static:
                c.static[field] = row.default
            if field in c.dynamic:
                c.dynamic[field] = c.dynamic[field].iloc[:, :0]
        if c.name == "SubNetwork" and len(c.static):
            n.remove("SubNetwork", c.static.index)
    # Native export serializes these network-level optimization attributes.
    n._model = None
    n._objective = None
    n._objective_constant = None
    n._optimize_window = None
    n.meta = {**n.meta, "editor_solution_state": "invalidated"}
