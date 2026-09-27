# SPDX-FileCopyrightText: PyPSA Contributors
# SPDX-License-Identifier: MIT
"""Await one isolated child process per request, with timeout and disconnect cleanup."""

import asyncio
import json
import os
import signal
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

from fastapi import HTTPException, Request
from pydantic import BaseModel

from app.config import Settings
from app.operations import Operation


async def _wait_for_disconnect(request: Request) -> None:
    while not await request.is_disconnected():
        await asyncio.sleep(0.2)


async def _stop(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    await process.wait()


async def execute(request: Request, operation: Operation, payload: BaseModel) -> dict:
    settings: Settings = request.app.state.settings
    slot: asyncio.Semaphore = request.app.state.slot
    if slot.locked():
        raise HTTPException(
            429,
            "This instance is busy; retry or allow the host to scale out.",
            headers={"Retry-After": "1"},
        )
    async with slot:
        # Each child imports the current source afresh. Nothing survives the request.
        with tempfile.TemporaryDirectory(prefix="pypsa-request-") as directory:
            root = Path(directory)
            limits = asdict(settings)
            limits.pop("api_key")
            job = {
                "operation": operation.value,
                "payload": payload.model_dump(mode="json"),
                "limits": limits,
            }
            input_path = root / "request.json"
            output_path = root / "response.json"
            input_path.write_text(json.dumps(job), encoding="utf-8")
            env = {
                **os.environ,
                "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
                "OMP_NUM_THREADS": "1",
                "OPENBLAS_NUM_THREADS": "1",
                "MKL_NUM_THREADS": "1",
                "MPLBACKEND": "Agg",
            }
            # The computation does not need the API credential.
            env.pop("SERVER_API_KEY", None)
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "app.worker",
                str(input_path),
                str(output_path),
                cwd=root,
                env=env,
                start_new_session=True,
                stdout=asyncio.subprocess.DEVNULL,
                # Diagnostic logs go to container stderr, not an unread PIPE.
                stderr=None,
            )
            finished = asyncio.create_task(process.wait())
            disconnected = asyncio.create_task(_wait_for_disconnect(request))
            try:
                done, _ = await asyncio.wait(
                    {finished, disconnected},
                    timeout=settings.request_timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    raise HTTPException(
                        504, "Execution exceeded the service request timeout."
                    )
                if disconnected in done:
                    raise HTTPException(
                        499, "Client disconnected; computation terminated."
                    )
                if process.returncode != 0 or not output_path.exists():
                    raise HTTPException(
                        500, "Computation process failed; inspect service logs."
                    )
                if output_path.stat().st_size > settings.max_result_bytes:
                    raise HTTPException(
                        413,
                        "Result exceeds MAX_RESULT_BYTES; use a smaller network or query.",
                    )
                result = json.loads(output_path.read_text(encoding="utf-8"))
                if not result["ok"]:
                    raise HTTPException(result["status"], result["error"])
                return result["data"]
            finally:
                disconnected.cancel()
                await _stop(process)
                await asyncio.gather(finished, disconnected, return_exceptions=True)
