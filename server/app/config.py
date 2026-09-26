# SPDX-FileCopyrightText: PyPSA Contributors
# SPDX-License-Identifier: MIT
"""Runtime limits; no database or application-specific configuration."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    api_key: str = ""
    request_timeout: int = 240
    max_body_bytes: int = 12 * 1024 * 1024
    max_result_bytes: int = 24 * 1024 * 1024
    max_components: int = 2000
    max_snapshots: int = 8760
    max_cells: int = 2_000_000

    @classmethod
    def from_env(cls) -> "Settings":
        values = {
            "request_timeout": int(os.getenv("REQUEST_TIMEOUT_SECONDS", "240")),
            "max_body_bytes": int(os.getenv("MAX_BODY_BYTES", str(12 * 1024 * 1024))),
            "max_result_bytes": int(
                os.getenv("MAX_RESULT_BYTES", str(24 * 1024 * 1024))
            ),
            "max_components": int(os.getenv("MAX_COMPONENTS", "2000")),
            "max_snapshots": int(os.getenv("MAX_SNAPSHOTS", "8760")),
            "max_cells": int(os.getenv("MAX_CELLS", "2000000")),
        }
        if any(value <= 0 for value in values.values()):
            raise ValueError("All service limits must be positive.")
        return cls(api_key=os.getenv("SERVER_API_KEY", ""), **values)
