# SPDX-FileCopyrightText: PyPSA Contributors
# SPDX-License-Identifier: MIT
"""Explicit PyPSA solver handlers, independent of HTTP and process management."""

import pypsa
from app import network
from app.operations import ExecutionContext
from app.schemas import NetworkRequest, OptimizeRequest, PowerFlowRequest


def _load_validated(
    body: NetworkRequest, context: ExecutionContext
) -> tuple[pypsa.Network, list[dict]]:
    n = network.load(body.source, context.root, context.limits)
    validation = network.validate(n)
    if not validation["valid"]:
        raise ValueError(
            "Network is inconsistent. Call /v1/networks/validate for diagnostics."
        )
    return n, validation["diagnostics"]


def optimize_network(body: OptimizeRequest, context: ExecutionContext) -> dict:
    n, diagnostics = _load_validated(body, context)
    status, termination = n.optimize(
        solver_name="highs",
        solver_options={
            "threads": 1,
            "time_limit": min(body.time_limit, context.limits["request_timeout"] * 0.8),
            "mip_rel_gap": body.mip_gap,
        },
        include_objective_constant=False,
        log_to_console=False,
    )
    solved = status == "ok" and termination == "optimal"
    return {
        "status": status,
        "termination_condition": termination,
        "solution_available": solved,
        "diagnostics": diagnostics,
        "objective": network.finite(n.objective) if solved else None,
        "artifact": network.artifact(n, context.root, context.limits)
        if solved
        else None,
        "statistics": network.statistics(n, body.statistics) if solved else [],
    }


def power_flow(body: PowerFlowRequest, context: ExecutionContext) -> dict:
    n, diagnostics = _load_validated(body, context)
    if body.mode == "linear":
        n.lpf()
        converged, convergence = True, None
    else:
        output = n.pf(x_tol=body.x_tol)
        converged = bool(output["converged"].to_numpy().all())
        convergence = {key: network.table(value) for key, value in output.items()}
    return {
        "status": "ok" if converged else "non_converged",
        "solution_available": converged,
        "convergence": convergence,
        "diagnostics": diagnostics,
        "artifact": network.artifact(n, context.root, context.limits)
        if converged
        else None,
    }
