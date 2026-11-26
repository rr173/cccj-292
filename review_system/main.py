"""FastAPI application factory and entry point.

Run with:
    uvicorn review_system.main:app --reload
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import config, db as database
from .errors import ApiError
from .routers import authors, editors, reviewers


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Prepare schema and the locally seeded author/reviewer/editor accounts.
    database.init_db(config.DB_PATH)
    yield


def create_app(db_path: str | None = None) -> FastAPI:
    if db_path is not None:
        config.DB_PATH = db_path
    # Ensure the schema and the locally seeded accounts exist even when the
    # app is created without going through the ASGI lifespan (e.g. tests).
    database.init_db(config.DB_PATH)

    app = FastAPI(
        title="Anonymous Peer Review Backend",
        version="1.0.0",
        description="Editor-driven anonymous review workflow over SQLite.",
        lifespan=lifespan,
    )

    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError):
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "detail": exc.detail}},
        )

    app.include_router(authors.router)
    app.include_router(reviewers.router)
    app.include_router(editors.router)

    @app.get("/health", tags=["meta"])
    def health():
        return {"status": "ok"}

    return app


# Default application used by `uvicorn review_system.main:app`.
app = create_app()
