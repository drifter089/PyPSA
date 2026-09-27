# SPDX-FileCopyrightText: PyPSA Contributors
# SPDX-License-Identifier: MIT
"""Child-process entry point: validate, call the registered handler, and serialise."""

import json
import logging
import sys
from pathlib import Path

import pypsa
from app.operations import ExecutionContext, Operation
from app.operations.registry import OPERATIONS


def main() -> None:
    input_path, output_path = map(Path, sys.argv[1:])
    job = json.loads(input_path.read_text(encoding="utf-8"))
    try:
        pypsa.options.general.allow_network_requests = False
        context = ExecutionContext(input_path.parent, job["limits"])
        spec = OPERATIONS[Operation(job["operation"])]
        result = spec.run(job["payload"], context)
        result = {"schema_version": 1, "pypsa_version": pypsa.__version__, **result}
        response = {"ok": True, "data": result}
    except (ValueError, KeyError, TypeError, NotImplementedError) as exc:
        response = {"ok": False, "status": 422, "error": str(exc)}
    except Exception:
        logging.exception("PyPSA request failed")
        response = {
            "ok": False,
            "status": 500,
            "error": "PyPSA operation failed; inspect service logs.",
        }
    output_path.write_text(json.dumps(response, allow_nan=False), encoding="utf-8")


if __name__ == "__main__":
    main()
