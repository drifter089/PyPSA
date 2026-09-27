# SPDX-FileCopyrightText: PyPSA Contributors
# SPDX-License-Identifier: MIT
"""Assemble native definitions, choices, and bounded instance form state."""

import hashlib
import json

import pandas as pd
import pypsa

from app.editor import metadata, policy
from app.editor.presets import PRESETS
from app.editor.schemas import CatalogResponse, FieldState, InstanceState


def source_id(source):
    if source is not None and source.kind == "netcdf":
        import base64

        return hashlib.sha256(base64.b64decode(source.data, validate=True)).hexdigest()
    data = source.model_dump(mode="json") if source is not None else {"empty": True}
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def field_state(field, asset, context, mode="scalar"):
    applicable, overridden, required = True, False, field.native_required
    reasons = []
    for rule in field.rules:
        result = policy.evaluate(rule.when, asset, context)
        if rule.effect == "applicable" and not result:
            applicable = False
            reasons.append(rule.id)
        elif rule.effect == "overridden" and result:
            overridden = True
            reasons.append(rule.id)
        elif rule.effect == "required" and result:
            required = True
    return FieldState(
        applicable=applicable,
        overridden=overridden,
        editable=field.support == "editable"
        and not field.create_only
        and applicable
        and not overridden
        and mode != "piecewise",
        required=required and applicable and not overridden,
        reasons=reasons,
        value_mode=mode,
        matches_default=metadata.encode(asset.get(field.key)) == field.default,
    )


def has_piecewise(c, field, name):
    table = c.piecewise.get(field)
    return (
        table is not None
        and not table.empty
        and name in table.columns.get_level_values(0)
    )


def instances(n, definitions, request):
    result = []
    selected = [
        (kind, name)
        for kind in definitions
        for name in n.components[kind].static.index
        if request.names is None or name in request.names
    ]
    for kind, name in selected[request.offset : request.offset + request.limit]:
        c = n.components[kind]
        asset = c.static.loc[name].to_dict()
        asset["name"] = str(name)
        states, profiles = {}, {}
        for key, field in definitions[kind].fields.items():
            mode = "scalar"
            if key in c.dynamic and name in c.dynamic[key].columns:
                mode = "profile"
                series = c.dynamic[key][name]
                profiles[key] = {
                    "snapshot_id": metadata.snapshot_id(n),
                    "length": len(series),
                    "missing": int(series.isna().sum()),
                }
                if request.include_profiles:
                    profiles[key]["values"] = metadata.encode(series.to_list())
            if has_piecewise(c, key, name):
                mode = "piecewise"
                profiles[key] = {"mode": "piecewise", "editable": False}
            states[key] = field_state(
                field,
                asset,
                {"analysis": request.analysis, "multi_investment": False},
                mode,
            )
        ports = []
        for port in definitions[kind].ports:
            bus = str(asset.get(port.attribute, ""))
            ports.append(
                {
                    **port.model_dump(mode="json"),
                    "bus": bus,
                    "connected": bool(bus),
                    "enabled": port.required or bool(bus),
                }
            )
        result.append(
            InstanceState(
                component=kind,
                name=str(name),
                static=metadata.encode(asset),
                fields=states,
                profiles=profiles,
                ports=ports,
            )
        )
    return result, len(selected)


def choice_sets(n):
    choices = {
        key: {
            "closed": True,
            "options": [{"value": value, "label": str(value)} for value in values],
        }
        for key, values in policy.ENUMS.items()
    }
    choices["buses"] = {
        "closed": True,
        "options": [
            {
                "value": str(name),
                "label": str(name),
                "carrier": str(row.carrier),
                "v_nom": metadata.encode(row.v_nom),
                "unit": metadata.encode(row.unit),
            }
            for name, row in n.buses.iterrows()
        ],
    }
    usage = {}
    for c in n.components:
        if "carrier" not in c.static:
            continue
        for value in c.static.carrier.dropna().unique():
            if value:
                usage.setdefault(str(value), []).append(c.name)
    carriers = sorted(set(n.carriers.index) | set(usage))
    choices["carriers"] = {
        "closed": False,
        "create_component": "Carrier",
        "options": [
            {
                "value": str(value),
                "label": str(n.carriers.at[value, "nice_name"] or value)
                if value in n.carriers.index
                else str(value),
                "color": str(n.carriers.at[value, "color"])
                if value in n.carriers.index
                else "",
                "registered": value in n.carriers.index,
                "used_by": usage.get(str(value), []),
            }
            for value in carriers
        ],
    }
    for kind in ["LineType", "TransformerType"]:
        # Registry records are supplied with all native standard-type attributes.
        choices[kind] = {
            "closed": True,
            "options": [
                {
                    "value": str(name),
                    "label": str(name),
                    "attributes": metadata.encode(row.to_dict()),
                }
                for name, row in n.components[kind].static.iterrows()
            ],
        }
    choices["constraint_carriers"] = {
        "depends_on": "asset.type",
        "by_type": {
            "primary_energy": {
                "closed": True,
                "options": [
                    {"value": str(key), "label": str(key)}
                    for key in n.carriers.select_dtypes(include="number").columns
                ],
            },
            "operational_limit": {
                "closed": False,
                "options": choices["carriers"]["options"],
            },
            "tech_capacity_expansion_limit": {
                "closed": False,
                "options": choices["carriers"]["options"],
            },
            "transmission_volume_expansion_limit": {
                "closed": False,
                "options": [],
                "description": "Comma-separated branch carriers.",
            },
            "transmission_expansion_cost_limit": {
                "closed": False,
                "options": [],
                "description": "Comma-separated branch carriers.",
            },
        },
    }
    return choices


def describe(n, request, identity, limits, diagnostics=()):
    available = n.all_components
    kinds = request.components if request.components is not None else sorted(available)
    if set(kinds) - available:
        raise ValueError(f"Unknown component types: {sorted(set(kinds) - available)}")
    definitions = {kind: metadata.definition(n, kind) for kind in kinds}
    states, total = instances(n, definitions, request)
    return CatalogResponse(
        pypsa_version=pypsa.__version__,
        software=metadata.software(),
        context={
            "source_id": identity,
            "analysis": request.analysis,
            "snapshot_id": metadata.snapshot_id(n),
            "snapshots": metadata.snapshot_labels(n),
            "snapshot_kind": "datetime"
            if isinstance(n.snapshots, pd.DatetimeIndex)
            else "labels",
            "snapshot_weightings": metadata.encode(
                n.snapshot_weightings.to_dict(orient="list")
            ),
            "capabilities": {
                "analyses": ["optimize", "lpf", "pf"],
                "multi_investment": False,
                "scenarios": False,
                "piecewise_edit": False,
                "rename": False,
                "custom_standard_types": False,
                "additional_ports": True,
                "max_components": limits["max_components"],
                "max_snapshots": limits["max_snapshots"],
                "max_cells": limits["max_cells"],
            },
            "offset": request.offset,
            "limit": request.limit,
            "solution_state": n.meta.get(
                "editor_solution_state", "unknown_or_imported"
            ),
        },
        component_types=definitions,
        network_forms={
            "snapshots": {
                "operation": "set_snapshots",
                "requires_no_profiles_for_axis_change": True,
                "weightings": ["objective", "stores", "generators"],
                "description": "Explicit snapshot axis and independent native weightings. Axis changes with profiles must be staged explicitly.",
            },
            "analysis": {"choices": ["optimize", "lpf", "pf"], "mutates_assets": False},
            "constraints": {"component": "GlobalConstraint"},
            "registries": ["Carrier", "LineType", "TransformerType"],
        },
        choice_sets=choice_sets(n),
        connection_rules=[
            {
                "id": "asset-to-bus",
                "action": "set_bus_reference",
                "asset_port_max_connections": 1,
                "bus_max_connections": None,
            },
            {
                "id": "bus-to-bus",
                "action": "create_branch",
                "choices": ["Line", "Transformer", "Link", "Process"],
            },
            {"id": "no-direct-asset-wire", "action": "reject"},
            {
                "id": "electrical-endpoints",
                "evaluation": "server",
                "source": "editor-policy",
                "description": "Distinct compatible electrical endpoints; Line voltage must match, Transformer requires AC.",
            },
            {
                "id": "topology",
                "cycles": True,
                "parallel_branches": True,
                "carrier_string_equality": False,
            },
        ],
        presets=PRESETS,
        instances=states,
        total_instances=total,
        diagnostics=list(diagnostics),
    )
