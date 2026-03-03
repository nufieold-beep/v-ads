"""
Prometheus metrics middleware for monitoring.

Provides:
- Request latency histograms
- Request counters by endpoint
- Active request gauge
- Business metrics (impressions, clicks, etc.)
"""

import time
from functools import lru_cache
from typing import Callable

from fastapi import Request, Response
from prometheus_client import Counter, Gauge, Histogram, Info, generate_latest
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response as StarletteResponse

from liteads.common.logger import get_logger

logger = get_logger(__name__)

# =============================================================================
# Prometheus Metrics Definitions
# =============================================================================

# Application info
APP_INFO = Info("liteads_app", "LiteAds application information")
APP_INFO.info({
    "version": "1.0.0",
    "name": "liteads",
    "description": "Lightweight Ad Server",
})

# HTTP request metrics
HTTP_REQUEST_TOTAL = Counter(
    "liteads_http_requests_total",
    "Total HTTP requests",
    ["method", "endpoint", "status"],
)

HTTP_REQUEST_DURATION = Histogram(
    "liteads_http_request_duration_seconds",
    "HTTP request duration in seconds",
    ["method", "endpoint"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75, 1.0, 2.5, 5.0),
)

HTTP_REQUESTS_IN_PROGRESS = Gauge(
    "liteads_http_requests_in_progress",
    "HTTP requests currently in progress",
    ["method", "endpoint"],
)

# Recommendation engine metrics
RETRIEVAL_LATENCY = Histogram(
    "liteads_retrieval_latency_seconds",
    "Retrieval stage latency",
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1),
)

FILTER_LATENCY = Histogram(
    "liteads_filter_latency_seconds",
    "Filter stage latency",
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1),
)

CANDIDATES_COUNT = Histogram(
    "liteads_candidates_count",
    "Number of candidates at each stage",
    ["stage"],
    buckets=(0, 10, 50, 100, 200, 500, 1000),
)

# Cache metrics
CACHE_HIT_TOTAL = Counter(
    "liteads_cache_hit_total",
    "Total cache hits",
    ["cache_type"],
)

CACHE_MISS_TOTAL = Counter(
    "liteads_cache_miss_total",
    "Total cache misses",
    ["cache_type"],
)

# ── CTV / SSAI delivery health metrics ──────────────────────────────────

VAST_ERRORS_TOTAL = Counter(
    "liteads_vast_errors_total",
    "VAST error events by error code and campaign",
    ["error_code", "campaign_id"],
)

AD_STARTS_TOTAL = Counter(
    "liteads_ad_starts_total",
    "Total video ad starts",
    ["campaign_id"],
)

AD_COMPLETIONS_TOTAL = Counter(
    "liteads_ad_completions_total",
    "Total video ad completions",
    ["campaign_id"],
)

AD_SKIPS_TOTAL = Counter(
    "liteads_ad_skips_total",
    "Total video ad skips",
    ["campaign_id"],
)

NO_BID_TOTAL = Counter(
    "liteads_no_bid_total",
    "Total no-bid responses (no fill)",
    ["reason"],
)

BID_FLOOR_FILTERED_TOTAL = Counter(
    "liteads_bid_floor_filtered_total",
    "Candidates removed by bid floor",
    ["slot_id"],
)


QUARTILE_FUNNEL = Counter(
    "liteads_quartile_funnel_total",
    "Video playback funnel (impression → start → Q1 → mid → Q3 → complete)",
    ["stage", "campaign_id"],
)

WIN_RATE = Histogram(
    "liteads_win_rate",
    "Auction win rate per request batch",
    buckets=(0, 0.1, 0.25, 0.5, 0.75, 1.0),
)


# =============================================================================
# Metrics Middleware
# =============================================================================

class MetricsMiddleware(BaseHTTPMiddleware):
    """
    Middleware that collects Prometheus metrics for all HTTP requests.
    """

    async def dispatch(
        self, request: Request, call_next: Callable
    ) -> Response:
        """Process request and record metrics."""
        method = request.method
        endpoint = self._get_endpoint(request)

        # Track in-progress requests
        HTTP_REQUESTS_IN_PROGRESS.labels(method=method, endpoint=endpoint).inc()

        start_time = time.perf_counter()
        status_code = 500  # Default to error

        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        except Exception as e:
            logger.error(f"Request error: {e}")
            raise
        finally:
            # Calculate duration
            duration = time.perf_counter() - start_time

            # Record metrics
            HTTP_REQUEST_TOTAL.labels(
                method=method,
                endpoint=endpoint,
                status=str(status_code),
            ).inc()

            HTTP_REQUEST_DURATION.labels(
                method=method,
                endpoint=endpoint,
            ).observe(duration)

            HTTP_REQUESTS_IN_PROGRESS.labels(
                method=method,
                endpoint=endpoint,
            ).dec()

    @staticmethod
    @lru_cache(maxsize=256)
    def _normalize_path(path: str) -> str:
        """Normalize path with LRU cache to avoid repeated string ops."""
        parts = path.split("/")
        normalized = []
        for part in parts:
            if part.isdigit():
                normalized.append("{id}")
            else:
                normalized.append(part)
        return "/".join(normalized)

    def _get_endpoint(self, request: Request) -> str:
        """Get endpoint path, normalizing path parameters."""
        return self._normalize_path(request.url.path)


# =============================================================================
# Metrics Endpoint
# =============================================================================

async def metrics_endpoint() -> StarletteResponse:
    """
    Prometheus metrics endpoint.

    Returns metrics in Prometheus text format.
    """
    return StarletteResponse(
        content=generate_latest(),
        media_type="text/plain; charset=utf-8",
    )


# =============================================================================
# Helper Functions for Recording Business Metrics
# =============================================================================

def record_vast_error(error_code: str, campaign_id: str = "unknown") -> None:
    """Record a VAST error event by error code and campaign."""
    VAST_ERRORS_TOTAL.labels(error_code=error_code, campaign_id=campaign_id).inc()


def record_ad_start(campaign_id: str) -> None:
    """Record a video ad start event."""
    AD_STARTS_TOTAL.labels(campaign_id=campaign_id).inc()


def record_ad_completion(campaign_id: str) -> None:
    """Record a video ad completion event."""
    AD_COMPLETIONS_TOTAL.labels(campaign_id=campaign_id).inc()


def record_ad_skip(campaign_id: str) -> None:
    """Record a video ad skip event."""
    AD_SKIPS_TOTAL.labels(campaign_id=campaign_id).inc()


def record_no_bid(reason: str = "no_fill") -> None:
    """Record a no-bid (no fill) response."""
    NO_BID_TOTAL.labels(reason=reason).inc()


def record_bid_floor_filtered(slot_id: str, count: int = 1) -> None:
    """Record candidates filtered by bid floor."""
    BID_FLOOR_FILTERED_TOTAL.labels(slot_id=slot_id).inc(count)


def record_quartile(stage: str, campaign_id: str) -> None:
    """Record a quartile funnel event (impression/start/q1/mid/q3/complete)."""
    QUARTILE_FUNNEL.labels(stage=stage, campaign_id=campaign_id).inc()
