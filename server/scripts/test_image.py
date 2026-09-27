# SPDX-FileCopyrightText: PyPSA Contributors
# SPDX-License-Identifier: MIT
"""Black-box tests of an existing Docker image. No rebuilds or source mounts.

Run from the repository root:
    uv run --project server python server/scripts/test_image.py pypsa-api:local
"""

import base64
import copy
import hashlib
import json
import math
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import httpx

IMAGE = "pypsa-api:local"
KEY = "temporary-image-test-credential"
REPORT = Path(tempfile.gettempdir()) / "pypsa-image-test-results.json"
RESULTS = []
CONTAINERS = []


def docker(*args):
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=90
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip())
    return result.stdout.strip()


@contextmanager
def instance(**environment):
    args = [
        "run",
        "-d",
        "--rm",
        "--init",
        "--cpus",
        "2",
        "--memory",
        "4g",
        "--label",
        "pypsa.image-test=true",
        "-p",
        "127.0.0.1::8080",
    ]
    env = {"PORT": "8080", "SERVER_API_KEY": KEY, **environment}
    for key, value in env.items():
        args.extend(["-e", f"{key}={value}"])
    cid = docker(*args, IMAGE)
    CONTAINERS.append(cid)
    port = json.loads(docker("inspect", cid))[0]["NetworkSettings"]["Ports"][
        "8080/tcp"
    ][0]["HostPort"]
    try:
        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}",
            timeout=300,
            headers={"Authorization": f"Bearer {KEY}"},
        ) as client:
            for _ in range(80):
                try:
                    if client.get("/healthz").status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                time.sleep(0.25)
            else:
                raise AssertionError("Container did not become healthy")
            yield cid, client
    finally:
        output = subprocess.run(["docker", "logs", cid], capture_output=True, text=True)
        REPORT.with_name(f"pypsa-image-{cid[:12]}.log").write_text(
            output.stdout + output.stderr, encoding="utf-8"
        )
        docker("rm", "-f", cid)
        CONTAINERS.remove(cid)


def check(name, function):
    start = time.monotonic()
    try:
        detail = function()
        outcome = {"name": name, "status": "PASS", "detail": detail}
    except Exception as error:
        outcome = {"name": name, "status": "FAIL", "detail": str(error)}
    outcome["seconds"] = round(time.monotonic() - start, 2)
    RESULTS.append(outcome)
    print(f"{outcome['status']:4} {name}: {outcome['detail'] or ''}", flush=True)


def expect(response, status=200):
    assert response.status_code == status, (
        f"HTTP {response.status_code}, expected {status}: {response.text[:1500]}"
    )
    return response.json()


def post(client, operation, source, **options):
    return expect(
        client.post(f"/v1/networks/{operation}", json={"source": source, **options})
    )


def model(demand=10, capacity=100):
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
            ]
        },
    }


def near(value, expected):
    assert math.isclose(value, expected, abs_tol=1e-5), (value, expected)


def inspect_state(cid):
    script = """import json, os
from pathlib import Path
workers = []
for directory in Path('/proc').glob('[0-9]*'):
    try:
        args = (directory / 'cmdline').read_bytes().split(b'\\0')
        if b'app.worker' in args:
            workers.append(int(directory.name))
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        pass
peak = Path('/sys/fs/cgroup/memory.peak')
print(json.dumps({'workers': workers, 'temporary_jobs': [str(p) for p in Path('/tmp').glob('pypsa-request-*')], 'uid': os.getuid(), 'has_git': Path('/src/.git').exists(), 'has_checkout': Path('/src/pypsa').exists(), 'has_registry': Path('/src/server/app/operations/registry.py').exists(), 'peak_memory_bytes': int(peak.read_text()) if peak.exists() else None}))
"""
    return json.loads(docker("exec", cid, "python", "-c", script))


def assert_clean(cid):
    state = inspect_state(cid)
    assert not state["workers"], state
    assert not state["temporary_jobs"], state
    return state


def main_suite():
    with instance() as (cid, client):

        def startup():
            state = assert_clean(cid)
            assert (
                state["uid"] == 10001
                and not state["has_git"]
                and not state["has_checkout"]
            ), state
            assert state["has_registry"], (
                "Image does not include the latest routes/operations refactor"
            )
            return "Non-root runtime, refactored handlers present, no source checkout/Git required; PORT=8080 works"

        check("Container startup and packaging", startup)

        def docs():
            assert client.get("/docs").status_code == 200
            schema = expect(client.get("/openapi.json"))
            assert len(schema["paths"]) == 14, list(schema["paths"])
            info = expect(client.get("/v1/capabilities"))
            assert info["concurrency_per_instance"] == 1 and info["solver"] == "highs"
            return {
                "openapi_paths": len(schema["paths"]),
                "pypsa_version": info["pypsa_version"],
            }

        check("Health, Swagger, OpenAPI, and capabilities", docs)

        def auth():
            paths = [
                ("GET", "/v1/capabilities"),
                ("GET", "/v1/components/Bus"),
                ("GET", "/v1/examples"),
                ("GET", "/v1/examples/two-bus"),
                ("POST", "/v1/editor/catalog"),
                ("POST", "/v1/editor/evaluate"),
                *[
                    ("POST", f"/v1/networks/{name}")
                    for name in [
                        "inspect",
                        "export",
                        "validate",
                        "optimize",
                        "power-flow",
                        "statistics",
                        "edit",
                    ]
                ],
            ]
            for method, path in paths:
                expect(
                    client.request(
                        method, path, headers={"Authorization": "Bearer wrong"}, json={}
                    ),
                    401,
                )
            with httpx.Client(base_url=client.base_url) as anonymous:
                expect(anonymous.get("/v1/capabilities"), 401)
                expect(anonymous.get("/healthz"))
            return "All 13 protected endpoint patterns reject invalid credentials; health remains public"

        check("Authentication across every protected route", auth)

        def metadata():
            names = [
                "Carrier",
                "Bus",
                "Generator",
                "Load",
                "StorageUnit",
                "Store",
                "Link",
                "Line",
                "Transformer",
                "GlobalConstraint",
                "ShuntImpedance",
                "LineType",
                "TransformerType",
                "Process",
            ]
            for name in names:
                data = expect(client.get(f"/v1/components/{name}"))
                assert data["component"] == name
                assert "unit" in data["attributes"]["columns"]
                assert "status" in data["attributes"]["columns"]
            return f"All {len(names)} supported component types returned their metadata"

        check("All component metadata", metadata)

        def editor():
            source = model()
            described = expect(
                client.post(
                    "/v1/editor/catalog",
                    json={"source": source, "components": ["Generator"]},
                )
            )
            fields = described["component_types"]["Generator"]["fields"]
            assert fields["p_nom_max"]["default"] == {"kind": "positiveInfinity"}
            body = {
                "source": source,
                "components": ["Generator"],
                "operations": [
                    {
                        "op": "set",
                        "component": "Generator",
                        "name": "supply",
                        "field": "p_nom_extendable",
                        "value": True,
                    },
                    {
                        "op": "set",
                        "component": "Generator",
                        "name": "supply",
                        "field": "p_nom_max",
                        "value": 200,
                    },
                ],
            }
            evaluated = expect(client.post("/v1/editor/evaluate", json=body))
            assert evaluated["valid"] and evaluated["artifact"] is None
            assert evaluated["catalog"]["instances"][0]["fields"]["p_nom"]["overridden"]
            edited = expect(client.post("/v1/networks/edit", json=body))
            assert edited["applied"] and edited["artifact"]["kind"] == "netcdf"
            assert post(client, "validate", edited["artifact"])["valid"]
            invalid = expect(
                client.post(
                    "/v1/networks/edit",
                    json={
                        "source": source,
                        "operations": [
                            {
                                "op": "set",
                                "component": "Generator",
                                "name": "supply",
                                "field": "p_nom_opt",
                                "value": 1,
                            },
                        ],
                    },
                )
            )
            assert not invalid["valid"] and invalid["artifact"] is None
            return "Catalog, conditional forms, transactional edits, and native artifact reload verified"

        check("Editor catalogue, evaluation, and native edits", editor)

        examples = {}

        def load_examples():
            listing = expect(client.get("/v1/examples"))["examples"]
            assert {item["id"] for item in listing} == {
                "two-bus",
                "ac-dc-meshed",
                "storage-hvdc",
                "model-energy",
            }
            for item in listing:
                examples[item["id"]] = expect(client.get(f"/v1/examples/{item['id']}"))
            assert (
                len(
                    examples["ac-dc-meshed"]["network"]["components"]["Bus"]["static"][
                        "index"
                    ]
                )
                == 9
            )
            return "two-bus and all three compatible bundled upstream networks loaded"

        check("All bundled examples", load_examples)

        def run_example(name):
            example = examples[name]
            source = example["artifact"]
            validation = post(client, "validate", source)
            assert validation["valid"], validation
            started = time.monotonic()
            result = post(client, "optimize", source, time_limit=180)
            solve_seconds = time.monotonic() - started
            assert result["solution_available"], {
                key: value for key, value in result.items() if key != "artifact"
            }
            assert math.isfinite(result["objective"])
            started = time.monotonic()
            stats = post(
                client,
                "statistics",
                result["artifact"],
                statistics=[
                    {"metric": "supply", "groupby_time": False},
                    {"metric": "optimal_capacity"},
                    {"metric": "system_cost"},
                ],
            )
            assert stats["statistics"][0]["parameters"]["groupby_time"] is False
            assert len(stats["statistics"][0]["table"]["columns"]) == len(
                example["network"]["snapshots"]["index"]
            )
            return {
                "snapshots": len(example["network"]["snapshots"]["index"]),
                "components": sum(
                    len(c["static"]["index"])
                    for c in example["network"]["components"].values()
                ),
                "objective": result["objective"],
                "solve_request_seconds": round(solve_seconds, 2),
                "statistics_request_seconds": round(time.monotonic() - started, 2),
                "solved_netcdf_bytes": result["artifact"]["size_bytes"],
            }

        for name in ("ac-dc-meshed", "storage-hvdc", "model-energy"):
            check(f"Full upstream example: {name}", lambda name=name: run_example(name))

        def unsupported_examples():
            root = Path(__file__).resolve().parents[2]
            results = {}
            for name, expected in [
                ("scigrid-de", "component"),
                ("stochastic-network", "deterministic"),
            ]:
                data = (root / "examples/networks" / name / f"{name}.nc").read_bytes()
                response = client.post(
                    "/v1/networks/inspect",
                    json={
                        "source": {
                            "kind": "netcdf",
                            "data": base64.b64encode(data).decode(),
                        }
                    },
                )
                body = expect(response, 422)
                assert expected in body["detail"], body
                results[name] = body["detail"]
            return results

        check(
            "Out-of-scope upstream examples rejected explicitly", unsupported_examples
        )

        def roundtrip():
            source = examples["two-bus"]["artifact"]
            content = base64.b64decode(source["data"], validate=True)
            assert len(content) == source["size_bytes"]
            assert hashlib.sha256(content).hexdigest() == source["sha256"]
            original = post(client, "inspect", source)["network"]
            exported = post(client, "export", source)["artifact"]
            assert post(client, "inspect", exported)["network"] == original
            assert post(client, "validate", source)["valid"]
            declared = post(client, "export", model())["artifact"]
            assert post(client, "validate", declared)["valid"]
            return "JSON construction, binary integrity, inspection, export/import, and validation passed"

        check("Network creation and NetCDF round trips", roundtrip)

        solved = {}

        def dispatch():
            solved.update(
                post(
                    client,
                    "optimize",
                    examples["two-bus"]["artifact"],
                    statistics=[{"metric": "supply"}],
                )
            )
            assert (
                solved["solution_available"]
                and solved["termination_condition"] == "optimal"
            )
            near(solved["objective"], 1400)
            data = post(
                client, "inspect", solved["artifact"], include_time_series=True
            )["network"]
            dispatch = data["components"]["Generator"]["dynamic"]["p"]
            values = dict(zip(dispatch["columns"], dispatch["data"][0], strict=True))
            near(values["cheap"], 50)
            near(values["expensive"], 30)
            return {"objective": solved["objective"], "dispatch": values}

        check("Economic dispatch and line capacity constraint", dispatch)

        def all_statistics():
            metrics = [
                "installed_capacity",
                "optimal_capacity",
                "expanded_capacity",
                "supply",
                "withdrawal",
                "energy_balance",
                "curtailment",
                "capacity_factor",
                "capex",
                "opex",
                "system_cost",
                "revenue",
                "market_value",
                "prices",
                "transmission",
            ]
            result = post(
                client,
                "statistics",
                solved["artifact"],
                statistics=[{"metric": m} for m in metrics],
            )
            assert [item["metric"] for item in result["statistics"]] == metrics
            supply = next(
                item for item in result["statistics"] if item["metric"] == "supply"
            )
            assert supply == solved["statistics"][0]
            return "All 15 exposed statistics; saved supply matches the solve response"

        check("Every statistics method on a saved solution", all_statistics)

        def weighted():
            source = model(demand=[10, 20])
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
            result = post(
                client,
                "optimize",
                source,
                statistics=[
                    {
                        "metric": "supply",
                        "components": ["Generator"],
                        "groupby": "name",
                    },
                    {
                        "metric": "supply",
                        "components": ["Generator"],
                        "groupby": "name",
                        "groupby_time": False,
                    },
                ],
            )
            near(result["objective"], 800)
            near(result["statistics"][0]["table"]["data"][0][0], 80)
            assert result["statistics"][1]["parameters"]["groupby_time"] is False
            assert result["statistics"][1]["table"]["data"][0] == [10, 20], result[
                "statistics"
            ]
            assert "name" in result["statistics"][1]["table"]["index_names"]
            return "Weighted energy=80, objective=800, unaggregated dispatch=[10,20]"

        check("Datetime profiles, weights, grouping, and chart time series", weighted)

        def filtering():
            source = model(demand=[10, 20])
            source["model"].update(
                {
                    "snapshots": ["a", "b"],
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
                    "bus_carrier": "AC",
                    "groupby": "name",
                    "groupby_time": False,
                },
                {
                    "metric": "supply",
                    "components": ["Generator"],
                    "bus_carrier": "AC",
                    "groupby": "bus",
                    "groupby_time": "mean",
                },
                {
                    "metric": "withdrawal",
                    "components": ["Load"],
                    "bus_carrier": "heat",
                    "groupby": "name",
                    "groupby_time": False,
                },
            ]
            result = post(client, "optimize", source, statistics=queries)
            stats = result["statistics"]
            assert [item["table"]["data"] for item in stats] == [
                [[10.0, 20.0]],
                [[16.0]],
                [[3.0, 7.0]],
            ], stats
            for query, item in zip(queries, stats, strict=True):
                assert item["parameters"] == {
                    key: value for key, value in query.items() if key != "metric"
                }
            assert (
                post(client, "statistics", result["artifact"], statistics=queries)[
                    "statistics"
                ]
                == stats
            )
            return "Component/carrier filters, name/bus grouping, weighted mean and time series preserved on both endpoints"

        check("Cross-carrier statistics filter regressions", filtering)

        def unsupported_statistics():
            for query in [
                {"metric": "installed_capacity", "groupby_time": False},
                {"metric": "prices", "components": ["Generator"]},
            ]:
                body = expect(
                    client.post(
                        "/v1/networks/statistics",
                        json={"source": solved["artifact"], "statistics": [query]},
                    ),
                    422,
                )
                assert "unexpected keyword" in body["detail"]
            return "Unsupported arguments return 422 rather than silently disappearing"

        check("Unsupported statistics options", unsupported_statistics)

        def capacity():
            source = model(capacity=0)
            source["model"]["components"][-1]["attributes"].update(
                {"p_nom_extendable": True, "p_nom_max": 100, "capital_cost": 20}
            )
            result = post(
                client,
                "optimize",
                source,
                statistics=[
                    {
                        "metric": "optimal_capacity",
                        "components": ["Generator"],
                        "groupby": "name",
                    }
                ],
            )
            assert result["solution_available"]
            near(result["objective"], 300)
            near(result["statistics"][0]["table"]["data"][0][0], 10)
            return "Optimal installed capacity=10; objective=300"

        check("Capacity expansion optimisation", capacity)

        def storage():
            source = model(demand=[5, 5, 5, 5], capacity=20)
            source["model"]["snapshots"] = ["0", "1", "2", "3"]
            source["model"]["components"][-1]["attributes"]["p_max_pu"] = [1, 0, 0, 1]
            source["model"]["components"].append(
                {
                    "type": "StorageUnit",
                    "name": "battery",
                    "attributes": {
                        "bus": "bus",
                        "p_nom": 10,
                        "max_hours": 2,
                        "state_of_charge_initial": 0,
                        "efficiency_store": 1,
                        "efficiency_dispatch": 1,
                    },
                }
            )
            result = post(client, "optimize", source)
            assert result["solution_available"]
            near(result["objective"], 200)
            data = post(
                client, "inspect", result["artifact"], include_time_series=True
            )["network"]
            soc = [
                row[0]
                for row in data["components"]["StorageUnit"]["dynamic"][
                    "state_of_charge"
                ]["data"]
            ]
            for actual, expected in zip(soc, [10, 5, 0, 0], strict=True):
                near(actual, expected)
            return {"objective": result["objective"], "state_of_charge": soc}

        check("Storage charging, discharging, and saved state-of-charge", storage)

        def conversion():
            source = model(demand=4, capacity=10)
            source["model"]["components"][0]["attributes"]["bus"] = "hydrogen"
            source["model"]["components"].extend(
                [
                    {"type": "Carrier", "name": "hydrogen"},
                    {
                        "type": "Bus",
                        "name": "hydrogen",
                        "attributes": {"carrier": "hydrogen"},
                    },
                    {
                        "type": "Link",
                        "name": "electrolyser",
                        "attributes": {
                            "bus0": "bus",
                            "bus1": "hydrogen",
                            "p_nom": 10,
                            "efficiency": 0.8,
                        },
                    },
                ]
            )
            result = post(client, "optimize", source)
            assert result["solution_available"]
            near(result["objective"], 50)
            data = post(
                client, "inspect", result["artifact"], include_time_series=True
            )["network"]
            near(data["components"]["Link"]["dynamic"]["p0"]["data"][0][0], 5)
            near(data["components"]["Link"]["dynamic"]["p1"]["data"][0][0], -4)
            return "5 units electricity -> 4 units hydrogen; objective=50"

        check("Cross-carrier link conversion and port sign conventions", conversion)

        def powerflow(mode):
            source = model(demand=40)
            source["model"]["components"][0]["attributes"]["bus"] = "east"
            source["model"]["components"][2]["attributes"]["v_nom"] = 220
            source["model"]["components"][-1]["attributes"].update(
                {"p_set": 40, "control": "Slack"}
            )
            source["model"]["components"].extend(
                [
                    {
                        "type": "Bus",
                        "name": "east",
                        "attributes": {"v_nom": 220, "carrier": "AC"},
                    },
                    {
                        "type": "Line",
                        "name": "line",
                        "attributes": {
                            "bus0": "bus",
                            "bus1": "east",
                            "x": 0.1,
                            "r": 0.01,
                            "s_nom": 100,
                        },
                    },
                ]
            )
            result = post(client, "power-flow", source, mode=mode)
            assert result["solution_available"], result
            data = post(
                client, "inspect", result["artifact"], include_time_series=True
            )["network"]
            flow = data["components"]["Line"]["dynamic"]["p0"]["data"][0][0]
            assert 39.99 < flow < 40.1, flow
            if mode == "nonlinear":
                assert all(
                    all(row) for row in result["convergence"]["converged"]["data"]
                )
            return {"line_flow": flow, "converged": result["solution_available"]}

        check("Linear power flow on an electrical line", lambda: powerflow("linear"))
        check("Nonlinear AC power flow and convergence", lambda: powerflow("nonlinear"))

        def infeasible():
            result = post(client, "optimize", model(capacity=1))
            assert result["termination_condition"] == "infeasible", result
            assert not result["solution_available"] and result["artifact"] is None
            return "Infeasible model has no claimed solution or solved artifact"

        check("Infeasible optimisation outcome", infeasible)

        def invalid():
            source = model()
            source["model"]["components"][0]["attributes"]["bus"] = "missing"
            report = post(client, "validate", source)
            assert not report["valid"] and report["diagnostics"]
            expect(client.post("/v1/networks/optimize", json={"source": source}), 422)
            expect(client.get("/v1/examples/unknown"), 404)
            expect(client.get("/v1/components/unknown"), 422)
            expect(
                client.post(
                    "/v1/networks/inspect",
                    json={"source": {"kind": "netcdf", "data": "bad-base64!"}},
                ),
                422,
            )
            corrupt = {**examples["two-bus"]["artifact"], "sha256": "0" * 64}
            expect(client.post("/v1/networks/inspect", json={"source": corrupt}), 422)
            for mutation in ("unknown", "series", "duplicate", "output"):
                source = model()
                attrs = source["model"]["components"][-1]["attributes"]
                if mutation == "unknown":
                    attrs["not_a_pypsa_attribute"] = 1
                if mutation == "series":
                    attrs["p_max_pu"] = [0.1, 0.2]
                if mutation == "duplicate":
                    source["model"]["components"].append(
                        copy.deepcopy(source["model"]["components"][-1])
                    )
                if mutation == "output":
                    attrs["p_nom_opt"] = 12
                expect(client.post("/v1/networks/export", json={"source": source}), 422)
            expect(
                client.post(
                    "/v1/networks/inspect",
                    content="{bad",
                    headers={"Content-Type": "application/json"},
                ),
                422,
            )
            return "Missing buses, invalid IDs/attributes, output injection, series mismatch, malformed JSON/NetCDF, checksum mismatch rejected"

        check("Validation and malformed-input errors", invalid)

        def isolated():
            pids = []
            with ThreadPoolExecutor(max_workers=1) as pool:
                for _ in range(2):
                    pending = pool.submit(client.get, "/v1/components/Generator")
                    for _ in range(40):
                        state = inspect_state(cid)
                        if state["workers"]:
                            pids.append(state["workers"][0])
                            break
                        if pending.done():
                            break
                        time.sleep(0.03)
                    assert len(pids) > 0, "Did not observe the worker"
                    before = time.monotonic()
                    expect(client.get("/healthz"))
                    assert time.monotonic() - before < 2
                    expect(client.get("/v1/components/Bus"), 429)
                    expect(pending.result())
                    assert_clean(cid)
            assert len(pids) == 2 and pids[0] != pids[1], pids
            return {
                "distinct_worker_pids": pids,
                "health_responsive": True,
                "busy_status": 429,
            }

        check("Fresh subprocesses, busy admission, and concurrent health", isolated)

        def disconnect():
            try:
                client.get("/v1/components/Generator", timeout=0.05)
            except httpx.ReadTimeout:
                pass
            else:
                raise AssertionError(
                    "Request finished before disconnect could be tested"
                )
            for _ in range(30):
                time.sleep(0.1)
                state = inspect_state(cid)
                if not state["workers"] and not state["temporary_jobs"]:
                    break
            assert_clean(cid)
            expect(client.get("/v1/components/Bus"))
            return "Client disconnect killed the child, removed scratch files, and released the slot"

        check("Disconnect cancellation and subsequent recovery", disconnect)

        def final_health():
            state = assert_clean(cid)
            for _ in range(35):
                health = (
                    json.loads(docker("inspect", cid))[0]["State"]
                    .get("Health", {})
                    .get("Status")
                )
                if health == "healthy":
                    break
                time.sleep(1)
            assert health == "healthy", health
            return {
                "docker_health": health,
                "leftover_workers": state["workers"],
                "temporary_jobs": state["temporary_jobs"],
                "peak_memory_bytes": state["peak_memory_bytes"],
            }

        check("Docker HEALTHCHECK and final cleanup", final_health)


def limit_suite():
    with instance(
        MAX_BODY_BYTES="2048",
        MAX_RESULT_BYTES="2048",
        MAX_COMPONENTS="4",
        MAX_SNAPSHOTS="2",
        MAX_CELLS="4",
    ) as (cid, client):

        def limits():
            expect(
                client.post(
                    "/v1/networks/inspect",
                    content="x" * 2049,
                    headers={"Content-Type": "application/json"},
                ),
                413,
            )
            expect(client.get("/v1/components/Generator"), 413)
            oversized = model()
            oversized["model"]["components"].append({"type": "Bus", "name": "extra"})
            expect(client.post("/v1/networks/inspect", json={"source": oversized}), 422)
            oversized = model()
            oversized["model"]["snapshots"] = ["a", "b", "c"]
            expect(client.post("/v1/networks/inspect", json={"source": oversized}), 422)
            oversized["model"]["snapshots"] = ["a", "b"]
            expect(client.post("/v1/networks/inspect", json={"source": oversized}), 422)
            assert_clean(cid)
            return "Body/result caps=413; component/snapshot/cell caps=422; no leftover work"

        check("Configured transport and model-size limits", limits)

    with instance(REQUEST_TIMEOUT_SECONDS="1") as (cid, client):

        def deadline():
            source = model(demand=100)
            source["model"]["snapshots"] = [str(i) for i in range(96)]
            for i in range(300):
                source["model"]["components"].append(
                    {
                        "type": "Generator",
                        "name": f"g-{i}",
                        "attributes": {
                            "bus": "bus",
                            "p_nom": 1,
                            "marginal_cost": i + 20,
                        },
                    }
                )
            start = time.monotonic()
            expect(client.post("/v1/networks/optimize", json={"source": source}), 504)
            elapsed = time.monotonic() - start
            assert elapsed < 5, elapsed
            assert_clean(cid)
            expect(client.get("/healthz"))
            return {
                "http_status": 504,
                "seconds": round(elapsed, 2),
                "worker_reaped": True,
            }

        check("Hard request deadline kills and reaps computation", deadline)


if __name__ == "__main__":
    IMAGE = docker(
        "image",
        "inspect",
        sys.argv[1] if len(sys.argv) > 1 else IMAGE,
        "--format",
        "{{.Id}}",
    )
    try:
        main_suite()
        limit_suite()
    finally:
        for cid in list(CONTAINERS):
            subprocess.run(["docker", "rm", "-f", cid], capture_output=True)
        REPORT.write_text(
            json.dumps({"image": IMAGE, "results": RESULTS}, indent=2), encoding="utf-8"
        )
    failures = [result for result in RESULTS if result["status"] != "PASS"]
    print(
        f"\nSUMMARY: {len(RESULTS) - len(failures)}/{len(RESULTS)} test groups passed. Report: {REPORT}"
    )
    raise SystemExit(bool(failures))
