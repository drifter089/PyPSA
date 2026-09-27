# SPDX-FileCopyrightText: PyPSA Contributors
# SPDX-License-Identifier: MIT
"""Native behavior, transaction preservation, and real HTTP subprocess contracts."""

import json
import sys
from dataclasses import asdict

import pandas as pd
import pypsa
import pytest
from fastapi.testclient import TestClient

from app import network
from app.config import Settings
from app.editor import metadata
from app.editor.schemas import CatalogRequest, EditRequest, SpecialValue
from app.main import create_app
from app.operations import ExecutionContext
from app.operations.editor import describe_editor, edit_network, evaluate_editor
from app.schemas import NetCDFSource, NetworkModel


@pytest.fixture
def context(tmp_path):
    return ExecutionContext(tmp_path, asdict(Settings()))


@pytest.fixture
def native():
    n = pypsa.Network()
    n.set_snapshots(["first", "second"])
    n.add("Carrier", ["AC", "hydrogen", "heat", "wind"])
    n.add("Bus", "electricity", carrier="AC", v_nom=220)
    n.add("Bus", "hydrogen", carrier="hydrogen")
    n.add("Bus", "heat", carrier="heat")
    n.add(
        "Generator",
        "wind",
        bus="electricity",
        carrier="wind",
        p_nom=100,
        marginal_cost=10,
        p_max_pu=[0.5, 0.8],
        ramp_limit_up=0.3,
    )
    n.add("Load", "demand", bus="electricity", p_set=[10, 20])
    return n


def source(n, context):
    return network.artifact(n, context.root, context.limits)


def run(n, context, operations, commit=True, **kwargs):
    body = EditRequest(source=source(n, context), operations=operations, **kwargs)
    result = (edit_network if commit else evaluate_editor)(body, context)
    json.dumps(result, allow_nan=False)
    return result


def reload(result, context):
    assert result["valid"], result["diagnostics"]
    return network.load(
        NetCDFSource.model_validate(result["artifact"]), context.root, context.limits
    )


def test_inventory_matches_native_metadata(context):
    result = describe_editor(CatalogRequest(limit=0), context)
    json.dumps(result, allow_nan=False)
    n = pypsa.Network()
    for kind, definition in result["component_types"].items():
        assert set(definition["fields"]) == set(n.components[kind].defaults.index)
        for key, field in definition["fields"].items():
            row = n.components[kind].defaults.loc[key]
            assert field["default"] == metadata.encode(row.default)
            assert field["varying"] == bool(row.varying)
            if field["role"] == "output":
                assert field["editor_value_modes"] == []
    generator = result["component_types"]["Generator"]["fields"]
    assert generator["p_nom"]["native_value_modes"] == ["scalar"]
    assert generator["p_max_pu"]["editor_value_modes"] == ["scalar", "profile"]
    assert generator["p_nom_max"]["default"] == {"kind": "positiveInfinity"}
    assert generator["p_set"]["default"] == {"kind": "unset"}
    assert result["choice_sets"]["carriers"]["options"] == []
    assert result["component_types"]["GlobalConstraint"]["ports"] == []


def test_catalog_pagination_and_profile_binding(native, context):
    body = CatalogRequest(
        source=source(native, context), components=["Generator"], include_profiles=True
    )
    result = describe_editor(body, context)
    state = result["instances"][0]
    assert state["fields"]["p_nom"]["value_mode"] == "scalar"
    assert state["fields"]["p_max_pu"]["value_mode"] == "profile"
    assert state["profiles"]["p_max_pu"]["values"] == [0.5, 0.8]
    assert state["fields"]["p_nom"]["provenance"] == "unknown"
    assert (
        describe_editor(body.model_copy(update={"limit": 0}), context)["instances"]
        == []
    )


def test_expansion_toggle_retains_other_inputs(native, context):
    result = run(
        native,
        context,
        [
            {
                "op": "set",
                "component": "Generator",
                "name": "wind",
                "field": "p_nom_extendable",
                "value": True,
            },
            {
                "op": "set",
                "component": "Generator",
                "name": "wind",
                "field": "p_nom_max",
                "value": 200,
            },
        ],
        components=["Generator"],
    )
    n = reload(result, context)
    assert n.generators.at["wind", "p_nom"] == 100
    assert n.generators.at["wind", "ramp_limit_up"] == 0.3
    assert list(n.generators_t.p_max_pu.wind) == [0.5, 0.8]
    fields = result["catalog"]["instances"][0]["fields"]
    assert fields["p_nom"]["overridden"] and not fields["p_nom"]["editable"]
    assert fields["p_nom_max"]["editable"]
    assert fields["p_max_pu"]["editable"]
    assert fields["marginal_cost"]["editable"]
    back = run(
        n,
        context,
        [
            {
                "op": "set",
                "component": "Generator",
                "name": "wind",
                "field": "p_nom_extendable",
                "value": False,
            }
        ],
    )
    assert reload(back, context).generators.at["wind", "p_nom"] == 100


def test_scalar_profile_transitions_and_solve(native, context):
    result = run(
        native,
        context,
        [
            {
                "op": "set",
                "component": "Generator",
                "name": "wind",
                "field": "p_max_pu",
                "value": 0.9,
            }
        ],
    )
    n = reload(result, context)
    assert "wind" not in n.generators_t.p_max_pu.columns
    assert (n.get_switchable_as_dense("Generator", "p_max_pu").wind == 0.9).all()
    result = run(
        n,
        context,
        [
            {
                "op": "set_profile",
                "component": "Generator",
                "name": "wind",
                "field": "p_max_pu",
                "snapshot_id": metadata.snapshot_id(n),
                "values": [0.4, 0.6],
            }
        ],
    )
    n = reload(result, context)
    assert list(n.get_switchable_as_dense("Generator", "p_max_pu").wind) == [0.4, 0.6]
    status, _ = n.optimize(solver_name="highs", log_to_console=False)
    assert status == "ok"
    assert n.objective == pytest.approx(300)


@pytest.mark.parametrize(
    "kind,coefficient", [("Link", "efficiency10"), ("Process", "rate10")]
)
def test_multiport_roundtrip_and_removal(native, context, kind, coefficient):
    native.add(kind, "converter", bus0="electricity", bus1="hydrogen", p_nom=10)
    native.add(kind, "simple", bus0="electricity", bus1="hydrogen", p_nom=10)
    result = run(
        native,
        context,
        [
            {
                "op": "add_port",
                "component": kind,
                "name": "converter",
                "index": 10,
                "bus": "heat",
                "coefficient": 0.3,
                "delay": 1,
                "cyclic_delay": False,
            }
        ],
        components=[kind],
    )
    n = reload(result, context)
    c = n.components[kind]
    assert c.static.at["converter", coefficient] == 0.3
    assert c.static.at["converter", "delay10"] == 1
    assert c.static.at["converter", "bus10"] == "heat"
    simple = next(s for s in result["catalog"]["instances"] if s["name"] == "simple")
    assert not next(p for p in simple["ports"] if p["attribute"] == "bus10")["enabled"]
    result = run(
        n,
        context,
        [{"op": "remove_port", "component": kind, "name": "converter", "index": 10}],
    )
    n = reload(result, context)
    assert n.components[kind].static.loc["converter"].get("bus10", "") == ""
    assert n.components[kind].static.at["converter", "bus1"] == "hydrogen"


@pytest.mark.parametrize(
    "kind,coefficient", [("Link", "efficiency10"), ("Process", "rate10")]
)
def test_existing_json_builder_additional_families(context, kind, coefficient):
    model = NetworkModel.model_validate(
        {
            "snapshots": ["a", "b"],
            "components": [
                {"type": "Bus", "name": "b"},
                {
                    "type": kind,
                    "name": "c",
                    "attributes": {
                        "bus0": "b",
                        "bus1": "b",
                        "bus10": "b",
                        coefficient: [0.2, 0.3],
                        "delay10": 1,
                    },
                },
            ],
        }
    )
    n = network.from_model(model, context.limits)
    assert n.components[kind].dynamic[coefficient]["c"].tolist() == [0.2, 0.3]
    assert n.components[kind].static.at["c", "delay10"] == 1
    model.components[-1].attributes["delay10"] = [1, 2]
    with pytest.raises(ValueError, match="does not accept time series"):
        network.from_model(model, context.limits)


def test_failed_batch_is_atomic(native, context):
    result = run(
        native,
        context,
        [
            {
                "op": "set",
                "component": "Generator",
                "name": "wind",
                "field": "p_nom",
                "value": 500,
            },
            {
                "op": "set",
                "component": "Generator",
                "name": "wind",
                "field": "p_nom_opt",
                "value": 600,
            },
        ],
        components=["Generator"],
    )
    assert not result["valid"] and not result["applied"] and result["artifact"] is None
    assert result["catalog"]["instances"][0]["static"]["p_nom"] == 100
    assert result["diagnostics"][0]["operation"] == 1
    assert native.generators.at["wind", "p_nom"] == 100


@pytest.mark.parametrize(
    "operation",
    [
        {"op": "set", "field": "p_nom", "value": True},
        {"op": "set", "field": "p_nom", "value": "12"},
        {"op": "set", "field": "p_nom", "value": {"kind": "positiveInfinity"}},
        {"op": "set", "field": "p_nom_extendable", "value": "true"},
        {"op": "set", "field": "p_nom", "value": -1},
        {"op": "connect", "port": "carrier", "bus": "electricity"},
        {"op": "connect", "port": "bus", "bus": "missing"},
        {
            "op": "set_profile",
            "field": "p_nom",
            "snapshot_id": "stale",
            "values": [1, 2],
        },
    ],
)
def test_invalid_edits(native, context, operation):
    result = run(
        native, context, [{"component": "Generator", "name": "wind", **operation}]
    )
    assert not result["valid"] and result["artifact"] is None


def test_incomplete_draft_can_be_evaluated(context):
    result = evaluate_editor(
        EditRequest(
            operations=[
                {"op": "create", "component": "Generator", "name": "incomplete"}
            ],
            components=["Generator"],
        ),
        context,
    )
    assert not result["valid"] and not result["applied"]
    assert result["catalog"]["instances"][0]["name"] == "incomplete"
    assert any(d["field"] == "bus" for d in result["diagnostics"])


def test_reset_and_unset(native, context):
    result = run(
        native,
        context,
        [
            {
                "op": "reset_to_default",
                "component": "Generator",
                "name": "wind",
                "field": "p_max_pu",
            },
            {
                "op": "set",
                "component": "Generator",
                "name": "wind",
                "field": "p_set",
                "value": {"kind": "unset"},
            },
        ],
    )
    n = reload(result, context)
    assert "wind" not in n.generators_t.p_max_pu
    assert n.generators.at["wind", "p_max_pu"] == 1
    assert pd.isna(n.generators.at["wind", "p_set"])


def test_solved_outputs_are_invalidated(native, context):
    native.optimize(solver_name="highs", log_to_console=False)
    result = run(
        native,
        context,
        [
            {
                "op": "set",
                "component": "Generator",
                "name": "wind",
                "field": "marginal_cost",
                "value": 11,
            }
        ],
    )
    n = reload(result, context)
    assert n.generators_t.p.empty
    assert n.generators.at["wind", "p_nom_opt"] == 0
    assert n._objective is None
    assert list(n.generators_t.p_max_pu.wind) == [0.5, 0.8]
    assert n.meta["editor_solution_state"] == "invalidated"


def test_storage_override_and_standard_type(native, context):
    native.add("Store", "tank", bus="hydrogen", e_nom=30, e_initial=10)
    result = run(
        native,
        context,
        [
            {
                "op": "set",
                "component": "Store",
                "name": "tank",
                "field": "e_cyclic",
                "value": True,
            }
        ],
        components=["Store"],
    )
    assert reload(result, context).stores.at["tank", "e_initial"] == 10
    assert result["catalog"]["instances"][0]["fields"]["e_initial"]["overridden"]
    native.add("Bus", "other", v_nom=220)
    standard = str(native.line_types.index[0])
    result = run(
        native,
        context,
        [
            {
                "op": "create",
                "component": "Line",
                "name": "line",
                "attributes": {
                    "bus0": "electricity",
                    "bus1": "other",
                    "type": standard,
                    "length": 10,
                    "s_nom": 100,
                },
            }
        ],
        components=["Line"],
    )
    reload(result, context)
    assert result["catalog"]["instances"][0]["fields"]["x"]["overridden"]


def test_network_axis_and_profiles(native, context):
    result = run(native, context, [{"op": "set_snapshots", "snapshots": ["new"]}])
    assert not result["valid"]
    result = run(
        native,
        context,
        [
            {
                "op": "set_snapshots",
                "snapshots": ["first", "second"],
                "weightings": {"objective": [2, 3]},
            }
        ],
    )
    assert list(reload(result, context).snapshot_weightings.objective) == [2, 3]


@pytest.mark.parametrize(
    "preset,parameters,buses",
    [
        (
            "electrolyser",
            {"p_nom": 10, "efficiency": 0.7},
            {"bus0": "electricity", "bus1": "hydrogen"},
        ),
        (
            "heat_pump",
            {"p_nom": 10, "efficiency": 3},
            {"bus0": "electricity", "bus1": "heat"},
        ),
        (
            "chp",
            {"p_nom": 10, "rate1": 0.3, "rate2": 0.5},
            {"bus0": "hydrogen", "bus1": "electricity", "bus2": "heat"},
        ),
        (
            "transport",
            {"p_nom": 10, "efficiency": 0.95},
            {"bus0": "electricity", "bus1": "hydrogen"},
        ),
        (
            "store_with_links",
            {
                "e_nom": 100,
                "charge_p_nom": 20,
                "discharge_p_nom": 10,
                "efficiency_store": 0.9,
                "efficiency_dispatch": 0.8,
            },
            {"external": "electricity"},
        ),
    ],
)
def test_native_presets(native, context, preset, parameters, buses):
    result = run(
        native,
        context,
        [
            {
                "op": "apply_preset",
                "preset": preset,
                "name": "device",
                "parameters": parameters,
                "buses": buses,
            }
        ],
    )
    n = reload(result, context)
    assert result["extensions"][0]["preset"] == preset
    if preset == "store_with_links":
        assert n.stores.at["device", "e_nom"] == 100
        assert n.links.at["device:charge", "p_nom"] == 20
        assert n.links.at["device:discharge", "p_nom"] == 10


def test_source_identity_is_enforced(native, context):
    with pytest.raises(ValueError, match="identity mismatch"):
        run(native, context, [], expected_source_id="stale")


def test_http_editor_contracts_and_auth():
    with TestClient(create_app(Settings(api_key="editor-test"))) as client:
        paths = ["/v1/editor/catalog", "/v1/editor/evaluate", "/v1/networks/edit"]
        for path in paths:
            assert client.post(path, json={}).status_code == 401
        headers = {"Authorization": "Bearer editor-test"}
        response = client.post(
            paths[0], json={"components": ["Generator"], "limit": 0}, headers=headers
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert "p_nom" in data["component_types"]["Generator"]["fields"]
        response = client.post(
            paths[1],
            json={
                "operations": [{"op": "create", "component": "Generator", "name": "g"}]
            },
            headers=headers,
        )
        assert response.status_code == 200, response.text
        assert not response.json()["valid"]
        response = client.post(
            paths[2],
            json={
                "operations": [
                    {"op": "create", "component": "Carrier", "name": "AC"},
                    {"op": "create", "component": "Bus", "name": "b"},
                ]
            },
            headers=headers,
        )
        assert response.status_code == 200, response.text
        assert response.json()["applied"]
        schema = client.get("/openapi.json").json()
        for path in paths:
            assert (
                "$ref"
                in schema["paths"][path]["post"]["responses"]["200"]["content"][
                    "application/json"
                ]["schema"]
            )
        assert (
            client.post(
                paths[2], json={"operations": [{"op": "arbitrary"}]}, headers=headers
            ).status_code
            == 422
        )


def test_rule_schemas_are_safe_and_special_values_strict():
    from app.editor.schemas import Condition

    with pytest.raises(ValueError):
        Condition(op="not", args=[])
    with pytest.raises(ValueError):
        SpecialValue(kind="nan")
    assert "app.editor.schemas" in sys.modules


def test_power_flow_forms_follow_native_usage(native, context):
    native.add("Bus", "lower", carrier="AC", v_nom=20)
    result = run(
        native,
        context,
        [
            {
                "op": "create",
                "component": "Transformer",
                "name": "t",
                "attributes": {
                    "bus0": "electricity",
                    "bus1": "lower",
                    "s_nom": 40,
                    "x": 0.1,
                    "r": 0.01,
                },
            }
        ],
        analysis="pf",
        components=["Generator", "Transformer"],
    )
    reload(result, context)
    generator = next(
        s for s in result["catalog"]["instances"] if s["component"] == "Generator"
    )
    assert generator["fields"]["control"]["editable"]
    assert generator["fields"]["q_set"]["editable"]
    assert not generator["fields"]["efficiency"]["applicable"]
    assert not generator["fields"]["name"]["editable"]
    linear = describe_editor(
        CatalogRequest(
            source=source(native, context), analysis="lpf", components=["Generator"]
        ),
        context,
    )
    assert not linear["instances"][0]["fields"]["q_set"]["applicable"]


def test_cost_configuration_and_constraint_interpretation(native, context):
    operations = [
        {
            "op": "set",
            "component": "Generator",
            "name": "wind",
            "field": "overnight_cost",
            "value": 1000,
        },
        {
            "op": "set",
            "component": "Generator",
            "name": "wind",
            "field": "discount_rate",
            "value": 0.05,
        },
        {
            "op": "set",
            "component": "Generator",
            "name": "wind",
            "field": "lifetime",
            "value": 20,
        },
        {
            "op": "create",
            "component": "GlobalConstraint",
            "name": "wind-limit",
            "attributes": {
                "type": "operational_limit",
                "carrier_attribute": "wind",
                "sense": "<=",
                "constant": 1000,
            },
        },
    ]
    result = run(
        native, context, operations, components=["Generator", "GlobalConstraint"]
    )
    n = reload(result, context)
    state = next(
        s for s in result["catalog"]["instances"] if s["component"] == "Generator"
    )
    assert state["fields"]["capital_cost"]["overridden"]
    assert n.global_constraints.at["wind-limit", "carrier_attribute"] == "wind"
    assert n.components.generators.capital_cost.at["wind"] > 0


def test_preserves_piecewise_and_requires_explicit_reset(native, context):
    native.add(
        "Generator",
        "curve",
        bus="electricity",
        p_nom=10,
        marginal_cost={0: 1, 0.5: 2, 1: 4},
    )
    result = run(
        native,
        context,
        [
            {
                "op": "set",
                "component": "Generator",
                "name": "curve",
                "field": "p_nom",
                "value": 20,
            }
        ],
    )
    n = reload(result, context)
    assert n.components.generators.has_piecewise("marginal_cost")
    rejected = run(
        n,
        context,
        [
            {
                "op": "set",
                "component": "Generator",
                "name": "curve",
                "field": "marginal_cost",
                "value": 4,
            }
        ],
    )
    assert not rejected["valid"]
    result = run(
        n,
        context,
        [
            {
                "op": "reset_to_default",
                "component": "Generator",
                "name": "curve",
                "field": "marginal_cost",
            }
        ],
    )
    assert not reload(result, context).components.generators.has_piecewise(
        "marginal_cost"
    )


def test_deleting_bus_requires_explicit_reference_handling(native, context):
    invalid = run(
        native, context, [{"op": "delete", "component": "Bus", "name": "electricity"}]
    )
    assert not invalid["valid"] and invalid["artifact"] is None
    result = run(
        native,
        context,
        [
            {
                "op": "connect",
                "component": "Generator",
                "name": "wind",
                "port": "bus",
                "bus": "heat",
            },
            {
                "op": "connect",
                "component": "Load",
                "name": "demand",
                "port": "bus",
                "bus": "heat",
            },
            {"op": "delete", "component": "Bus", "name": "electricity"},
        ],
    )
    n = reload(result, context)
    assert (
        n.generators.at["wind", "bus"] == "heat"
    )  # carrier labels do not impose technology rules


def test_parallel_branches_and_cycles(native, context):
    for name in ["b", "c"]:
        native.add("Bus", name, carrier="AC", v_nom=220)
    operations = [
        {
            "op": "create",
            "component": "Line",
            "name": str(i),
            "attributes": {"bus0": a, "bus1": b, "x": 0.1, "s_nom": 100},
        }
        for i, (a, b) in enumerate(
            [
                ("electricity", "b"),
                ("electricity", "b"),
                ("b", "c"),
                ("c", "electricity"),
            ]
        )
    ]
    assert len(reload(run(native, context, operations), context).lines) == 4


def test_native_default_equality_is_not_claimed_provenance(native, context):
    result = run(
        native,
        context,
        [
            {
                "op": "set",
                "component": "Generator",
                "name": "wind",
                "field": "p_nom_extendable",
                "value": True,
            },
            {
                "op": "set",
                "component": "Generator",
                "name": "wind",
                "field": "p_nom_max",
                "value": {"kind": "positiveInfinity"},
            },
        ],
        components=["Generator"],
    )
    state = result["catalog"]["instances"][0]["fields"]["p_nom_max"]
    assert state["matches_default"] and state["provenance"] == "unknown"
    assert reload(result, context).generators.at["wind", "p_nom_max"] == float("inf")


def test_evaluation_invalidates_stale_results_without_export(native, context):
    native.optimize(
        solver_name="highs", log_to_console=False, include_objective_constant=False
    )
    result = run(
        native,
        context,
        [
            {
                "op": "set",
                "component": "Generator",
                "name": "wind",
                "field": "p_nom",
                "value": 110,
            }
        ],
        commit=False,
        components=["Generator"],
    )
    assert result["valid"] and not result["applied"] and result["artifact"] is None
    assert result["catalog"]["context"]["solution_state"] == "invalidated"
    assert "p" not in result["catalog"]["instances"][0]["profiles"]


def test_power_flow_port_creation_does_not_require_optimization_fields(native, context):
    native.add("Link", "converter", bus0="electricity", bus1="hydrogen", p_set=0)
    result = run(
        native,
        context,
        [
            {
                "op": "add_port",
                "component": "Link",
                "name": "converter",
                "index": 2,
                "bus": "heat",
                "coefficient": 0.2,
            }
        ],
        analysis="pf",
    )
    assert reload(result, context).links.at["converter", "bus2"] == "heat"
