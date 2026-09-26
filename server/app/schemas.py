# SPDX-FileCopyrightText: PyPSA Contributors
# SPDX-License-Identifier: MIT
"""Version-one transport models. Arbitrary Python and solver callbacks are excluded."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ComponentRequest(StrictModel):
    component: str


class ExampleRequest(StrictModel):
    name: str


class Component(StrictModel):
    type: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=256)
    attributes: dict[str, JsonValue] = Field(default_factory=dict)


class NetworkModel(StrictModel):
    name: str = "Untitled network"
    snapshots: list[str] = Field(default_factory=lambda: ["now"], min_length=1)
    snapshot_kind: Literal["labels", "datetime"] = "labels"
    snapshot_weightings: dict[
        Literal["objective", "stores", "generators"], list[float]
    ] = Field(default_factory=dict)
    components: list[Component] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_and_aligned(self) -> "NetworkModel":
        if len(set(self.snapshots)) != len(self.snapshots):
            raise ValueError("Snapshots must be unique.")
        ids = [(component.type, component.name) for component in self.components]
        if len(set(ids)) != len(ids):
            raise ValueError("Component names must be unique within each type.")
        for weights in self.snapshot_weightings.values():
            if len(weights) != len(self.snapshots) or any(
                value <= 0 for value in weights
            ):
                raise ValueError(
                    "Snapshot weightings must be positive and aligned to snapshots."
                )
        return self


class ModelSource(StrictModel):
    kind: Literal["model"] = "model"
    model: NetworkModel


class NetCDFSource(StrictModel):
    kind: Literal["netcdf"] = "netcdf"
    data: str = Field(
        min_length=1,
        description="Base64-encoded native NetCDF bytes; never a path or URL.",
    )
    sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    size_bytes: int | None = Field(default=None, ge=0)


Source = Annotated[ModelSource | NetCDFSource, Field(discriminator="kind")]


class NetworkRequest(StrictModel):
    source: Source


class InspectRequest(NetworkRequest):
    include_time_series: bool = False


class ValidateRequest(NetworkRequest):
    strict: bool = False


Metric = Literal[
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


class Statistic(StrictModel):
    metric: Metric
    components: list[str] | None = None
    groupby: Literal["carrier", "bus", "bus_carrier", "name", "unit"] = "carrier"
    groupby_time: Literal["sum", "mean", False] = "sum"
    bus_carrier: str | None = None


class StatisticsRequest(NetworkRequest):
    statistics: list[Statistic] = Field(min_length=1, max_length=20)


class OptimizeRequest(NetworkRequest):
    time_limit: float = Field(default=120, gt=0)
    mip_gap: float = Field(default=0.01, ge=0, le=1)
    statistics: list[Statistic] = Field(default_factory=list, max_length=20)


class PowerFlowRequest(NetworkRequest):
    mode: Literal["linear", "nonlinear"] = "linear"
    x_tol: float = Field(default=1e-6, gt=0, le=0.1)
