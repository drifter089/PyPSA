# SPDX-FileCopyrightText: PyPSA Contributors
# SPDX-License-Identifier: MIT
"""Network data and statistics endpoints."""

from fastapi import APIRouter, Request

from app.editor.schemas import EditRequest, EditResponse
from app.execution import execute
from app.operations import Operation
from app.schemas import (
    InspectRequest,
    NetworkRequest,
    StatisticsRequest,
    ValidateRequest,
)

router = APIRouter(prefix="/v1/networks", tags=["Networks"])


@router.post("/edit", response_model=EditResponse)
async def edit_network(body: EditRequest, request: Request):
    return await execute(request, Operation.EDIT_NETWORK, body)


@router.post("/inspect")
async def inspect_network(body: InspectRequest, request: Request):
    return await execute(request, Operation.INSPECT, body)


@router.post("/export")
async def export_network(body: NetworkRequest, request: Request):
    return await execute(request, Operation.EXPORT, body)


@router.post("/validate")
async def validate_network(body: ValidateRequest, request: Request):
    return await execute(request, Operation.VALIDATE, body)


@router.post("/statistics")
async def network_statistics(body: StatisticsRequest, request: Request):
    return await execute(request, Operation.STATISTICS, body)
