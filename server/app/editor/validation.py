# SPDX-FileCopyrightText: PyPSA Contributors
# SPDX-License-Identifier: MIT
"""Located checks supplement native consistency; validity is not feasibility."""

import math

import numpy as np
import pandas as pd

from app.editor import metadata, policy
from app.editor.catalog import field_state, has_piecewise
from app.editor.schemas import Diagnostic


def validate(n, analysis):
    from app import network

    diagnostics = []

    def add(code, message, kind=None, name=None, field=None, severity="error"):
        diagnostics.append(
            Diagnostic(
                severity=severity,
                code=code,
                message=message,
                component=kind,
                name=str(name) if name is not None else None,
                field=field,
            )
        )

    for c in n.components:
        if c.name in policy.READ_ONLY_TYPES:
            continue
        definition = metadata.definition(n, c.name)
        for name, row in c.static.iterrows():
            asset = row.to_dict()
            asset["name"] = name
            context = {"analysis": analysis, "multi_investment": False}
            for key, field in definition.fields.items():
                if field.role == "output" or field.support in {
                    "placeholder",
                    "deprecated",
                }:
                    continue
                state = field_state(field, asset, context)
                if not state.applicable or state.overridden:
                    continue
                value = asset.get(key)
                if field.data_type == "string" and state.required and not value:
                    add(
                        "required",
                        "Required reference/value is empty.",
                        c.name,
                        name,
                        key,
                    )
                enum = policy.ENUMS.get(field.choice_set)
                if enum and value not in enum:
                    add("choice", f"Expected one of {enum}.", c.name, name, key)
                if (
                    state.required
                    and isinstance(value, (float, np.floating))
                    and math.isnan(value)
                ):
                    add("required", "Required value is unset.", c.name, name, key)
                if has_piecewise(c, key, name):
                    add(
                        "piecewise-preserved",
                        "Imported piecewise values are retained read-only.",
                        c.name,
                        name,
                        key,
                        "info",
                    )
                elif field.static and not (key in c.dynamic and name in c.dynamic[key]):
                    try:
                        metadata.scalar_value(
                            c.defaults.loc[key],
                            value.item() if isinstance(value, np.generic) else value,
                        )
                        if (field.minimum is not None and value < field.minimum) or (
                            field.maximum is not None and value > field.maximum
                        ):
                            raise ValueError("Value is outside declared bounds.")
                    except (ValueError, TypeError) as exc:
                        add("field-value", str(exc), c.name, name, key)
            for port in definition.ports:
                bus = row.get(port.attribute, "")
                if bus and bus not in n.buses.index:
                    add(
                        "unknown-bus",
                        f"Unknown bus: {bus}",
                        c.name,
                        name,
                        port.attribute,
                    )
            carrier = row.get("carrier", "")
            if carrier and carrier not in n.carriers.index:
                add(
                    "unregistered-carrier",
                    f"Carrier {carrier!r} is not registered.",
                    c.name,
                    name,
                    "carrier",
                    "warning",
                )
            if c.name in {"Line", "Transformer"}:
                standard = row.type
                if (
                    standard
                    and standard not in n.components[f"{c.name}Type"].static.index
                ):
                    add("standard-type", "Unknown standard type.", c.name, name, "type")
                if standard and (
                    row.num_parallel <= 0 or (c.name == "Line" and row.length <= 0)
                ):
                    add(
                        "standard-parameters",
                        "Standard types require positive parallel count and Line length.",
                        c.name,
                        name,
                        "type",
                    )
                if c.name == "Transformer" and not standard and row.s_nom <= 0:
                    add(
                        "transformer-rating",
                        "Manual transformer impedance requires a positive s_nom base.",
                        c.name,
                        name,
                        "s_nom",
                    )
                if row.bus0 in n.buses.index and row.bus1 in n.buses.index:
                    b0, b1 = n.buses.loc[row.bus0], n.buses.loc[row.bus1]
                    if row.bus0 == row.bus1:
                        add(
                            "distinct-endpoints",
                            "Electrical branch endpoints must differ.",
                            c.name,
                            name,
                            "bus1",
                        )
                    if (
                        b0.carrier not in {"AC", "DC"}
                        or b0.carrier != b1.carrier
                        or (c.name == "Transformer" and b0.carrier != "AC")
                    ):
                        add(
                            "electrical-domain",
                            "Incompatible electrical bus carriers.",
                            c.name,
                            name,
                            "bus1",
                        )
                    if c.name == "Line" and not np.isclose(b0.v_nom, b1.v_nom):
                        add(
                            "line-voltage",
                            "Line endpoint voltages must match; use a Transformer.",
                            c.name,
                            name,
                            "bus1",
                        )
                    if not standard:
                        bad = (
                            row.r == 0 and row.x == 0
                            if analysis == "pf"
                            else (row.r == 0 if b0.carrier == "DC" else row.x == 0)
                        )
                        if bad:
                            add(
                                "branch-impedance",
                                "Nonzero impedance is required for this analysis.",
                                c.name,
                                name,
                                "r" if b0.carrier == "DC" else "x",
                            )
            for nominal in ("p_nom", "e_nom", "s_nom"):
                if nominal not in row:
                    continue
                if (
                    row[nominal] < 0
                    or row[f"{nominal}_min"] < 0
                    or row[f"{nominal}_max"] < row[f"{nominal}_min"]
                ):
                    add(
                        "capacity-bounds",
                        "Capacity/bounds must be nonnegative and ordered.",
                        c.name,
                        name,
                        nominal,
                    )
                if analysis == "optimize" and row.get(f"{nominal}_extendable", False):
                    fixed = row.get(f"{nominal}_set", float("nan"))
                    if (
                        pd.notna(fixed)
                        and not row[f"{nominal}_min"] <= fixed <= row[f"{nominal}_max"]
                    ):
                        add(
                            "capacity-set",
                            "Fixed optimized capacity is outside its bounds.",
                            c.name,
                            name,
                            f"{nominal}_set",
                        )
            if c.name == "Bus" and (not math.isfinite(row.v_nom) or row.v_nom <= 0):
                add(
                    "voltage",
                    "Nominal voltage must be finite and positive.",
                    c.name,
                    name,
                    "v_nom",
                )
            if analysis == "optimize" and pd.notna(
                row.get("overnight_cost", float("nan"))
            ):
                if not math.isfinite(row.lifetime) or row.lifetime <= 0:
                    add(
                        "cost-lifetime",
                        "Overnight costs require a positive finite lifetime.",
                        c.name,
                        name,
                        "lifetime",
                    )
                if pd.isna(row.discount_rate) or row.discount_rate < 0:
                    add(
                        "cost-discount",
                        "Overnight costs require a nonnegative discount rate.",
                        c.name,
                        name,
                        "discount_rate",
                    )
            if (
                c.name == "GlobalConstraint"
                and analysis == "optimize"
                and row.type == "primary_energy"
                and row.carrier_attribute not in n.carriers.columns
            ):
                add(
                    "carrier-attribute",
                    "Constraint carrier attribute is not a Carrier column.",
                    c.name,
                    name,
                    "carrier_attribute",
                )
            if (
                c.name == "GlobalConstraint"
                and analysis == "optimize"
                and row.type == "tech_capacity_expansion_limit"
                and row.bus
                and row.bus not in n.buses.index
            ):
                add(
                    "constraint-bus",
                    "Constraint bus filter references an unknown bus.",
                    c.name,
                    name,
                    "bus",
                )
        for key, frame in c.dynamic.items():
            if (
                key not in c.defaults.index
                or str(c.defaults.at[key, "status"]).strip().startswith("Output")
                or frame.empty
            ):
                continue
            if not frame.index.equals(n.snapshots):
                add(
                    "profile-alignment",
                    "Profile index must match snapshots.",
                    c.name,
                    field=key,
                )
            for name in frame.columns:
                if name not in c.static.index:
                    add(
                        "profile-asset",
                        "Profile has no corresponding asset.",
                        c.name,
                        name,
                        key,
                    )
                if frame[name].isna().any() and not pd.isna(
                    c.defaults.at[key, "default"]
                ):
                    add(
                        "profile-missing",
                        "Profile cannot contain missing values for this field.",
                        c.name,
                        name,
                        key,
                    )
                if np.isinf(frame[name].to_numpy(dtype=float)).any():
                    add(
                        "profile-infinity",
                        "Profile values must be finite or an allowed unset.",
                        c.name,
                        name,
                        key,
                    )
                low, high = policy.bounds_for(c.name, key)
                if (low is not None and (frame[name] < low).any()) or (
                    high is not None and (frame[name] > high).any()
                ):
                    add(
                        "profile-bounds",
                        "Profile exceeds declared field bounds.",
                        c.name,
                        name,
                        key,
                    )
    # Run on a copy: native checks may derive topology or parameters.
    if not any(d.severity == "error" for d in diagnostics):
        native = network.validate(n.copy())
        for item in native["diagnostics"]:
            diagnostics.append(
                Diagnostic(
                    severity="error" if item["level"] == "error" else "warning",
                    code="native-consistency",
                    source="pypsa",
                    message=item["message"],
                )
            )
        if not native["valid"] and not any(d.severity == "error" for d in diagnostics):
            add("native-invalid", "Native consistency validation failed.")
    return diagnostics
