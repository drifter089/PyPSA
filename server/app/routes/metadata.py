# SPDX-FileCopyrightText: PyPSA Contributors
# SPDX-License-Identifier: MIT
"""Capability, component, and example endpoints without loading PyPSA in the API."""

from importlib.metadata import version

from fastapi import APIRouter, HTTPException, Request

from app.examples import EXAMPLES
from app.execution import execute
from app.operations import Operation
from app.schemas import ComponentRequest, ExampleRequest

router = APIRouter(prefix="/v1", tags=["Metadata"])


@router.get("/capabilities")
async def capabilities(request: Request):
    settings = request.app.state.settings
    return {
        "schema_version": 1,
        "pypsa_version": version("pypsa"),
        "execution": "request-scoped",
        "concurrency_per_instance": 1,
        "solver": "highs",
        "request_timeout_seconds": settings.request_timeout,
        "operations": [
            operation
            for operation in Operation
            if operation not in {Operation.COMPONENT, Operation.EXAMPLE}
        ],
        "limits": {
            "components": settings.max_components,
            "snapshots": settings.max_snapshots,
            "cells": settings.max_cells,
        },
    }


@router.get("/components/{component_type}")
async def component(component_type: str, request: Request):
    return await execute(
        request, Operation.COMPONENT, ComponentRequest(component=component_type)
    )


@router.get("/examples")
async def examples():
    return {
        "examples": [
            {"id": name, "description": example["description"]}
            for name, example in EXAMPLES.items()
        ]
    }


@router.get("/examples/{name}")
async def load_example(name: str, request: Request):
    if name not in EXAMPLES:
        raise HTTPException(404, "Unknown example.")
    return await execute(request, Operation.EXAMPLE, ExampleRequest(name=name))
