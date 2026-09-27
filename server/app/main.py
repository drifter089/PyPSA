# SPDX-FileCopyrightText: PyPSA Contributors
# SPDX-License-Identifier: MIT
"""Create the application, attach middleware/authentication, and register routes."""

import asyncio
import secrets
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import Settings
from app.middleware import BodyLimit
from app.routes import analyses, metadata, networks

bearer = HTTPBearer(auto_error=False)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.slot = asyncio.Semaphore(1)
        yield

    app = FastAPI(
        title="PyPSA API",
        version="0.1.0",
        lifespan=lifespan,
        description="Local-source PyPSA. Each computational request finishes before its response; no persistent jobs or database.",
    )
    app.state.settings = settings
    app.add_middleware(BodyLimit, maximum=settings.max_body_bytes)

    async def authorize(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> None:
        if settings.api_key and (
            credentials is None
            or not secrets.compare_digest(credentials.credentials, settings.api_key)
        ):
            raise HTTPException(
                401,
                "Invalid service credential.",
                headers={"WWW-Authenticate": "Bearer"},
            )

    @app.get("/healthz")
    async def health():
        return {"status": "ok"}

    for router in (metadata.router, networks.router, analyses.router):
        app.include_router(router, dependencies=[Depends(authorize)])

    return app


app = create_app()
