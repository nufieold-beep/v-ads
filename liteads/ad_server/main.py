"""
LiteAds Ad Server – CPM CTV & In-App Video Only.

Main entry point for the ad serving API with OpenRTB 2.6,
VAST 2.x–4.x, and nurl/burl support.
"""

import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from liteads.ad_server.middleware.metrics import MetricsMiddleware, metrics_endpoint
from liteads.ad_server.routers import ad, event, health, openrtb, vast_tag
from liteads.ad_server.routers import admin as admin_router
from liteads.ad_server.routers import analytics as analytics_router
from liteads.ad_server.routers import demand as demand_router
from liteads.ad_server.routers import supply_demand as supply_demand_router
from liteads.common.cache import redis_client
from liteads.common.config import get_settings
from liteads.common.database import close_db, create_tables, init_db
from liteads.common.exceptions import LiteAdsError
from liteads.common.logger import clear_log_context, get_logger, log_context
from liteads.common.utils import generate_request_id
from liteads.schemas.response import ErrorResponse

logger = get_logger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    settings = get_settings()

    # Startup
    logger.info(
        "Starting LiteAds server",
        version=settings.app_version,
        env=settings.env,
    )

    # Initialize database
    await init_db()
    # Always ensure tables exist (CREATE TABLE IF NOT EXISTS — safe for production)
    await create_tables()

    # Validate DB connectivity
    from liteads.common.database import db
    healthy = await db.health_check()
    if not healthy:
        logger.error("Database health check FAILED on startup — endpoints will return 500")
    else:
        logger.info("Database health check passed")

    # Initialize Redis
    await redis_client.connect()

    logger.info("LiteAds server started successfully")

    yield

    # Shutdown
    logger.info("Shutting down LiteAds server")
    await redis_client.close()
    await close_db()
    logger.info("LiteAds server stopped")


def create_app() -> FastAPI:
    """Create and configure FastAPI application."""
    settings = get_settings()

    app = FastAPI(
        title="LiteAds",
        description="CPM CTV & In-App Video Ad Server – OpenRTB 2.6 / VAST 2.x–4.x",
        version=settings.app_version,
        docs_url="/docs" if settings.debug else None,
        redoc_url="/redoc" if settings.debug else None,
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

    # Prometheus metrics middleware
    app.add_middleware(MetricsMiddleware)

    # Prometheus metrics endpoint
    app.add_api_route("/metrics", metrics_endpoint, methods=["GET"], tags=["monitoring"])

    # Request logging middleware
    @app.middleware("http")
    async def logging_middleware(request: Request, call_next: Any) -> Any:
        """Log all requests with timing."""
        request_id = generate_request_id()
        log_context(request_id=request_id)

        start_time = time.perf_counter()

        response = await call_next(request)

        duration_ms = (time.perf_counter() - start_time) * 1000

        logger.info(
            "Request completed",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=round(duration_ms, 2),
        )

        # Add headers
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Response-Time"] = f"{duration_ms:.2f}ms"

        clear_log_context()

        return response

    # Exception handlers
    @app.exception_handler(LiteAdsError)
    async def liteads_error_handler(
        request: Request,
        exc: LiteAdsError,
    ) -> JSONResponse:
        """Handle LiteAds errors."""
        logger.warning(
            "LiteAds error",
            error=exc.__class__.__name__,
            message=exc.message,
            details=exc.details,
        )

        return JSONResponse(
            status_code=400,
            content=ErrorResponse(
                error=exc.__class__.__name__,
                message=exc.message,
                details=exc.details,
            ).model_dump(),
        )

    @app.exception_handler(Exception)
    async def general_error_handler(
        request: Request,
        exc: Exception,
    ) -> JSONResponse:
        """Handle unexpected errors."""
        import traceback

        tb = traceback.format_exception(type(exc), exc, exc.__traceback__)
        logger.error(
            "Unexpected error",
            error=str(exc),
            error_type=exc.__class__.__name__,
            path=request.url.path,
            traceback="".join(tb),
        )

        # In debug mode, return the actual error details for easier debugging
        detail: dict[str, Any] = {
            "error": "InternalServerError",
            "message": "An unexpected error occurred",
        }
        if settings.debug:
            detail["message"] = str(exc)
            detail["error_type"] = exc.__class__.__name__
            detail["traceback"] = tb

        return JSONResponse(
            status_code=500,
            content=detail,
        )

    # Include routers
    app.include_router(health.router, tags=["health"])
    app.include_router(ad.router, prefix="/api/v1/ad", tags=["ad"])
    app.include_router(event.router, prefix="/api/v1/event", tags=["event"])
    app.include_router(openrtb.router, prefix="/api/v1/openrtb", tags=["openrtb"])
    app.include_router(vast_tag.router, prefix="/api/vast", tags=["vast-tag"])
    app.include_router(admin_router.router, prefix="/api/v1/admin", tags=["admin"])
    app.include_router(analytics_router.router, prefix="/api/v1/analytics", tags=["analytics"])
    app.include_router(demand_router.router, prefix="/api/v1/demand", tags=["demand"])
    app.include_router(supply_demand_router.router, prefix="/api/v1/supply-demand", tags=["supply-demand"])

    # ── Admin Dashboard UI ─────────────────────────────────────
    _static_dir = Path(__file__).resolve().parent / "static"

    @app.get("/dashboard", response_class=HTMLResponse, tags=["dashboard"])
    async def dashboard_ui():
        """Serve the admin dashboard single-page application."""
        html_path = _static_dir / "dashboard.html"
        return HTMLResponse(content=html_path.read_text(encoding="utf-8"))

    # Legacy/direct path redirect – handles requests to the filesystem-style URL
    @app.get("/liteads/ad_server/static/dashboard.html", response_class=RedirectResponse, tags=["dashboard"])
    async def dashboard_legacy_redirect():
        """Redirect legacy filesystem-style dashboard URL to /dashboard."""
        return RedirectResponse(url="/dashboard")

    # Serve any additional static assets (JS, CSS, images) if needed
    if _static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

    return app


# Create app instance
app = create_app()


def main() -> None:
    """Run the server using uvicorn."""
    import uvicorn

    settings = get_settings()

    uvicorn.run(
        "liteads.ad_server.main:app",
        host=settings.server.host,
        port=settings.server.port,
        workers=settings.server.workers,
        reload=settings.server.reload,
        log_level="info" if not settings.debug else "debug",
    )


if __name__ == "__main__":
    main()