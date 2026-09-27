# SPDX-FileCopyrightText: PyPSA Contributors
# SPDX-License-Identifier: MIT
"""Network inspection, export, validation, and statistics handlers."""

from app import network
from app.operations import ExecutionContext
from app.schemas import (
    InspectRequest,
    NetworkRequest,
    StatisticsRequest,
    ValidateRequest,
)


def inspect_network(body: InspectRequest, context: ExecutionContext) -> dict:
    n = network.load(body.source, context.root, context.limits)
    return {"network": network.project(n, body.include_time_series)}


def export_network(body: NetworkRequest, context: ExecutionContext) -> dict:
    n = network.load(body.source, context.root, context.limits)
    return {"artifact": network.artifact(n, context.root, context.limits)}


def validate_network(body: ValidateRequest, context: ExecutionContext) -> dict:
    n = network.load(body.source, context.root, context.limits)
    return network.validate(n, body.strict)


def calculate_statistics(body: StatisticsRequest, context: ExecutionContext) -> dict:
    n = network.load(body.source, context.root, context.limits)
    return {"statistics": network.statistics(n, body.statistics)}
