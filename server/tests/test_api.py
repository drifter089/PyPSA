# SPDX-FileCopyrightText: PyPSA Contributors
# SPDX-License-Identifier: MIT
"""Integration tests use the real local PyPSA source and HiGHS solver."""

import json
import os
from pathlib import Path

import pytest
from app import execution
from app.config import Settings
from app.main import create_app
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    with TestClient(create_app(Settings())) as instance:
        yield instance


def post(client, endpoint, body):
    response = client.post(f"/v1/networks/{endpoint}", json=body)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture(scope="module")
def source(client):
    response = client.get("/v1/examples/two-bus")
    assert response.status_code == 200, response.text
    return response.json()["artifact"]


@pytest.fixture(scope="module")
def solved(client, source):
    return post(
        client, "optimize", {"source": source, "statistics": [{"metric": "supply"}]}
    )


@pytest.fixture
def child_processes(monkeypatch):
    """Observe real child lifecycles rather than replace the execution boundary."""
    children = []
    spawn = execution.asyncio.create_subprocess_exec

    async def record(*args, **kwargs):
        process = await spawn(*args, **kwargs)
        children.append((process, Path(kwargs["cwd"])))
        return process

    monkeypatch.setattr(execution.asyncio, "create_subprocess_exec", record)
    return children


def model_source(demand=10, capacity=100):
    return {
        "kind": "model",
        "model": {
            "components": [
                {
                    "type": "Load",
                    "name": "load",
                    "attributes": {"bus": "bus", "p_set": demand},
                },
                {"type": "Carrier", "name": "AC"},
                {"type": "Bus", "name": "bus", "attributes": {"carrier": "AC"}},
                {
                    "type": "Generator",
                    "name": "supply",
                    "attributes": {
                        "bus": "bus",
                        "p_nom": capacity,
                        "marginal_cost": 10,
                    },
                },
            ],
        },
    }


def test_health_auth_and_openapi():
    with TestClient(create_app(Settings(api_key="test-credential"))) as client:
        assert client.get("/healthz").status_code == 200
        for method, path in [
            ("GET", "/v1/capabilities"),
            ("GET", "/v1/components/Generator"),
            ("GET", "/v1/examples"),
            ("GET", "/v1/examples/two-bus"),
            ("POST", "/v1/networks/inspect"),
            ("POST", "/v1/networks/export"),
            ("POST", "/v1/networks/validate"),
            ("POST", "/v1/networks/statistics"),
            ("POST", "/v1/networks/optimize"),
            ("POST", "/v1/networks/power-flow"),
        ]:
            assert client.request(method, path, json={}).status_code == 401, path
        assert (
            client.get(
                "/v1/capabilities", headers={"Authorization": "Bearer test-credential"}
            ).status_code
            == 200
        )
        schema = client.get("/openapi.json").json()
        assert "/v1/networks/optimize" in schema["paths"]
        assert "HTTPBearer" in schema["components"]["securitySchemes"]


def test_component_metadata(client):
    response = client.get("/v1/components/Generator")
    assert response.status_code == 200, response.text
    table = response.json()["attributes"]
    assert "p_nom" in table["index"]
    assert "unit" in table["columns"]


def test_computations_use_separate_children_and_clean_up(client, child_processes):
    assert client.get("/healthz").status_code == 200
    assert child_processes == []
    assert client.get("/v1/components/Generator").status_code == 200
    post(client, "inspect", {"source": model_source()})
    assert len(child_processes) == 2
    assert len({process.pid for process, _ in child_processes} | {os.getpid()}) == 3
    for process, directory in child_processes:
        assert process.returncode == 0
        assert not directory.exists()


def test_native_roundtrip(client, source):
    imported = post(client, "inspect", {"source": source})["network"]
    exported = post(client, "export", {"source": source})["artifact"]
    reimported = post(client, "inspect", {"source": exported})["network"]
    assert imported == reimported
    assert post(client, "validate", {"source": source})["valid"]


def test_optimize_dispatch_and_saved_statistics(client, solved):
    assert solved["solution_available"]
    assert solved["termination_condition"] == "optimal"
    assert solved["objective"] == pytest.approx(1400)
    network = post(
        client, "inspect", {"source": solved["artifact"], "include_time_series": True}
    )["network"]
    dispatch = network["components"]["Generator"]["dynamic"]["p"]
    values = dict(zip(dispatch["columns"], dispatch["data"][0], strict=True))
    assert values == pytest.approx({"cheap": 50, "expensive": 30})
    stats = post(
        client,
        "statistics",
        {"source": solved["artifact"], "statistics": [{"metric": "supply"}]},
    )
    assert stats["statistics"] == solved["statistics"]


def test_weighted_time_series(client):
    source = model_source(demand=[10, 20])
    source["model"].update(
        {
            "snapshots": ["first", "second"],
            "snapshot_weightings": {
                "objective": [2, 3],
                "generators": [2, 3],
                "stores": [2, 3],
            },
        }
    )
    result = post(
        client,
        "optimize",
        {
            "source": source,
            "statistics": [
                {"metric": "supply", "components": ["Generator"], "groupby": "name"}
            ],
        },
    )
    assert result["objective"] == pytest.approx(800)
    assert result["statistics"][0]["table"]["data"][0][0] == pytest.approx(80)


def test_infeasible_is_not_reported_as_success(client):
    result = post(client, "optimize", {"source": model_source(demand=10, capacity=1)})
    assert result["termination_condition"] == "infeasible"
    assert not result["solution_available"]
    assert result["artifact"] is None


@pytest.mark.parametrize("mode", ["linear", "nonlinear"])
def test_power_flow(client, mode):
    result = post(client, "power-flow", {"source": model_source(), "mode": mode})
    assert result["solution_available"]
    assert result["artifact"]["kind"] == "netcdf"
    if mode == "nonlinear":
        assert "converged" in result["convergence"]


@pytest.mark.parametrize(
    "name,buses", [("ac-dc-meshed", 9), ("storage-hvdc", 6), ("model-energy", 2)]
)
def test_bundled_upstream_example(client, name, buses):
    response = client.get(f"/v1/examples/{name}")
    assert response.status_code == 200, response.text
    assert (
        len(response.json()["network"]["components"]["Bus"]["static"]["index"]) == buses
    )


def test_statistics_preserve_filters_grouping_and_time_series(client):
    source = model_source(demand=[10, 20])
    source["model"].update(
        {
            "snapshots": ["2026-01-01T00:00:00", "2026-01-01T01:00:00"],
            "snapshot_kind": "datetime",
            "snapshot_weightings": {
                "objective": [2, 3],
                "generators": [2, 3],
                "stores": [2, 3],
            },
        }
    )
    source["model"]["components"].extend(
        [
            {"type": "Carrier", "name": "heat"},
            {"type": "Bus", "name": "heat", "attributes": {"carrier": "heat"}},
            {
                "type": "Generator",
                "name": "heater",
                "attributes": {"bus": "heat", "p_nom": 100, "marginal_cost": 2},
            },
            {
                "type": "Load",
                "name": "heat-demand",
                "attributes": {"bus": "heat", "p_set": [3, 7]},
            },
        ]
    )
    queries = [
        {
            "metric": "supply",
            "components": ["Generator"],
            "groupby": "name",
            "bus_carrier": "AC",
            "groupby_time": False,
        },
        {
            "metric": "supply",
            "components": ["Generator"],
            "groupby": "bus",
            "bus_carrier": "AC",
            "groupby_time": "mean",
        },
        {
            "metric": "withdrawal",
            "components": ["Load"],
            "groupby": "name",
            "bus_carrier": "heat",
            "groupby_time": False,
        },
    ]
    solved = post(client, "optimize", {"source": source, "statistics": queries})
    assert solved["objective"] == pytest.approx(854)
    stats = solved["statistics"]
    for query, result in zip(queries, stats, strict=True):
        assert result["parameters"] == {
            key: value for key, value in query.items() if key != "metric"
        }
    assert stats[0]["table"]["data"] == [[10.0, 20.0]]
    assert "name" in stats[0]["table"]["index_names"]
    assert stats[1]["table"]["data"] == [[16.0]]
    assert "bus" in stats[1]["table"]["index_names"]
    assert stats[2]["table"]["data"] == [[3.0, 7.0]]
    saved = post(
        client, "statistics", {"source": solved["artifact"], "statistics": queries}
    )
    assert saved["statistics"] == stats


def test_unsupported_statistics_options_are_rejected_not_dropped(client, solved):
    for query in [
        {"metric": "installed_capacity", "groupby_time": False},
        {"metric": "prices", "components": ["Generator"]},
    ]:
        response = client.post(
            "/v1/networks/statistics",
            json={"source": solved["artifact"], "statistics": [query]},
        )
        assert response.status_code == 422, response.text
        assert "unexpected keyword" in response.json()["detail"]


def test_validation_and_bad_input(client):
    source = model_source()
    source["model"]["components"][0]["attributes"]["bus"] = "missing"
    assert not post(client, "validate", {"source": source})["valid"]
    assert (
        client.post("/v1/networks/optimize", json={"source": source}).status_code == 422
    )
    assert (
        client.post(
            "/v1/networks/inspect",
            json={"source": {"kind": "netcdf", "data": "not-base64"}},
        ).status_code
        == 422
    )
    source = model_source()
    source["model"]["components"][-1]["attributes"]["made_up_attribute"] = 10
    assert (
        client.post("/v1/networks/export", json={"source": source}).status_code == 422
    )


def test_body_and_network_limits():
    with TestClient(create_app(Settings(max_body_bytes=10))) as client:
        response = client.post(
            "/v1/networks/inspect", content=json.dumps({"source": model_source()})
        )
        assert response.status_code == 413
    with TestClient(create_app(Settings(max_components=1))) as client:
        assert (
            client.post(
                "/v1/networks/inspect", json={"source": model_source()}
            ).status_code
            == 422
        )


def test_execution_timeout_and_slot_released(child_processes):
    with TestClient(create_app(Settings(request_timeout=0.01))) as client:
        assert client.get("/v1/examples/two-bus").status_code == 504
        assert not client.app.state.slot.locked()
        assert client.get("/healthz").status_code == 200
    assert len(child_processes) == 1
    process, directory = child_processes[0]
    assert process.returncode is not None
    assert not directory.exists()


def test_busy_does_not_enqueue(client):
    async def reserve():
        await client.app.state.slot.acquire()

    client.portal.call(reserve)
    try:
        assert client.get("/v1/examples/two-bus").status_code == 429
        assert client.get("/healthz").status_code == 200
    finally:
        client.portal.call(client.app.state.slot.release)
