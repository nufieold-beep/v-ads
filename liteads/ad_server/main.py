"""
LiteAds Ad Server – CPM CTV & In-App Video Only.

Main entry point for the ad serving API with OpenRTB 2.6,
VAST 2.x–4.x, and nurl/burl support.
"""

import hashlib
import hmac
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Cookie, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from urllib.parse import parse_qs, urlencode

from liteads.ad_server.middleware.metrics import MetricsMiddleware, metrics_endpoint
from liteads.ad_server.routers import ad, event, health, openrtb, vast_tag
from liteads.ad_server.routers import admin as admin_router
from liteads.ad_server.routers import analytics as analytics_router
from liteads.ad_server.routers import auth as auth_router
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
    # Close demand forwarder HTTP client
    from liteads.ad_server.services.demand_forwarder import close_http_client
    await close_http_client()
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

    # ── VAST query-param cleanup middleware ──────────────────────
    # Publishers sometimes send broken query strings:
    #   • Empty values       → dnt=  (breaks int parsing → 422)
    #   • Unresolved macros  → ip={uip}  (string in int slot → 422)
    # This middleware strips those before FastAPI validates params.
    @app.middleware("http")
    async def clean_vast_query_params(request: Request, call_next: Any) -> Any:
        if request.url.path.startswith("/api/vast"):
            raw_qs = request.scope.get("query_string", b"").decode("latin-1")
            # Fast pre-check: only parse/rebuild when dirty patterns detected
            if raw_qs and (
                "={" in raw_qs or "=%7B" in raw_qs.upper()
                or "=&" in raw_qs or raw_qs.endswith("=")
            ):
                pairs = parse_qs(raw_qs, keep_blank_values=True)
                cleaned: dict[str, list[str]] = {}
                for key, vals in pairs.items():
                    good = [
                        v for v in vals
                        if v  # drop empty
                        and "{" not in v and "}" not in v  # drop macros
                        and "%7B" not in v.upper()         # encoded macros
                    ]
                    if good:
                        cleaned[key] = good
                request.scope["query_string"] = urlencode(
                    cleaned, doseq=True,
                ).encode("latin-1")
        return await call_next(request)

    # Prometheus metrics endpoint
    app.add_api_route("/metrics", metrics_endpoint, methods=["GET"], tags=["monitoring"])

    # Request logging middleware
    @app.middleware("http")
    async def logging_middleware(request: Request, call_next: Any) -> Any:
        """Log all requests with timing (skip hot-path to reduce overhead)."""
        path = request.url.path

        # Skip logging overhead for high-frequency hot paths and health checks.
        # VAST endpoint has its own request_id and logging; health/metrics are noise.
        if path.startswith(("/api/vast", "/health", "/ping", "/live", "/ready", "/metrics")):
            return await call_next(request)

        request_id = generate_request_id()
        log_context(request_id=request_id)

        start_time = time.perf_counter()

        response = await call_next(request)

        duration_ms = (time.perf_counter() - start_time) * 1000

        logger.info(
            "Request completed",
            method=request.method,
            path=path,
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

    # ── Authentication (Adtelligent-style Bearer token) ──────────
    # POST /api/v1/token  – same path convention as Adtelligent /v1/token
    app.include_router(auth_router.router, prefix="/api/v1/token", tags=["auth"])

    # ── Adtelligent-compatible route aliases ────────────────────
    # Adtelligent SSP uses /v1/channels for supply inventory and
    # /v1/sources for demand endpoints.  These aliases keep full
    # API compatibility with Adtelligent-style integrations while
    # the canonical paths remain at /api/v1/supply-demand/*.
    app.include_router(
        supply_demand_router.router,
        prefix="/api/v1/channels",
        tags=["channels"],
        include_in_schema=False,   # hide duplicates from OpenAPI docs
    )
    app.include_router(
        supply_demand_router.router,
        prefix="/api/v1/sources",
        tags=["sources"],
        include_in_schema=False,
    )

    # ── Admin Dashboard UI (with login auth) ───────────────────
    _static_dir = Path(__file__).resolve().parent / "static"

    # Dashboard credentials (configurable via LITEADS_DASHBOARD__* env vars)
    _DASH_USER = settings.dashboard.username
    _DASH_PASS = settings.dashboard.password
    _DASH_SECRET = settings.dashboard.secret_key
    _COOKIE_NAME = "liteads_session"
    _COOKIE_MAX_AGE = settings.dashboard.session_max_age

    def _make_session_token(username: str) -> str:
        """Create an HMAC-signed session token."""
        payload = f"{username}:liteads-dash"
        sig = hmac.new(_DASH_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
        return f"{username}:{sig}"

    def _verify_session(token: str | None) -> bool:
        """Verify the session cookie token."""
        if not token:
            return False
        parts = token.split(":", 1)
        if len(parts) != 2:
            return False
        expected = _make_session_token(parts[0])
        return hmac.compare_digest(token, expected)

    # ── Login page HTML ──────────────────────────────────────
    _LOGIN_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>LiteAds – Login</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#0f1117;--bg2:#181b24;--bg3:#1e222d;--bg4:#272c3a;--fg:#e4e6ed;--fg2:#9ba1b0;--fg3:#6b7280;--accent:#6366f1;--accent2:#818cf8;--red:#ef4444;--radius:8px;--radius-lg:12px}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:var(--bg);color:var(--fg);display:flex;align-items:center;justify-content:center;min-height:100vh}
.login-card{background:var(--bg2);border:1px solid var(--bg4);border-radius:var(--radius-lg);padding:40px;width:100%;max-width:400px;box-shadow:0 10px 25px rgba(0,0,0,.4)}
.login-card h1{text-align:center;font-size:22px;margin-bottom:6px}
.login-card .subtitle{text-align:center;color:var(--fg3);font-size:13px;margin-bottom:28px}
.login-card .logo{text-align:center;margin-bottom:20px;font-size:36px}
.form-group{margin-bottom:18px}
.form-group label{display:block;font-size:13px;font-weight:600;color:var(--fg2);margin-bottom:6px}
.form-group input{width:100%;padding:10px 14px;background:var(--bg3);border:1px solid var(--bg4);border-radius:var(--radius);color:var(--fg);font-size:14px;outline:none;transition:border-color .2s}
.form-group input:focus{border-color:var(--accent)}
.btn-login{width:100%;padding:12px;background:var(--accent);color:#fff;border:none;border-radius:var(--radius);font-size:15px;font-weight:600;cursor:pointer;transition:background .2s}
.btn-login:hover{background:var(--accent2)}
.error-msg{background:rgba(239,68,68,.12);color:var(--red);padding:10px 14px;border-radius:var(--radius);font-size:13px;margin-bottom:16px;display:none}
.error-msg.show{display:block}
.brand{display:flex;align-items:center;justify-content:center;gap:8px;margin-bottom:24px}
.brand svg{width:32px;height:32px}
.brand span{font-size:10px;background:var(--accent);color:#fff;padding:2px 6px;border-radius:10px;font-weight:600}
</style>
</head>
<body>
<div class="login-card">
  <div class="brand">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg>
    <h1>LiteAds</h1>
    <span>v2</span>
  </div>
  <p class="subtitle">Sign in to access the dashboard</p>
  <div class="error-msg" id="errorMsg">__ERROR__</div>
  <form method="POST" action="/dashboard/login">
    <div class="form-group">
      <label for="username">Username</label>
      <input type="text" id="username" name="username" placeholder="Enter username" required autofocus>
    </div>
    <div class="form-group">
      <label for="password">Password</label>
      <input type="password" id="password" name="password" placeholder="Enter password" required>
    </div>
    <button type="submit" class="btn-login">Sign In</button>
  </form>
</div>
</body>
</html>
"""

    @app.get("/dashboard/login", response_class=HTMLResponse, tags=["dashboard"])
    async def dashboard_login_page(
        error: str = "",
        liteads_session: str | None = Cookie(None),
    ):
        """Show the login form (or redirect if already logged in)."""
        if _verify_session(liteads_session):
            return RedirectResponse(url="/dashboard", status_code=302)
        html = _LOGIN_HTML.replace(
            '<div class="error-msg" id="errorMsg">__ERROR__</div>',
            f'<div class="error-msg show" id="errorMsg">{error}</div>' if error else '<div class="error-msg" id="errorMsg"></div>',
        )
        return HTMLResponse(content=html)

    @app.post("/dashboard/login", tags=["dashboard"])
    async def dashboard_login_submit(request: Request):
        """Validate credentials and set session cookie."""
        body = await request.body()
        form_data = parse_qs(body.decode("utf-8"))
        username = form_data.get("username", [""])[0]
        password = form_data.get("password", [""])[0]
        if username == _DASH_USER and password == _DASH_PASS:
            token = _make_session_token(username)
            response = RedirectResponse(url="/dashboard", status_code=302)
            response.set_cookie(
                key=_COOKIE_NAME,
                value=token,
                max_age=_COOKIE_MAX_AGE,
                httponly=True,
                samesite="lax",
                path="/",
            )
            logger.info("Dashboard login successful", user=username)
            return response
        logger.warning("Dashboard login failed", user=username)
        return RedirectResponse(
            url="/dashboard/login?error=Invalid+username+or+password",
            status_code=302,
        )

    @app.get("/dashboard/logout", tags=["dashboard"])
    async def dashboard_logout():
        """Clear session cookie and redirect to login."""
        response = RedirectResponse(url="/dashboard/login", status_code=302)
        response.delete_cookie(key=_COOKIE_NAME, path="/")
        return response

    @app.get("/dashboard", response_class=HTMLResponse, tags=["dashboard"])
    async def dashboard_ui(liteads_session: str | None = Cookie(None)):
        """Serve the admin dashboard (requires login)."""
        if not _verify_session(liteads_session):
            return RedirectResponse(url="/dashboard/login", status_code=302)
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