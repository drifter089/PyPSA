# SPDX-FileCopyrightText: PyPSA Contributors
# SPDX-License-Identifier: MIT
"""Reviewed native form policy; no rules inferred from prose at request time."""

import math
import re

from app.editor.schemas import Condition, FieldRule

VERSION = "1"
READ_ONLY_TYPES = {"Shape", "SubNetwork", "LineType", "TransformerType"}
GLOBAL_TYPES = [
    "primary_energy",
    "operational_limit",
    "tech_capacity_expansion_limit",
    "transmission_volume_expansion_limit",
    "transmission_expansion_cost_limit",
]
ENUMS = {
    "control": ["PQ", "PV", "Slack"],
    "GlobalConstraint.type": GLOBAL_TYPES,
    "GlobalConstraint.sense": ["<=", "==", ">="],
    "Transformer.model": ["t", "pi"],
    "Transformer.tap_side": [0, 1],
}
PLACEHOLDERS = {
    (kind, "type")
    for kind in ["Bus", "Generator", "Load", "Store", "StorageUnit", "Link", "Process"]
} | {("Bus", "v_mag_pu_min"), ("Bus", "v_mag_pu_max")}
DEPRECATED = {("Line", "v_ang_min"), ("Transformer", "v_ang_min")}
# Complex phase optimization is inspectable until its form policy is implemented.
UNREVIEWED = {("Transformer", "phase_shift_min"), ("Transformer", "phase_shift_max")}
COMMITMENT = {
    "start_up_cost",
    "shut_down_cost",
    "stand_by_cost",
    "min_up_time",
    "min_down_time",
    "up_time_before",
    "down_time_before",
    "ramp_limit_start_up",
    "ramp_limit_shut_down",
}
PF_ONLY = {"q_set", "control", "v_mag_pu_set"}
COMMON = {
    "name",
    "bus",
    "bus0",
    "bus1",
    "carrier",
    "type",
    "sign",
    "p_set",
    "v_nom",
    "unit",
    "x",
    "y",
    "location",
    "r",
    "g",
    "b",
    "model",
    "length",
    "num_parallel",
    "tap_ratio",
    "tap_side",
    "tap_position",
    "phase_shift",
    "active",
}


def comparison(ref, value, op="eq"):
    return Condition(op=op, ref=ref, value=value)


def both(*conditions):
    return Condition(op="all", args=list(conditions))


def rules_for(kind, field, available):
    rules = []
    optimize = comparison("context.analysis", "optimize")

    def add(effect, when, reason, suffix):
        rules.append(
            FieldRule(
                id=f"{kind}.{field}.{suffix}", effect=effect, when=when, reason=reason
            )
        )

    if field in PF_ONLY:
        add(
            "applicable",
            comparison(
                "context.analysis",
                ["pf", "lpf"] if field == "control" else ["pf"],
                "in",
            ),
            "Relevant to the selected power-flow analysis.",
            "power-flow",
        )
    elif kind == "GlobalConstraint" or (
        kind
        not in {
            "Bus",
            "Carrier",
            "LineType",
            "TransformerType",
            "Shape",
            "SubNetwork",
            "ShuntImpedance",
        }
        and field not in COMMON
        and not re.fullmatch(r"bus\d+", field)
        and not (
            kind in {"Link", "Process"} and field.startswith(("efficiency", "rate"))
        )
        and not (kind == "Transformer" and field == "s_nom")
    ):
        add("applicable", optimize, "Used by optimization, not power flow.", "optimize")

    nominal = next((a for a in ("p_nom", "e_nom", "s_nom") if a in available), None)
    if nominal and f"{nominal}_extendable" in available:
        extend = comparison(f"asset.{nominal}_extendable", True)
        if field == nominal:
            add(
                "overridden",
                both(optimize, extend),
                f"{nominal}_extendable selects optimized capacity; this fixed value is retained.",
                "extendable",
            )
        if field in {f"{nominal}_{s}" for s in ("min", "max", "set")}:
            add(
                "applicable",
                both(optimize, extend),
                "Applies to extendable capacity.",
                "extendable",
            )
    if field in COMMITMENT:
        add(
            "applicable",
            comparison("asset.committable", True),
            "Requires committable=True.",
            "commitment",
        )
    if field.startswith("maintenance_"):
        add(
            "applicable",
            comparison("asset.maintainable", True),
            "Requires maintainable=True.",
            "maintenance",
        )
    if field in {"e_initial", "state_of_charge_initial"}:
        cyclic = "e_cyclic" if kind == "Store" else "cyclic_state_of_charge"
        add(
            "overridden",
            both(optimize, comparison(f"asset.{cyclic}", True)),
            "Cyclic storage determines initial state from final state.",
            "cyclic",
        )
    if field == "capital_cost" and "overnight_cost" in available:
        add(
            "overridden",
            Condition(op="isSet", ref="asset.overnight_cost"),
            "overnight_cost takes precedence over capital_cost.",
            "overnight",
        )
    if field == "discount_rate":
        add(
            "applicable",
            Condition(op="isSet", ref="asset.overnight_cost"),
            "Discount rate is used with overnight_cost.",
            "overnight",
        )
        add(
            "required",
            Condition(op="isSet", ref="asset.overnight_cost"),
            "Specify a discount rate for overnight investment costs.",
            "required-rate",
        )
    if kind in {"Line", "Transformer"}:
        standard = comparison("asset.type", "", "ne")
        overrides = (
            {"r", "x", "b"}
            if kind == "Line"
            else {"r", "x", "g", "b", "s_nom", "tap_ratio", "tap_side", "phase_shift"}
        )
        if field in overrides:
            add(
                "overridden",
                standard,
                "Derived from selected standard type.",
                "standard-type",
            )
        if field in {"num_parallel", "tap_position"}:
            add(
                "applicable",
                standard,
                "Requires a selected standard type.",
                "standard-type",
            )
    if kind == "GlobalConstraint":
        if field == "bus":
            add(
                "applicable",
                comparison("asset.type", "tech_capacity_expansion_limit"),
                "Bus filter applies to technology capacity expansion limits.",
                "constraint-type",
            )
        if field == "investment_period":
            add(
                "applicable",
                comparison("context.multi_investment", True),
                "Investment-period axes are not supported by this API.",
                "periods",
            )
    return rules


def evaluate(condition, asset, context):
    if condition.op in {"all", "any", "not"}:
        values = [evaluate(arg, asset, context) for arg in condition.args]
        return (
            all(values)
            if condition.op == "all"
            else any(values)
            if condition.op == "any"
            else not values[0]
        )
    scope, key = condition.ref.split(".", 1)
    if scope not in {"asset", "context"}:
        raise ValueError(f"Invalid rule scope: {scope}")
    value = (asset if scope == "asset" else context).get(key)
    if condition.op == "isSet":
        return (
            value is not None
            and value != ""
            and not (isinstance(value, float) and math.isnan(value))
        )
    if condition.op == "in":
        return value in condition.value
    return (
        value == condition.value if condition.op == "eq" else value != condition.value
    )


def validate_rules(fields):
    """References point only at native values, never at other evaluated states."""

    def check(condition):
        if condition.ref:
            scope, key = condition.ref.split(".", 1)
            if scope == "asset" and key not in fields:
                raise ValueError(f"Unknown policy reference: {condition.ref}")
            if scope == "context" and key not in {"analysis", "multi_investment"}:
                raise ValueError(f"Unknown context reference: {condition.ref}")
            if scope not in {"asset", "context"}:
                raise ValueError("Rules may not reference evaluated states.")
        for arg in condition.args:
            check(arg)

    for field in fields.values():
        for rule in field.rules:
            check(rule.when)


def group_for(field):
    if field.startswith("bus") or field in {"name", "carrier", "type"}:
        return "identity_and_connections"
    if "nom" in field or field == "max_hours":
        return "capacity"
    if "cost" in field or field == "discount_rate":
        return "costs"
    if field.startswith(("efficiency", "rate", "delay", "cyclic_delay")):
        return "conversion_and_transport"
    if (
        field in COMMITMENT
        or field in {"committable", "maintainable"}
        or field.startswith("maintenance_")
    ):
        return "commitment"
    if field.startswith(("e_", "state_", "cyclic_")) or field in {
        "standing_loss",
        "inflow",
    }:
        return "storage"
    return "operation"


def bounds_for(kind, field):
    if field in {
        "standing_loss",
        "efficiency_store",
        "efficiency_dispatch",
    } and kind in {"Store", "StorageUnit"}:
        return 0.0, 1.0
    if (
        re.fullmatch(r"(?:p|e|s)_nom(?:_(?:min|max|mod|set))?", field)
        or field == "max_hours"
        or re.fullmatch(r"delay\d*", field)
    ):
        return 0.0, None
    return None, None
