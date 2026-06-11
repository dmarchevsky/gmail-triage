"""FastAPI application entrypoint."""

import asyncio
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from app.api import (
    auth_routes,
    category_routes,
    digest_routes,
    email_routes,
    feedback_routes,
    gmail_routes,
    llm_routes,
    poller_routes,
    rule_routes,
    settings_routes,
    status,
)
from app.auth import AuthMiddleware
from app.config import get_config
from app.db import get_sessionmaker, run_migrations
from app.logging_setup import get_logger, setup_logging
from app.models import GmailAuth
from app.services import classifier, digest_scheduler, llm, settings_service
from app.services.gmail import assert_scopes_safe
from app.services.poller import poller_loop, set_classify_hook

log = get_logger(__name__)


def assert_stored_scopes_safe() -> None:
    """Startup guard (§6.1): refuse to run with a send-capable stored token."""
    session = get_sessionmaker()()
    try:
        row = session.scalar(select(GmailAuth).limit(1))
        if row is not None:
            assert_scopes_safe(row.granted_scopes)
    finally:
        session.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = get_config()
    setup_logging(cfg.log_level)
    cfg.validate_secrets()
    run_migrations()
    assert_stored_scopes_safe()
    set_classify_hook(classifier.classify_pending)
    session = get_sessionmaker()()
    try:
        await llm.health_probe(settings_service.get_all_settings(session, redact=False),
                               timeout=5)
    finally:
        session.close()
    poller_task = asyncio.create_task(poller_loop())
    digest_scheduler.start()
    log.info("startup_complete", db=cfg.sqlalchemy_url)
    yield
    digest_scheduler.shutdown()
    poller_task.cancel()
    with suppress(asyncio.CancelledError):
        await poller_task
    log.info("shutdown")


def create_app() -> FastAPI:
    app = FastAPI(title="MailTriage", version="0.1.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url="/api/v1/openapi.json")
    app.add_middleware(AuthMiddleware)

    api_prefix = "/api/v1"
    app.include_router(status.router, prefix=api_prefix)
    app.include_router(auth_routes.router, prefix=api_prefix)
    app.include_router(settings_routes.router, prefix=api_prefix)
    app.include_router(gmail_routes.router, prefix=api_prefix)
    app.include_router(poller_routes.router, prefix=api_prefix)
    app.include_router(category_routes.router, prefix=api_prefix)
    app.include_router(llm_routes.router, prefix=api_prefix)
    app.include_router(rule_routes.router, prefix=api_prefix)
    app.include_router(email_routes.router, prefix=api_prefix)
    app.include_router(feedback_routes.router, prefix=api_prefix)
    app.include_router(digest_routes.router, prefix=api_prefix)

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
