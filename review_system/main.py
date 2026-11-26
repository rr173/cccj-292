"""FastAPI application factory and entry point.

Run with:
    uvicorn review_system.main:app --reload
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
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

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        # Keep the same {"error": ...} envelope as domain errors while
        # preserving the structured pydantic details.
        first = exc.errors()[0] if exc.errors() else {}
        location = ".".join(str(p) for p in first.get("loc", []) if p != "body")
        message = first.get("msg", "invalid request")
        detail = f"{location}: {message}" if location else message
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "invalid_request",
                    "detail": detail,
                    "errors": jsonable_encoder(exc.errors()),
                }
            },
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
