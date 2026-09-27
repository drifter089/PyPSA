# SPDX-FileCopyrightText: PyPSA Contributors
# SPDX-License-Identifier: MIT
"""Child-process-only registry of request schemas and their concrete handlers."""

from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel

from app.editor.schemas import CatalogRequest, EditRequest
from app.operations import ExecutionContext, Operation
from app.operations.analyses import optimize_network, power_flow
from app.operations.editor import describe_editor, edit_network, evaluate_editor
from app.operations.metadata import component_metadata, load_example
from app.operations.networks import (
    calculate_statistics,
    export_network,
    inspect_network,
    validate_network,
)
from app.schemas import (
    ComponentRequest,
    ExampleRequest,
    InspectRequest,
    NetworkRequest,
    OptimizeRequest,
    PowerFlowRequest,
    StatisticsRequest,
    ValidateRequest,
)


@dataclass(frozen=True)
class OperationSpec[Payload: BaseModel]:
    schema: type[Payload]
    handler: Callable[[Payload, ExecutionContext], dict]

    def run(self, payload: dict, context: ExecutionContext) -> dict:
        return self.handler(self.schema.model_validate(payload), context)


OPERATIONS = {
    Operation.EDITOR_CATALOG: OperationSpec(CatalogRequest, describe_editor),
    Operation.EDITOR_EVALUATE: OperationSpec(EditRequest, evaluate_editor),
    Operation.EDIT_NETWORK: OperationSpec(EditRequest, edit_network),
    Operation.COMPONENT: OperationSpec(ComponentRequest, component_metadata),
    Operation.EXAMPLE: OperationSpec(ExampleRequest, load_example),
    Operation.INSPECT: OperationSpec(InspectRequest, inspect_network),
    Operation.EXPORT: OperationSpec(NetworkRequest, export_network),
    Operation.VALIDATE: OperationSpec(ValidateRequest, validate_network),
    Operation.OPTIMIZE: OperationSpec(OptimizeRequest, optimize_network),
    Operation.POWER_FLOW: OperationSpec(PowerFlowRequest, power_flow),
    Operation.STATISTICS: OperationSpec(StatisticsRequest, calculate_statistics),
}
