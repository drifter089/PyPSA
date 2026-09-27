# SPDX-FileCopyrightText: PyPSA Contributors
# SPDX-License-Identifier: MIT
"""JSON-only editor contracts, safe to import in the HTTP process."""

from typing import Annotated, Literal

from pydantic import Field, JsonValue, model_validator

from app.schemas import NetCDFSource, Source, StrictModel

Analysis = Literal["optimize", "lpf", "pf"]
Name = Annotated[str, Field(min_length=1, max_length=256)]


class SpecialValue(StrictModel):
    kind: Literal["unset", "positiveInfinity", "negativeInfinity"]


Value = bool | int | float | str | SpecialValue


class Condition(StrictModel):
    op: Literal["eq", "ne", "in", "isSet", "all", "any", "not"]
    ref: str | None = None
    value: JsonValue = None
    args: list["Condition"] = Field(default_factory=list)

    @model_validator(mode="after")
    def structure(self):
        if self.op in {"all", "any", "not"}:
            if self.ref is not None or not self.args:
                raise ValueError("Logical conditions require args, not ref.")
            if self.op == "not" and len(self.args) != 1:
                raise ValueError("not requires exactly one argument.")
        elif not self.ref or self.args:
            raise ValueError("Comparison conditions require ref, not args.")
        if self.op == "in" and not isinstance(self.value, list):
            raise ValueError("in requires an array value.")
        return self


class FieldRule(StrictModel):
    id: str
    effect: Literal["applicable", "overridden", "required"]
    when: Condition
    reason: str
    source: str = "editor-policy"


class FieldDefinition(StrictModel):
    key: str
    native_type: str
    data_type: str
    unit: str | None
    default: JsonValue
    description: str
    native_status: str
    role: Literal["input", "output"]
    native_required: bool
    static: bool
    varying: bool
    native_value_modes: list[str]
    editor_value_modes: list[str]
    support: Literal["editable", "output", "placeholder", "deprecated", "read_only"]
    group: str
    control: str
    minimum: float | None = None
    maximum: float | None = None
    create_only: bool = False
    choice_set: str | None = None
    rules: list[FieldRule] = Field(default_factory=list)
    source: str


class PortDefinition(StrictModel):
    attribute: str
    required: bool
    target: str = "Bus"
    max_connections: int = 1
    coefficient: str | None = None
    delay: str | None = None
    cyclic_delay: str | None = None
    reference_role: str


class ComponentDefinition(StrictModel):
    name: str
    list_name: str
    category: str | None
    description: str
    create: bool
    fields: dict[str, FieldDefinition]
    ports: list[PortDefinition]
    additional_ports: bool
    groups: list[str]


class Diagnostic(StrictModel):
    severity: Literal["error", "warning", "info"]
    code: str
    message: str
    source: str = "editor-policy"
    component: str | None = None
    name: str | None = None
    field: str | None = None
    operation: int | None = None


class FieldState(StrictModel):
    applicable: bool
    editable: bool
    required: bool
    overridden: bool
    reasons: list[str]
    value_mode: str
    matches_default: bool
    provenance: str = "unknown"


class InstanceState(StrictModel):
    component: str
    name: str
    static: dict[str, JsonValue]
    fields: dict[str, FieldState]
    profiles: dict[str, JsonValue]
    ports: list[dict[str, JsonValue]]


class CatalogRequest(StrictModel):
    source: Source | None = None
    analysis: Analysis = "optimize"
    components: list[str] | None = Field(default=None, max_length=30)
    names: list[str] | None = Field(default=None, max_length=200)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=0, le=200)
    include_profiles: bool = False


class AssetOperation(StrictModel):
    component: Name
    name: Name


class Create(AssetOperation):
    op: Literal["create"]
    attributes: dict[str, Value] = Field(default_factory=dict)


class SetValue(AssetOperation):
    op: Literal["set"]
    field: Name
    value: Value


class SetProfile(AssetOperation):
    op: Literal["set_profile"]
    field: Name
    snapshot_id: str
    values: list[Value] = Field(min_length=1)


class Reset(AssetOperation):
    op: Literal["reset_to_default"]
    field: Name


class Connect(AssetOperation):
    op: Literal["connect"]
    port: Name
    bus: Name


class Disconnect(AssetOperation):
    op: Literal["disconnect"]
    port: Name


class Delete(AssetOperation):
    op: Literal["delete"]


class AddPort(AssetOperation):
    op: Literal["add_port"]
    index: int = Field(ge=2, le=999)
    bus: Name
    coefficient: Value = 1.0
    delay: int = Field(default=0, ge=0)
    cyclic_delay: bool = True


class RemovePort(AssetOperation):
    op: Literal["remove_port"]
    index: int = Field(ge=2, le=999)


class SetSnapshots(StrictModel):
    op: Literal["set_snapshots"]
    snapshots: list[str] = Field(min_length=1)
    snapshot_kind: Literal["labels", "datetime"] = "labels"
    weightings: dict[Literal["objective", "stores", "generators"], list[float]] = Field(
        default_factory=dict
    )


class ApplyPreset(StrictModel):
    op: Literal["apply_preset"]
    preset: Name
    name: Name
    buses: dict[str, Name]
    parameters: dict[str, Value]


Edit = Annotated[
    Create
    | SetValue
    | SetProfile
    | Reset
    | Connect
    | Disconnect
    | Delete
    | AddPort
    | RemovePort
    | SetSnapshots
    | ApplyPreset,
    Field(discriminator="op"),
]


class EditRequest(CatalogRequest):
    operations: list[Edit] = Field(default_factory=list, max_length=500)
    expected_source_id: str | None = None


class CatalogResponse(StrictModel):
    schema_version: int = 1
    pypsa_version: str
    software: dict[str, str]
    context: dict[str, JsonValue]
    component_types: dict[str, ComponentDefinition]
    network_forms: dict[str, JsonValue]
    choice_sets: dict[str, JsonValue]
    connection_rules: list[dict[str, JsonValue]]
    presets: list[dict[str, JsonValue]]
    instances: list[InstanceState]
    total_instances: int
    diagnostics: list[Diagnostic]


class EditResponse(StrictModel):
    schema_version: int = 1
    pypsa_version: str
    valid: bool
    applied: bool
    base_source_id: str
    artifact: NetCDFSource | None = None
    catalog: CatalogResponse
    diagnostics: list[Diagnostic]
    extensions: list[dict[str, JsonValue]] = Field(default_factory=list)
