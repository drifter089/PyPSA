# SPDX-FileCopyrightText: PyPSA Contributors
# SPDX-License-Identifier: MIT
"""Editor orchestration in the existing isolated computation worker."""

import pypsa

from app import network
from app.editor import catalog, edits, validation
from app.editor.schemas import CatalogRequest, Diagnostic, EditRequest, EditResponse
from app.operations import ExecutionContext
from app.schemas import NetCDFSource


def load(body, context):
    return (
        network.load(body.source, context.root, context.limits)
        if body.source
        else pypsa.Network()
    )


def describe_editor(body: CatalogRequest, context: ExecutionContext):
    n = load(body, context)
    return catalog.describe(
        n, body, catalog.source_id(body.source), context.limits
    ).model_dump(mode="json")


def process_edits(body: EditRequest, context: ExecutionContext, commit: bool):
    original = load(body, context)
    identity = catalog.source_id(body.source)
    if body.expected_source_id is not None and body.expected_source_id != identity:
        raise ValueError("Source identity mismatch; reload the current revision.")
    n = original.copy()
    diagnostics, extensions = edits.apply(
        n, body.operations, body.analysis, context.limits
    )
    if diagnostics:
        # Failed operation batches never expose a partly mutated network.
        n = original
        extensions = []
    else:
        if body.operations:
            edits.invalidate_solution(n)
        diagnostics.extend(validation.validate(n, body.analysis))
    valid = not any(d.severity == "error" for d in diagnostics)
    artifact = None
    if commit and valid:
        edits.invalidate_solution(n)
        artifact = network.artifact(n, context.root, context.limits)
        # Native export omits all-default columns (including unused extra ports).
        # Describe the persisted representation, not a divergent pre-export view.
        n = network.load(
            NetCDFSource.model_validate(artifact), context.root, context.limits
        )
    if valid:
        diagnostics.append(
            Diagnostic(
                severity="info",
                code="not-solved",
                message="Input checks passed; feasibility/convergence has not been evaluated.",
            )
        )
    result = catalog.describe(
        n,
        body,
        artifact["sha256"] if artifact else identity,
        context.limits,
        diagnostics,
    )
    if commit and valid:
        result.context["solution_state"] = "invalidated"
    return EditResponse(
        pypsa_version=pypsa.__version__,
        valid=valid,
        applied=commit and valid,
        base_source_id=identity,
        artifact=artifact,
        catalog=result,
        diagnostics=diagnostics,
        extensions=extensions if valid else [],
    ).model_dump(mode="json")


def evaluate_editor(body: EditRequest, context: ExecutionContext):
    return process_edits(body, context, commit=False)


def edit_network(body: EditRequest, context: ExecutionContext):
    return process_edits(body, context, commit=True)
