# SPDX-FileCopyrightText: PyPSA Contributors
# SPDX-License-Identifier: MIT
"""Explicit native recipes, with user-supplied engineering parameters."""

from app.editor.schemas import Create

PRESETS = [
    {
        "id": "electrolyser",
        "version": 1,
        "component": "Link",
        "ports": {"bus0": "electricity", "bus1": "hydrogen"},
        "parameters": ["p_nom", "efficiency"],
        "assumptions": "Capacity is electricity input. User supplies conversion efficiency; bus roles are explicit, not inferred from names.",
    },
    {
        "id": "heat_pump",
        "version": 1,
        "component": "Link",
        "ports": {"bus0": "electricity", "bus1": "heat"},
        "parameters": ["p_nom", "efficiency"],
        "assumptions": "User supplies COP, which may exceed one. Ambient heat is implicit.",
    },
    {
        "id": "chp",
        "version": 1,
        "component": "Process",
        "ports": {"bus0": "fuel", "bus1": "electricity", "bus2": "heat"},
        "parameters": ["p_nom", "rate1", "rate2"],
        "assumptions": "Fixed output ratios relative to fuel input, rate0=-1.",
    },
    {
        "id": "transport",
        "version": 1,
        "component": "Link",
        "ports": {"bus0": "origin", "bus1": "destination"},
        "parameters": ["p_nom", "efficiency"],
        "assumptions": "Unidirectional energy transport, not a hydraulic pipe model.",
    },
    {
        "id": "store_with_links",
        "version": 1,
        "component": "composition",
        "ports": {"external": "external bus"},
        "parameters": [
            "e_nom",
            "charge_p_nom",
            "discharge_p_nom",
            "efficiency_store",
            "efficiency_dispatch",
        ],
        "assumptions": "Creates an internal bus, Store, and two independently rated Links. No exclusivity constraint on simultaneous charging/discharging.",
    },
]


def expand(request, n):
    recipe = next((p for p in PRESETS if p["id"] == request.preset), None)
    if recipe is None:
        raise ValueError("Unknown preset.")
    if set(request.buses) != set(recipe["ports"]):
        raise ValueError(f"Preset requires bus roles: {list(recipe['ports'])}")
    if set(request.parameters) != set(recipe["parameters"]):
        raise ValueError(f"Preset requires parameters: {recipe['parameters']}")
    if any(bus not in n.buses.index for bus in request.buses.values()):
        raise ValueError("Preset references an unknown bus.")
    attrs = dict(request.parameters)
    if recipe["component"] != "composition":
        attrs.update(request.buses)
        if request.preset == "chp":
            attrs["rate0"] = -1.0
        return [
            Create(
                op="create",
                component=recipe["component"],
                name=request.name,
                attributes=attrs,
            )
        ]
    base = request.name
    external = request.buses["external"]
    bus = f"{base}:bus"
    return [
        Create(
            op="create",
            component="Bus",
            name=bus,
            attributes={
                "carrier": str(n.buses.at[external, "carrier"]),
                "unit": str(n.buses.at[external, "unit"]),
            },
        ),
        Create(
            op="create",
            component="Store",
            name=base,
            attributes={"bus": bus, "e_nom": attrs["e_nom"]},
        ),
        Create(
            op="create",
            component="Link",
            name=f"{base}:charge",
            attributes={
                "bus0": external,
                "bus1": bus,
                "p_nom": attrs["charge_p_nom"],
                "efficiency": attrs["efficiency_store"],
            },
        ),
        Create(
            op="create",
            component="Link",
            name=f"{base}:discharge",
            attributes={
                "bus0": bus,
                "bus1": external,
                "p_nom": attrs["discharge_p_nom"],
                "efficiency": attrs["efficiency_dispatch"],
            },
        ),
    ]
