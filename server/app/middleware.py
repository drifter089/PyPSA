# SPDX-FileCopyrightText: PyPSA Contributors
# SPDX-License-Identifier: MIT
"""HTTP body limits, independent of PyPSA operations."""

from fastapi.responses import JSONResponse


class BodyLimit:
    """Bound streamed bodies before FastAPI/Pydantic allocates the decoded model."""

    def __init__(self, app, maximum: int) -> None:
        self.app, self.maximum = app, maximum

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        messages, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            size += len(message.get("body", b""))
            if size > self.maximum:
                response = JSONResponse(
                    {"detail": "Request exceeds MAX_BODY_BYTES."}, status_code=413
                )
                await response(scope, receive, send)
                return
            messages.append(message)
            if not message.get("more_body", False):
                break
        iterator = iter(messages)

        async def replay():
            message = next(iterator, None)
            return message if message is not None else await receive()

        await self.app(scope, replay, send)
