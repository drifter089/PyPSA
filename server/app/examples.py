# SPDX-FileCopyrightText: PyPSA Contributors
# SPDX-License-Identifier: MIT
"""Trusted, offline example catalogue shared by routes and computation children."""

EXAMPLES = {
    "two-bus": {
        "description": "Two-bus constrained economic dispatch",
        "path": None,
    },
    "ac-dc-meshed": {
        "description": "Bundled upstream AC/DC meshed network",
        "path": "examples/networks/ac-dc-meshed/ac-dc-meshed.nc",
    },
    "storage-hvdc": {
        "description": "Bundled upstream storage and HVDC network",
        "path": "examples/networks/storage-hvdc/storage-hvdc.nc",
    },
    "model-energy": {
        "description": "Bundled upstream model.energy capacity-expansion network",
        "path": "examples/networks/model-energy/model-energy.nc",
    },
}
