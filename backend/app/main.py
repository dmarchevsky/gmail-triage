"""FastAPI application entrypoint."""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import auth_routes, settings_routes, status
from app.auth import AuthMiddleware
from app.config import get_config
from app.db import run_migrations
from app.logging_setup import get_logger, setup_logging

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = get_config()
    setup_logging(cfg.log_level)
    cfg.validate_secrets()
    run_migrations()
    log.info("startup_complete", db=cfg.sqlalchemy_url)
    yield
    log.info("shutdown")


def create_app() -> FastAPI:
    app = FastAPI(title="MailTriage", version="0.1.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url="/api/v1/openapi.json")
    app.add_middleware(AuthMiddleware)

    api_prefix = "/api/v1"
    app.include_router(status.router, prefix=api_prefix)
    app.include_router(auth_routes.router, prefix=api_prefix)
    app.include_router(settings_routes.router, prefix=api_prefix)

    static_dir: Path = get_config().static_dir
    if static_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=static_dir / "assets"), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        def spa(full_path: str):
            candidate = static_dir / full_path
            if full_path and candidate.is_file() and candidate.resolve().is_relative_to(
                    static_dir.resolve()):
                return FileResponse(candidate)
            return FileResponse(static_dir / "index.html")

    return app


app = create_app()
