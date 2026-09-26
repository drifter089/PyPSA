# SPDX-FileCopyrightText: PyPSA Contributors
# SPDX-License-Identifier: MIT
"""Optimisation and power-flow endpoints."""

from fastapi import APIRouter, Request

from app.execution import execute
from app.operations import Operation
from app.schemas import OptimizeRequest, PowerFlowRequest

router = APIRouter(prefix="/v1/networks", tags=["Analyses"])


@router.post("/optimize")
async def optimize_network(body: OptimizeRequest, request: Request):
    return await execute(request, Operation.OPTIMIZE, body)


@router.post("/power-flow")
async def power_flow(body: PowerFlowRequest, request: Request):
    return await execute(request, Operation.POWER_FLOW, body)
