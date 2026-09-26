# SPDX-FileCopyrightText: PyPSA Contributors
# SPDX-License-Identifier: MIT
"""PyPSA component definitions and trusted example handlers."""

import pypsa
from app import network
from app.operations import ExecutionContext
from app.schemas import ComponentRequest, ExampleRequest


def component_metadata(body: ComponentRequest, context: ExecutionContext) -> dict:
    if body.component not in network.COMPONENTS:
        raise ValueError("Unsupported component type.")
    n = pypsa.Network()
    return {
        "component": body.component,
        "attributes": network.table(n.components[body.component].defaults),
    }


def load_example(body: ExampleRequest, context: ExecutionContext) -> dict:
    n = network.example(body.name)
    network.bounded(n, context.limits)
    return {
        "network": network.project(n),
        "artifact": network.artifact(n, context.root, context.limits),
    }
