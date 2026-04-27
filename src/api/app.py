"""
FastAPI Application Factory for genworker.

Provides:
- Lifespan management (startup/shutdown) via BootstrapOrchestrator
- CORS middleware
- Health check route
- Route registration
"""
import asyncio
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.common.logger import get_logger
from src.common.settings import get_settings
from src.api.static_frontend import mount_frontend
from src.runtime.app_state import (
    MissingEngineDispatcher,
    configure_persona_reload_watcher,
    store_dependencies,
    stop_persona_reload_watcher,
)
from src.runtime.runtime_profile import runtime_profile_warnings
from src.runtime.runtime_summary import build_runtime_summary

logger = get_logger()

_orchestrator: Optional["BootstrapOrchestrator"] = None

_MissingEngineDispatcher = MissingEngineDispatcher
_store_dependencies = store_dependencies


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """FastAPI lifespan context manager using BootstrapOrchestrator."""
    global _orchestrator

    from src.bootstrap import create_orchestrator
    from src.bootstrap.llm_preflight import preflight_litellm_config_provider

    logger.info("=" * 60)
    logger.info("Starting genworker server")
    logger.info("=" * 60)

    settings = get_settings()
    preflight_litellm_config_provider(settings)

    try:
        _orchestrator = create_orchestrator()
        context = await _orchestrator.startup()

        if context.has_errors():
            for name, err in context.errors.items():
                logger.warning(f"Initializer '{name}' had an error: {err}")

        settings = context.settings
        # Store dependencies in app.state for route access
        store_dependencies(app, context)
        logger.info(build_runtime_summary(app.state))
        for warning in runtime_profile_warnings(settings):
            logger.warning(warning)
        configure_persona_reload_watcher(app, settings)

        logger.info("=" * 60)
        logger.info("genworker server READY")
        logger.info("=" * 60)

    except Exception as e:
        logger.error(f"Startup failed: {e}")
        raise

    yield

    logger.info("=" * 60)
    logger.info("Shutting down genworker server...")
    logger.info("=" * 60)

    try:
        await stop_persona_reload_watcher(app)
        if _orchestrator:
            await _orchestrator.shutdown()
    except asyncio.CancelledError:
        logger.info("Shutdown cancelled (expected during force exit)")
    except Exception as e:
        logger.error(f"Shutdown error: {e}")

    logger.info("genworker server stopped")


def create_app() -> FastAPI:
    """
    Create and configure the FastAPI application.

    Returns:
        Configured FastAPI application instance.
    """
    settings = get_settings()

    app = FastAPI(
        title=settings.service_name,
        version=settings.service_version,
        lifespan=lifespan,
    )

    # CORS middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Register routes
    from src.api.routes.health_routes import router as health_router
    from src.api.routes.runtime_routes import router as runtime_router
    from src.api.routes.worker_routes import router as worker_router
    from src.api.routes.chat_routes import router as chat_router
    from src.api.routes.webhook_routes import router as webhook_router
    from src.api.routes.channel_routes import router as channel_router
    if bool(getattr(settings, "im_channel_enabled", False)):
        from src.api.routes.im_config_routes import router as im_config_router
    else:
        im_config_router = None

    app.include_router(health_router)
    app.include_router(runtime_router)
    app.include_router(worker_router)
    if im_config_router is not None:
        app.include_router(im_config_router)
    app.include_router(chat_router)
    app.include_router(webhook_router)
    app.include_router(channel_router)
    mount_frontend(app)

    return app


# Application instance for uvicorn
app = create_app()
