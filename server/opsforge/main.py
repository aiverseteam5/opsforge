"""FastAPI app factory. Mounts API routers + (later) the SPA. Entrypoint: `api`."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from .api import (
    actions,
    audit,
    catalog,
    connectors,
    graph,
    knowledge,
    llm_providers,
    runs,
    schedules,
    skills,
    webhooks,
)
from .db import engine
from .security import redact
from .skills import install_builtin_skills
from .surfaces import slack as slack_surface

logger = logging.getLogger("opsforge.main")

_SPA_DIR = "workbench/dist"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Install/refresh built-in skill packs at startup (idempotent).
    try:
        installed = await install_builtin_skills()
        logger.info("installed %d built-in skill(s)", len(installed))
    except Exception:  # noqa: BLE001 - never block startup on skill install
        logger.warning("built-in skill install skipped", exc_info=True)
    yield
    await engine().dispose()


def create_app() -> FastAPI:
    app = FastAPI(title="OpsForge", version="0.1.0", lifespan=lifespan)

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(_request: Request, exc: RequestValidationError):
        # FastAPI's default 422 echoes pydantic's `input` (the rejected value) — which on a
        # credential-bearing body (POST/PATCH /connectors) is the plaintext secret, or the
        # WHOLE request body. Strip `input`/`ctx` so a submitted credential can never leak
        # into an error response (or the DOM that renders it). loc+msg+type suffice for a
        # client to act on. redact() is belt-and-suspenders over the remaining fields.
        safe = []
        for err in exc.errors():
            err = dict(err)
            err.pop("input", None)
            err.pop("ctx", None)
            safe.append(err)
        return JSONResponse(status_code=422, content={"detail": redact(safe)})

    @app.get("/healthz", tags=["meta"])
    async def healthz() -> dict[str, str]:
        # Unauthenticated; does a real DB round-trip so Compose healthchecks and
        # load balancers fail closed if Postgres is unreachable.
        async with engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
        return {"status": "ok"}

    for module in (
        runs, connectors, skills, schedules, actions, webhooks, graph, audit, knowledge,
        llm_providers, catalog,
    ):
        app.include_router(module.router)
    app.include_router(slack_surface.router)

    # Serve the built SPA (absent until M4 — guarded so earlier milestones boot).
    # Hashed bundles live under /assets; every other non-API path returns
    # index.html so client-side routes (e.g. /runs/<id>) deep-link correctly.
    if os.path.isdir(_SPA_DIR):
        app.mount(
            "/assets",
            StaticFiles(directory=os.path.join(_SPA_DIR, "assets")),
            name="assets",
        )
        index_html = os.path.join(_SPA_DIR, "index.html")

        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa(full_path: str):
            if full_path.startswith("api/"):
                raise HTTPException(status_code=404, detail="not found")
            return FileResponse(index_html)

    return app


app = create_app()
