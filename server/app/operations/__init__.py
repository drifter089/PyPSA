# SPDX-FileCopyrightText: PyPSA Contributors
# SPDX-License-Identifier: MIT
"""Lightweight operation identifiers and execution context shared across processes.

Keep PyPSA and the handler registry out of this module: HTTP routes import it.
"""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class Operation(StrEnum):
    COMPONENT = "component"
    EXAMPLE = "example"
    INSPECT = "inspect"
    EXPORT = "export"
    VALIDATE = "validate"
    OPTIMIZE = "optimize"
    POWER_FLOW = "power_flow"
    STATISTICS = "statistics"
    EDITOR_CATALOG = "editor_catalog"
    EDITOR_EVALUATE = "editor_evaluate"
    EDIT_NETWORK = "edit_network"


@dataclass(frozen=True)
class ExecutionContext:
    root: Path
    limits: dict
