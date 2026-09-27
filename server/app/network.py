# SPDX-FileCopyrightText: PyPSA Contributors
# SPDX-License-Identifier: MIT
"""Thin adapters over public PyPSA APIs; imported only in computation children."""

import base64
import hashlib
import json
import logging
import math
import warnings
from pathlib import Path

import pandas as pd
import pypsa

from app.examples import EXAMPLES
from app.schemas import ModelSource, NetworkModel, Source, Statistic

COMPONENTS = {
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
}


def table(data: pd.DataFrame | pd.Series) -> dict:
    """Preserve explicit axes, including MultiIndexes; non-finite cells become null."""
    frame = data.to_frame("value") if isinstance(data, pd.Series) else data
    result = json.loads(
        frame.to_json(orient="split", date_format="iso", default_handler=str)
    )
    result["index_names"] = list(frame.index.names)
    result["column_names"] = list(frame.columns.names)
    return result


def bounded(n: pypsa.Network, limits: dict) -> None:
    count = sum(
        len(component.static)
        for component in n.components
        if component.name in COMPONENTS - {"LineType", "TransformerType"}
    )
    if count > limits["max_components"] or len(n.snapshots) > limits["max_snapshots"]:
        raise ValueError("Network exceeds component or snapshot limits.")
    if count * len(n.snapshots) > limits["max_cells"]:
        raise ValueError("Network exceeds the component × snapshot budget.")
    if n.has_scenarios or n.has_investment_periods:
        raise ValueError(
            "API v1 supports deterministic, single-investment-period networks only."
        )


def from_model(model: NetworkModel, limits: dict) -> pypsa.Network:
    if (
        len(model.components) > limits["max_components"]
        or len(model.snapshots) > limits["max_snapshots"]
    ):
        raise ValueError("Model exceeds component or snapshot limits.")
    if len(model.components) * len(model.snapshots) > limits["max_cells"]:
        raise ValueError("Model exceeds the component × snapshot budget.")
    n = pypsa.Network(name=model.name)
    snapshots = model.snapshots
    if model.snapshot_kind == "datetime":
        snapshots = pd.DatetimeIndex(pd.to_datetime(snapshots))
        if snapshots.tz is not None:
            raise ValueError(
                "Use timezone-naive timestamps with an explicit external timezone convention."
            )
        if not snapshots.is_unique or not snapshots.is_monotonic_increasing:
            raise ValueError("Datetime snapshots must be unique and increasing.")
    n.set_snapshots(snapshots)
    for column, weights in model.snapshot_weightings.items():
        n.snapshot_weightings[column] = weights
    # Bus references should resolve independently of request component ordering.
    priority = {"Carrier": 0, "Bus": 1, "LineType": 2, "TransformerType": 2}
    for component in sorted(model.components, key=lambda c: priority.get(c.type, 3)):
        if component.type not in COMPONENTS:
            raise ValueError(f"Unsupported component type: {component.type}")
        from app.editor.metadata import expand_ports, scalar_value

        expand_ports(n, component.type, component.attributes)
        defaults = n.components[component.type].defaults
        for attribute, value in component.attributes.items():
            if attribute == "name" or attribute not in defaults.index:
                raise ValueError(f"Unknown attribute {component.type}.{attribute}")
            if attribute in defaults.index and str(
                defaults.at[attribute, "status"]
            ).startswith("Output"):
                raise ValueError(
                    f"Output attribute cannot be supplied as an input: {attribute}"
                )
            if isinstance(value, list):
                if len(value) != len(n.snapshots):
                    raise ValueError(
                        f"Time series {component.name}.{attribute} must match snapshots."
                    )
                if (
                    attribute in defaults.index
                    and not defaults.at[attribute, "varying"]
                ):
                    raise ValueError(
                        f"Attribute {attribute} does not accept time series."
                    )
                for item in value:
                    scalar_value(defaults.loc[attribute], item)
            else:
                scalar_value(defaults.loc[attribute], value)
        n.add(component.type, component.name, **component.attributes)
    return n


def load(source: Source, root: Path, limits: dict) -> pypsa.Network:
    if isinstance(source, ModelSource):
        n = from_model(source.model, limits)
    else:
        try:
            content = base64.b64decode(source.data, validate=True)
        except ValueError as exc:
            raise ValueError("Invalid base64 NetCDF payload.") from exc
        if len(content) > limits["max_body_bytes"]:
            raise ValueError("NetCDF input exceeds the size limit.")
        if source.sha256 and hashlib.sha256(content).hexdigest() != source.sha256:
            raise ValueError("NetCDF checksum does not match the supplied bytes.")
        path = root / "input.nc"
        path.write_bytes(content)
        try:
            n = pypsa.Network(path)
        except Exception as exc:
            raise ValueError("Unable to import this PyPSA NetCDF file.") from exc
    bounded(n, limits)
    return n


def artifact(n: pypsa.Network, root: Path, limits: dict) -> dict:
    path = root / "network.nc"
    n.export_to_netcdf(path).close()
    if path.stat().st_size * 4 // 3 > limits["max_result_bytes"]:
        raise ValueError("Encoded network exceeds the result size limit.")
    content = path.read_bytes()
    return {
        "kind": "netcdf",
        "data": base64.b64encode(content).decode("ascii"),
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
    }


def project(n: pypsa.Network, include_time_series: bool = False) -> dict:
    components = {}
    for component in n.components:
        if component.static.empty or component.name in {
            "LineType",
            "TransformerType",
            "SubNetwork",
        }:
            continue
        item = {"static": table(component.static)}
        if include_time_series:
            item["dynamic"] = {
                key: table(value)
                for key, value in component.dynamic.items()
                if not value.empty
            }
        components[component.name] = item
    return {
        "name": n.name,
        "snapshots": table(n.snapshot_weightings),
        "components": components,
    }


class Diagnostics(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.items: list[dict] = []

    def emit(self, record: logging.LogRecord) -> None:
        if len(self.items) < 100:
            self.items.append(
                {"level": record.levelname.lower(), "message": record.getMessage()}
            )


def validate(n: pypsa.Network, strict: bool = False) -> dict:
    capture = Diagnostics()
    logger = logging.getLogger("pypsa.consistency")
    logger.addHandler(capture)
    valid = True
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            try:
                n.consistency_check(
                    strict=["all"]
                    if strict
                    else [
                        "unknown_buses",
                        "time_series",
                        "static_power_attrs",
                        "time_series_power_attrs",
                        "dispatch_delays",
                        "maintenance",
                        "phase_shift_bounds",
                    ]
                )
            except pypsa.consistency.ConsistencyError as exc:
                valid = False
                capture.items.append({"level": "error", "message": str(exc)})
            capture.items.extend(
                {"level": "warning", "message": str(w.message)} for w in caught[:100]
            )
    finally:
        logger.removeHandler(capture)
    return {"valid": valid, "diagnostics": capture.items}


def statistics(n: pypsa.Network, queries: list[Statistic]) -> list[dict]:
    results = []
    for query in queries:
        method = getattr(n.statistics, query.metric)
        # PyPSA wraps statistics with (*args, **kwargs). Signature-based filtering
        # would discard real options. Let its public API validate supplied options.
        kwargs = query.model_dump(exclude={"metric"}, exclude_none=True)
        data = method(**kwargs)
        results.append(
            {"metric": query.metric, "parameters": kwargs, "table": table(data)}
        )
    return results


def example(name: str) -> pypsa.Network:
    if name not in EXAMPLES:
        raise ValueError("Unknown example.")
    path = EXAMPLES[name]["path"]
    if path is not None:
        return pypsa.Network(Path(__file__).resolve().parents[2] / path)
    n = pypsa.Network(name="Two-bus dispatch")
    n.add("Carrier", "AC")
    n.add("Carrier", "gas")
    n.add("Bus", ["west", "east"], v_nom=220, carrier="AC")
    n.add("Generator", "cheap", bus="west", carrier="gas", p_nom=100, marginal_cost=10)
    n.add(
        "Generator", "expensive", bus="east", carrier="gas", p_nom=100, marginal_cost=30
    )
    n.add("Load", "demand", bus="east", p_set=80)
    n.add("Line", "connection", bus0="west", bus1="east", x=0.1, r=0.01, s_nom=50)
    return n


def finite(value: float) -> float | None:
    return float(value) if math.isfinite(value) else None
