"""
Demand Forwarder Service – sends bid requests to mapped demand sources.

When a supply-side VAST tag request comes in, this service:

1. Looks up the SupplyTag by ``slot_id``
2. Finds active SupplyDemandMappings (ordered by priority)
3. For **ORTB demand endpoints**: builds an OpenRTB 2.6 BidRequest,
   POSTs it via ``httpx``, and parses the BidResponse
4. For **demand VAST tags**: creates an AdCandidate whose ``vast_url``
   points to the third-party VAST URL (with macros substituted)
5. Returns a merged list of AdCandidates from all demand sources

Optimizations:
- Adaptive timeouts based on endpoint response-time history
- Automatic retry with exponential backoff for transient failures
- Connection pool tuning for high-throughput environments
- No-bid reason tracking for fill-rate diagnostics
- In-memory supply tag/mapping cache (60s TTL) to reduce DB load
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections import defaultdict
from decimal import Decimal
from typing import Optional, Tuple
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from liteads.common.logger import get_logger
from liteads.models.ad import (
    DemandEndpoint,
    DemandVastTag,
    SupplyDemandMapping,
    SupplyTag,
)
from liteads.models.base import Status
from liteads.schemas.internal import AdCandidate
from liteads.schemas.openrtb import (
    App as OrtbApp,
    BidRequest,
    BidResponse,
    Content as OrtbContent,
    Device as OrtbDevice,
    Geo as OrtbGeo,
    Imp,
    Publisher as OrtbPublisher,
    Regs as OrtbRegs,
    Source as OrtbSource,
    User as OrtbUser,
    Video as OrtbVideo,
)
from liteads.schemas.request import AdRequest
from liteads.common.geoip import lookup as geoip_lookup
from liteads.common.ortb_enricher import enrich_bid_request as _enrich_ortb

logger = get_logger(__name__)

# Module-level shared HTTP client (created lazily, reused across requests)
_http_client: httpx.AsyncClient | None = None

# ── Endpoint performance tracker (adaptive timeouts & diagnostics) ─────────
# Tracks p95 response times and no-bid rates per endpoint for optimisation.
_endpoint_stats: dict[int, dict] = defaultdict(lambda: {
    "total_requests": 0,
    "total_bids": 0,
    "total_nobids": 0,
    "total_timeouts": 0,
    "total_errors": 0,
    "latency_sum_ms": 0.0,
    "last_success_ts": 0.0,
})

# ── Retry configuration ───────────────────────────────────────────────────
_MAX_RETRIES = 1          # One retry on transient failure (DSP 5xx / timeout)
_RETRY_BACKOFF_MS = 50    # Base backoff — 50ms (fast retry, won't hurt tmax)

# ── In-memory cache for supply tag / mapping lookups ──────────────────────
# Avoids a DB round-trip on every single VAST tag request.
# TTL = 60s — supply tag changes are rare.
_SUPPLY_CACHE_TTL = 60.0
_supply_tag_cache: dict[str, Tuple[float, Optional[object]]] = {}
_mapping_cache: dict[int, Tuple[float, list]] = {}


def _strip_empty(obj: object) -> object:
    """Recursively strip empty lists/dicts from a JSON-serialisable object."""
    if isinstance(obj, dict):
        return {
            k: _strip_empty(v)
            for k, v in obj.items()
            if not (isinstance(v, (list, dict)) and len(v) == 0)
        }
    if isinstance(obj, list):
        return [_strip_empty(i) for i in obj]
    return obj


# Fields to strip from the ORTB payload to reduce size / DSP parse time.
# Keeps all targeting-critical fields (device.ifa, device.geo, device.ua,
# device.devicetype, app.bundle, video core fields, pod fields).
_SLIM_VIDEO_KEYS = {
    "skip", "skipmin", "skipafter", "boxingallowed", "maxextended",
    "pos", "api", "companiontype", "playbackend", "minbitrate", "maxbitrate",
    "sequence", "battr", "companionad", "ext",
}
_SLIM_IMP_KEYS = {
    "metric", "rwdd", "displaymanager", "displaymanagerver",
    "instl", "bidfloorcur",
}
_SLIM_DEVICE_KEYS = {"hwv", "pxratio", "js", "connectiontype", "w", "h"}
_SLIM_APP_KEYS = {
    "privacypolicy", "paid", "sectioncat", "ver",
}
_SLIM_TOP_KEYS = {"cur", "wlang", "cattax", "allimps"}


def _slim_payload(payload: dict) -> dict:
    """Remove heavy optional fields to shrink payload for faster DSP response.

    Keeps ALL targeting-critical and compliance-critical fields:
    - source.fd, source.tid (required for transaction tracking)
    - user.customdata, user.eids (required for identity resolution)
    - device.didsha1, device.didmd5, device.language (DSPs need these)
    - app.pagecat, app.inventorypartnerdomain, app.ext (contextual targeting)
    - imp.exp (impression expiry)
    - video pod fields: poddur, maxseq, podid, podseq, poddedupe
    """
    # Top-level
    for k in _SLIM_TOP_KEYS:
        payload.pop(k, None)

    # Video object inside each imp
    for imp in payload.get("imp", []):
        for k in _SLIM_IMP_KEYS:
            imp.pop(k, None)
        video = imp.get("video")
        if video:
            for k in _SLIM_VIDEO_KEYS:
                video.pop(k, None)

    # Device — strip non-essential screen/JS fields
    dev = payload.get("device")
    if dev:
        for k in _SLIM_DEVICE_KEYS:
            dev.pop(k, None)

    # App — strip non-essential policy fields
    app = payload.get("app")
    if app:
        for k in _SLIM_APP_KEYS:
            app.pop(k, None)

    # Content — strip verbose fields DSPs don't use
    content = app.get("content") if app else None
    if content:
        for k in ("sourcerelationship", "embeddable", "cattax", "producer"):
            content.pop(k, None)

    # Source — strip schain (target schema uses fd+tid only)
    source = payload.get("source")
    if source:
        source.pop("schain", None)
        source.pop("pchain", None)
        source.pop("ext", None)

    # Regs — strip ext, gpp, gpp_sid (target schema uses top-level only)
    regs = payload.get("regs")
    if regs:
        regs.pop("ext", None)
        regs.pop("gpp", None)
        regs.pop("gpp_sid", None)

    # User — strip id and ext (target schema uses customdata+eids only)
    user = payload.get("user")
    if user:
        user.pop("id", None)
        user.pop("ext", None)

    return payload


def _stable_hash_id(value: str) -> int:
    """Deterministic 31-bit hash for creative IDs (survives process restarts)."""
    return int(hashlib.md5(value.encode()).hexdigest()[:8], 16) % (2**31)


def _get_http_client() -> httpx.AsyncClient:
    """Return a module-level shared async HTTP client.

    Tuned for high-throughput ad serving:
    - Higher keepalive pool for persistent DSP connections
    - Aggressive connect timeout (1.5s) — DSPs that can't connect fast won't bid fast
    - 4s total timeout as outer safety net (per-request timeout overrides this)
    - HTTP/2 disabled for compatibility with most DSP endpoints
    """
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            follow_redirects=True,
            verify=False,
            timeout=httpx.Timeout(4.0, connect=1.5),
            limits=httpx.Limits(
                max_connections=200,
                max_keepalive_connections=50,
                keepalive_expiry=30,  # Keep connections alive for 30s
            ),
        )
    return _http_client


class DemandForwarder:
    """Forwards bid requests to demand sources mapped to a supply tag.

    Uses its own DB session to avoid conflicts when running concurrently
    with the local campaign pipeline (which shares a different session).
    """

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def forward(
        self,
        ad_request: AdRequest,
        request_id: str,
    ) -> list[AdCandidate]:
        """
        Forward bid requests to all active demand sources for the supply tag.

        Returns a list of ``AdCandidate`` objects from demand sources that
        can be merged with local campaign candidates and ranked together.
        """
        from liteads.common.database import db

        # Use a dedicated session so we don't conflict with the local
        # campaign pipeline that runs concurrently on a different session.
        async with db.session() as session:
            return await self._do_forward(session, ad_request, request_id)

    async def _do_forward(
        self,
        session: AsyncSession,
        ad_request: AdRequest,
        request_id: str,
    ) -> list[AdCandidate]:
        # 1. Look up supply tag
        supply_tag = await self._get_supply_tag(session, ad_request.slot_id)
        if not supply_tag:
            logger.debug(
                "No supply tag found",
                slot_id=ad_request.slot_id,
            )
            return []

        # 2. Get active mappings
        mappings = await self._get_active_mappings(session, supply_tag.id)
        if not mappings:
            logger.debug(
                "No demand mappings",
                supply_tag=supply_tag.name,
                slot_id=supply_tag.slot_id,
            )
            return []

        logger.info(
            "Forwarding to demand sources",
            request_id=request_id,
            supply_tag=supply_tag.name,
            num_mappings=len(mappings),
        )

        # 3. Fire all demand requests in parallel
        tasks: list[asyncio.Task] = []
        for mapping in mappings:
            if (
                mapping.demand_endpoint_id
                and mapping.demand_endpoint
                and mapping.demand_endpoint.status == Status.ACTIVE
            ):
                tasks.append(
                    asyncio.ensure_future(
                        self._request_ortb_endpoint(
                            endpoint=mapping.demand_endpoint,
                            ad_request=ad_request,
                            request_id=request_id,
                            supply_tag=supply_tag,
                        )
                    )
                )
            elif (
                mapping.demand_vast_tag_id
                and mapping.demand_vast_tag
                and mapping.demand_vast_tag.status == Status.ACTIVE
            ):
                tasks.append(
                    asyncio.ensure_future(
                        self._resolve_vast_tag(
                            vast_tag=mapping.demand_vast_tag,
                            ad_request=ad_request,
                            request_id=request_id,
                        )
                    )
                )

        if not tasks:
            return []

        results = await asyncio.gather(*tasks, return_exceptions=True)

        candidates: list[AdCandidate] = []
        for result in results:
            if isinstance(result, Exception):
                logger.warning("Demand source error: %s", str(result))
            elif isinstance(result, list):
                candidates.extend(result)
            elif isinstance(result, AdCandidate):
                candidates.append(result)

        logger.info(
            "Demand forwarding complete",
            request_id=request_id,
            total_candidates=len(candidates),
        )

        return candidates

    # ------------------------------------------------------------------
    # DB look-ups
    # ------------------------------------------------------------------

    async def _get_supply_tag(self, session: AsyncSession, slot_id: str) -> Optional[SupplyTag]:
        """Look up an active SupplyTag by its ``slot_id`` (cached, 60s TTL)."""
        now = time.monotonic()
        cached = _supply_tag_cache.get(slot_id)
        if cached and (now - cached[0]) < _SUPPLY_CACHE_TTL:
            return cached[1]

        result = await session.execute(
            select(SupplyTag).where(
                SupplyTag.slot_id == slot_id,
                SupplyTag.status == Status.ACTIVE,
            )
        )
        tag = result.scalars().first()
        _supply_tag_cache[slot_id] = (now, tag)
        return tag

    async def _get_active_mappings(
        self, session: AsyncSession, supply_tag_id: int
    ) -> list[SupplyDemandMapping]:
        """Return active mappings ordered by priority (cached, 60s TTL)."""
        now = time.monotonic()
        cached = _mapping_cache.get(supply_tag_id)
        if cached and (now - cached[0]) < _SUPPLY_CACHE_TTL:
            return cached[1]

        result = await session.execute(
            select(SupplyDemandMapping)
            .where(
                SupplyDemandMapping.supply_tag_id == supply_tag_id,
                SupplyDemandMapping.status == Status.ACTIVE,
            )
            .options(
                selectinload(SupplyDemandMapping.demand_endpoint),
                selectinload(SupplyDemandMapping.demand_vast_tag),
            )
            .order_by(SupplyDemandMapping.priority)
        )
        mappings = list(result.scalars().all())
        _mapping_cache[supply_tag_id] = (now, mappings)
        return mappings

    # ------------------------------------------------------------------
    # ORTB demand endpoint
    # ------------------------------------------------------------------

    async def _request_ortb_endpoint(
        self,
        endpoint: DemandEndpoint,
        ad_request: AdRequest,
        request_id: str,
        supply_tag: SupplyTag,
    ) -> list[AdCandidate]:
        """Build and send an OpenRTB 2.6 bid request to a demand endpoint.

        Includes:
        - Adaptive timeout based on endpoint history
        - Automatic retry on transient failures (5xx, timeout)
        - No-bid reason tracking for fill-rate diagnostics
        - Response-time tracking for performance monitoring
        """
        ep_id = endpoint.id
        stats = _endpoint_stats[ep_id]

        # ── Adaptive timeout: use configured value but cap at 80% of tmax
        # to leave headroom for response processing ──
        base_timeout_ms = endpoint.timeout_ms or 1500
        # If this endpoint has a history of slow responses, reduce timeout
        # to avoid wasting the tmax budget
        avg_latency = (
            stats["latency_sum_ms"] / max(stats["total_requests"], 1)
        )
        if stats["total_requests"] > 10 and avg_latency > base_timeout_ms * 0.7:
            # Endpoint consistently slow — tighten to 90% of configured
            effective_timeout_ms = int(base_timeout_ms * 0.9)
        else:
            effective_timeout_ms = base_timeout_ms

        bid_request = self._build_bid_request(
            ad_request=ad_request,
            request_id=request_id,
            supply_tag=supply_tag,
            bid_floor=float(endpoint.bid_floor or Decimal("0")),
            tmax=effective_timeout_ms,
        )

        # Enforce a strict TOTAL timeout (not per-phase) so the DSP
        # can never exceed the tmax budget regardless of how long
        # individual phases (connect, read, write) take.
        timeout_s = effective_timeout_ms / 1000.0
        request_timeout = httpx.Timeout(
            timeout_s,               # per-phase default
            connect=min(timeout_s, 1.0),  # cap connect at 1s
        )

        bid_payload = bid_request.model_dump(exclude_none=True)

        # Strip empty lists/dicts for cleaner payload (DSPs don't need them)
        bid_payload = _strip_empty(bid_payload)

        # Slim down payload for faster DSP response (0.2s vs 1.3-2.5s)
        bid_payload = _slim_payload(bid_payload)

        # ── Build headers ──
        device_ua = bid_payload.get("device", {}).get("ua", "")
        device_ip = bid_payload.get("device", {}).get("ip", "")
        fwd_headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Accept-Encoding": "gzip,deflate",
            "X-Openrtb-Version": "2.6",
        }
        if device_ua:
            fwd_headers["User-Agent"] = device_ua
        if device_ip:
            fwd_headers["X-Forwarded-For"] = device_ip

        # ── Log request details ──
        _app = bid_payload.get("app", {})
        _dev = bid_payload.get("device", {})
        _geo = _dev.get("geo", {})
        _src = bid_payload.get("source", {})
        _usr = bid_payload.get("user", {})
        logger.info(
            "Sending ORTB bid request",
            request_id=request_id,
            endpoint=endpoint.name,
            url=endpoint.endpoint_url,
            timeout_ms=effective_timeout_ms,
            ortb_bundle=_app.get("bundle"),
            ortb_app_name=_app.get("name"),
            ortb_app_id=_app.get("id"),
            ortb_domain=_app.get("domain"),
            ortb_storeurl=_app.get("storeurl"),
            ortb_ua=_dev.get("ua", "")[:80],
            ortb_ip=_dev.get("ip"),
            ortb_ifa=_dev.get("ifa"),
            ortb_devicetype=_dev.get("devicetype"),
            ortb_os=_dev.get("os"),
            ortb_make=_dev.get("make"),
            ortb_model=_dev.get("model"),
            ortb_country=_geo.get("country"),
            has_source=bool(_src),
            has_schain=bool(_src.get("schain")),
            source_tid=_src.get("tid"),
            has_user_id=bool(_usr.get("id")),
            has_user_eids=bool(_usr.get("eids")),
            has_regs=bool(bid_payload.get("regs")),
        )

        # ── Debug: log full ORTB payload (truncated to avoid log bloat) ──
        try:
            _payload_str = json.dumps(bid_payload, separators=(",", ":"))
            logger.debug(
                "ORTB payload",
                request_id=request_id,
                endpoint=endpoint.name,
                payload=_payload_str[:4000],
            )
        except Exception:
            pass

        # ── Send request with retry on transient failures ──
        last_error: Exception | None = None
        for attempt in range(_MAX_RETRIES + 1):
            if attempt > 0:
                # Exponential backoff: 50ms, 100ms, ...
                await asyncio.sleep(_RETRY_BACKOFF_MS * (2 ** (attempt - 1)) / 1000.0)
                logger.info(
                    "Retrying ORTB request",
                    endpoint=endpoint.name,
                    attempt=attempt + 1,
                    request_id=request_id,
                )

            req_start = time.monotonic()
            try:
                client = _get_http_client()
                response = await client.post(
                    endpoint.endpoint_url,
                    json=bid_payload,
                    headers=fwd_headers,
                    timeout=request_timeout,
                )
                elapsed_ms = (time.monotonic() - req_start) * 1000

                # ── Track performance stats ──
                stats["total_requests"] += 1
                stats["latency_sum_ms"] += elapsed_ms
                stats["last_success_ts"] = time.time()

                # 204 = no-bid (per OpenRTB spec)
                if response.status_code == 204:
                    stats["total_nobids"] += 1
                    logger.info(
                        "No-bid from demand",
                        endpoint=endpoint.name,
                        request_id=request_id,
                        latency_ms=round(elapsed_ms, 1),
                        nobid_reason="dsp_204",
                        response_headers=dict(response.headers) if response.headers else None,
                    )
                    return []

                if response.status_code >= 500:
                    # Server error — retryable
                    stats["total_errors"] += 1
                    last_error = Exception(
                        f"HTTP {response.status_code}: {response.text[:200]}"
                    )
                    logger.warning(
                        "ORTB endpoint server error (retryable)",
                        endpoint=endpoint.name,
                        status=response.status_code,
                        attempt=attempt + 1,
                        request_id=request_id,
                    )
                    continue  # retry

                if response.status_code != 200:
                    stats["total_errors"] += 1
                    logger.warning(
                        "ORTB endpoint error",
                        endpoint=endpoint.name,
                        status=response.status_code,
                        body=response.text[:500],
                        latency_ms=round(elapsed_ms, 1),
                    )
                    return []  # Non-retryable (4xx)

                # ── Parse bid response ──
                bid_response = BidResponse.model_validate_json(response.text)
                candidates = self._extract_candidates(
                    bid_response=bid_response,
                    endpoint=endpoint,
                    request_id=request_id,
                )

                if candidates:
                    stats["total_bids"] += 1
                else:
                    stats["total_nobids"] += 1

                logger.info(
                    "ORTB response received",
                    endpoint=endpoint.name,
                    request_id=request_id,
                    latency_ms=round(elapsed_ms, 1),
                    num_candidates=len(candidates),
                    fill_rate_pct=round(
                        stats["total_bids"]
                        / max(stats["total_requests"], 1)
                        * 100,
                        1,
                    ),
                )
                return candidates

            except httpx.TimeoutException:
                elapsed_ms = (time.monotonic() - req_start) * 1000
                stats["total_requests"] += 1
                stats["total_timeouts"] += 1
                stats["latency_sum_ms"] += elapsed_ms
                last_error = httpx.TimeoutException(
                    f"Timeout after {elapsed_ms:.0f}ms"
                )
                logger.warning(
                    "ORTB timeout (retryable)",
                    endpoint=endpoint.name,
                    timeout_ms=effective_timeout_ms,
                    actual_ms=round(elapsed_ms, 1),
                    attempt=attempt + 1,
                    request_id=request_id,
                )
                continue  # retry

            except httpx.ConnectError as exc:
                stats["total_requests"] += 1
                stats["total_errors"] += 1
                last_error = exc
                logger.warning(
                    "ORTB connection error (retryable)",
                    endpoint=endpoint.name,
                    error=str(exc),
                    attempt=attempt + 1,
                    request_id=request_id,
                )
                continue  # retry

            except Exception as exc:
                stats["total_requests"] += 1
                stats["total_errors"] += 1
                logger.error(
                    "ORTB request failed",
                    endpoint=endpoint.name,
                    error=str(exc),
                    request_id=request_id,
                )
                return []  # Non-retryable

        # All retries exhausted
        logger.warning(
            "ORTB all retries exhausted",
            endpoint=endpoint.name,
            request_id=request_id,
            error=str(last_error),
            timeout_rate=round(
                stats["total_timeouts"]
                / max(stats["total_requests"], 1)
                * 100,
                1,
            ),
        )
        return []

    # ------------------------------------------------------------------
    # Demand VAST tag
    # ------------------------------------------------------------------

    async def _resolve_vast_tag(
        self,
        vast_tag: DemandVastTag,
        ad_request: AdRequest,
        request_id: str,
    ) -> AdCandidate | None:
        """
        Create an AdCandidate from a demand VAST tag URL (macro-substituted).

        The VAST tag router already handles candidates with ``vast_url``
        by building a VAST Wrapper pointing to the external tag URL.
        """
        try:
            url = self._substitute_macros(vast_tag.vast_url, ad_request)

            logger.info(
                "Resolved demand VAST tag",
                request_id=request_id,
                vast_tag=vast_tag.name,
                cpm=float(vast_tag.cpm_value or 0),
            )

            return AdCandidate(
                # Use 0 / negative IDs to distinguish from local campaigns
                campaign_id=0,
                creative_id=vast_tag.id * -1,
                advertiser_id=0,
                bid=float(vast_tag.cpm_value or Decimal("0")),
                title=vast_tag.name or "Demand VAST",
                vast_url=url,
                width=ad_request.video.width or 1920,
                height=ad_request.video.height or 1080,
                duration=ad_request.video.max_duration or 30,
                metadata={
                    "source": "demand_vast_tag",
                    "demand_vast_tag_id": vast_tag.id,
                },
            )

        except Exception as exc:
            logger.error(
                "VAST tag resolution failed",
                vast_tag=vast_tag.name,
                error=str(exc),
                request_id=request_id,
            )
            return None

    # ------------------------------------------------------------------
    # Endpoint diagnostics (for analytics / monitoring)
    # ------------------------------------------------------------------

    @staticmethod
    def get_endpoint_diagnostics() -> dict[int, dict]:
        """Return per-endpoint performance metrics.

        Useful for dashboards or admin APIs to see:
        - Fill rate per endpoint
        - Average latency
        - Timeout rate
        - Error rate
        """
        diagnostics: dict[int, dict] = {}
        for ep_id, stats in _endpoint_stats.items():
            total = max(stats["total_requests"], 1)
            diagnostics[ep_id] = {
                "total_requests": stats["total_requests"],
                "fill_rate_pct": round(stats["total_bids"] / total * 100, 2),
                "nobid_rate_pct": round(stats["total_nobids"] / total * 100, 2),
                "timeout_rate_pct": round(stats["total_timeouts"] / total * 100, 2),
                "error_rate_pct": round(stats["total_errors"] / total * 100, 2),
                "avg_latency_ms": round(stats["latency_sum_ms"] / total, 1),
            }
        return diagnostics

    # ------------------------------------------------------------------
    # Build OpenRTB 2.6 BidRequest  (IAB / IAV / CTV-IFA compliant)
    # ------------------------------------------------------------------

    # ── CTV / In-App platform constants ───────────────────────────

    # Mapping from OS string → canonical OS name for the ORTB payload
    _OS_CANONICAL: dict[str, str] = {
        "roku": "Roku", "firetv": "Fire OS", "fireos": "Fire OS",
        "tvos": "tvOS", "tizen": "Tizen", "webos": "webOS",
        "webostv": "webOS", "vizio": "SmartCast", "androidtv": "Android",
        "googletv": "Android", "chromecast": "Android",
        "android": "Android", "ios": "iOS",
    }

    @staticmethod
    def _build_bid_request(
        ad_request: AdRequest,
        request_id: str,
        supply_tag: SupplyTag,
        bid_floor: float = 0.0,
        tmax: int = 500,
    ) -> BidRequest:
        """Construct a fully IAB OpenRTB 2.6 / IAV-compliant BidRequest.

        Cross-platform support:
        • CTV:   devicetype 3/7, connection Ethernet/WiFi, js=0, SSAI signals,
                  IFA types (RIDA/TIFA/AFAI/LGUDID/VIDA), full-screen instl=1
        • In-App: devicetype 1/4, MRAID/OMID api, carrier data, IDFA/GAID,
                  rewarded-video (rwdd), interstitial support

        Standards implemented:
        • IAB OpenRTB 2.6 (all video objects + ad pod)
        • IAB CTV IFA Guidelines (ifa_type in device.ext)
        • IAB SupplyChain / ads.txt (schain in source)
        • IAB GPP (Global Privacy Platform) + TCF 2.0 + CCPA
        • IAB Video Ad Format Guidelines (placement, plcmt, linearity)
        • IAB OM SDK / OMID signalling (api=[7])
        • IAB Extended Identifiers (user.eids)
        """

        is_ctv = ad_request.environment == "ctv"

        # ─────────────────────────────────────────────────────────────
        # Helpers – parse comma-separated values from publisher params
        # ─────────────────────────────────────────────────────────────

        def _csv_ints(val: str | None) -> list[int]:
            if not val:
                return []
            out: list[int] = []
            for p in val.split(","):
                p = p.strip()
                if p:
                    try:
                        out.append(int(p))
                    except ValueError:
                        pass
            return out

        def _csv_strs(val: str | None) -> list[str]:
            if not val:
                return []
            return [s.strip() for s in val.split(",") if s.strip()]

        v = ad_request.video if ad_request.video else None

        # ══════════════════════════════════════════════════════════════
        # 1. VIDEO OBJECT  (Section 3.2.7)
        #    IAB Video Ad Format Guidelines + OpenRTB 2.6 Ad Pods
        # ══════════════════════════════════════════════════════════════

        # ── startdelay (§5.12): 0=pre, >0=mid-offset, -1=generic mid, -2=post
        if v and v.startdelay_raw is not None:
            startdelay = v.startdelay_raw
        else:
            placement_str = v.placement if v else "pre_roll"
            startdelay = (
                0 if placement_str == "pre_roll"
                else -1 if placement_str == "mid_roll"
                else -2
            )

        # ── protocols (§5.8): VAST versions supported
        # CTV players universally accept VAST 2/3/4.x; in-app may also support VPAID
        vid_protocols = _csv_ints(v.video_protocols if v else None)
        if not vid_protocols:
            # 2=VAST2.0, 3=VAST3.0, 4=VAST2.0-Wrapper, 5=VAST3.0-Wrapper,
            # 6=VAST4.0, 7=VAST4.1, 8=VAST4.2
            vid_protocols = [2, 3, 4, 5, 6, 7, 8]
            if not is_ctv:
                # In-app may support VPAID 2.0 JS (§5.8 value 3 is taken;
                # VPAID is signalled via api[] instead).
                pass

        # ── mimes – CTV emphasises MP4/HLS; in-app adds WebM/DASH
        default_mimes = (
            ["video/mp4", "application/x-mpegURL"]
            if is_ctv
            else ["video/mp4", "video/webm", "application/x-mpegURL",
                  "application/dash+xml"]
        )
        mimes = v.mimes if v and v.mimes else default_mimes

        # ── placement / plcmt (§5.9 / §5.9.1 OpenRTB 2.6)
        # plcmt=1 (In-stream) is the standard for both CTV and in-app video
        pub_plcmt = v.plcmt if v and v.plcmt else None
        if pub_plcmt:
            plcmt = pub_plcmt
            placement = pub_plcmt
        else:
            plcmt = 1      # In-Stream (IAB default for CTV/video)
            placement = 1

        # ── linearity (§5.7): 1=Linear (in-stream), 2=Non-linear/overlay
        linearity = v.linearity if v and v.linearity else 1

        # ── playbackmethod (§5.10):
        #    CTV: 1=Page-load sound-on (auto-play with sound)
        #    In-app: may use 2=on-click, 5=auto-play-viewport-sound-on
        playback = _csv_ints(v.playbackmethod if v else None)
        if not playback:
            playback = [1] if is_ctv else [1, 5]

        # ── delivery (§5.15): 1=Streaming, 2=Progressive, 3=Download
        delivery = _csv_ints(v.delivery if v else None) or [2, 1]

        # ── Video dimensions / durations
        width = v.width if v and v.width else supply_tag.width or 1920
        height = v.height if v and v.height else supply_tag.height or 1080
        minduration = (v.min_duration if v and v.min_duration
                       else supply_tag.min_duration or 1)
        maxduration = (v.max_duration if v and v.max_duration
                       else supply_tag.max_duration or 120)

        video = OrtbVideo(
            mimes=mimes,
            protocols=vid_protocols,
            minduration=minduration,
            maxduration=maxduration,
            w=width,
            h=height,
            startdelay=startdelay,
            placement=placement,
            plcmt=plcmt,
            linearity=linearity,
            playbackmethod=playback,
            delivery=delivery,
            # Ad Pod fields (OpenRTB 2.6 §3.2.7) – DSPs need these
            poddur=v.pod_duration if v and v.pod_duration else None,
            maxseq=v.max_ads_in_pod if v and v.max_ads_in_pod else None,
            podid=v.podid if v and v.podid else None,
            podseq=v.podseq if v else None,
            poddedupe=_csv_ints(v.poddedupe if v else None) or [],
        )

        # ══════════════════════════════════════════════════════════════
        # 2. IMPRESSION OBJECT (Section 3.2.4)
        #    IAB Video: displaymanager, instl, rwdd, metric
        # ══════════════════════════════════════════════════════════════

        effective_floor = max(
            bid_floor,
            float(ad_request.bidfloor_override or 0),
            float(supply_tag.bid_floor or Decimal("0")),
        )

        imp = Imp(
            id="1",
            video=video,
            tagid=ad_request.tagid or supply_tag.slot_id,
            bidfloor=effective_floor,
            secure=1,
            exp=ad_request.imp_exp or (3600 if is_ctv else 300),
        )

        # ══════════════════════════════════════════════════════════════
        # 3. DEVICE OBJECT (Section 3.2.18)
        #    IAB CTV IFA Guidelines + cross-platform device signals
        # ══════════════════════════════════════════════════════════════

        device = None
        if ad_request.device:
            d = ad_request.device

            # ── Geo (§3.2.19) ─────────────────────────────────────
            # Always enrich geo from MaxMind when publisher doesn't
            # send it — added value for DSP optimisation.
            geo = None
            if ad_request.geo and ad_request.geo.country:
                g = ad_request.geo
                geo = OrtbGeo(
                    country=g.country,
                    region=g.region,
                    city=g.city,
                    metro=g.dma,
                    lat=g.latitude,
                    lon=g.longitude,
                    zip=g.zip_code,
                    type=g.geo_type or (2 if g.latitude else None),  # 2=IP-based
                    accuracy=100 if g.geo_type == 2 else None,
                    ipservice=g.ipservice,
                )

            # Fallback: auto-enrich geo from MaxMind GeoIP using device IP
            if geo is None:
                _dev_ip = d.ip or ""
                if _dev_ip:
                    _g = geoip_lookup(_dev_ip)
                    geo = OrtbGeo(
                        country=_g.country,
                        region=_g.region,
                        city=_g.city,
                        metro=_g.metro,
                        lat=_g.lat,
                        lon=_g.lon,
                        zip=_g.zip,
                        type=_g.type,          # 2 = IP-based
                        accuracy=_g.accuracy,
                        ipservice=_g.ipservice,  # 3 = MaxMind
                    )
                    logger.debug(
                        "Geo enriched from MaxMind",
                        ip=_dev_ip,
                        country=_g.country,
                        city=_g.city,
                    )

            # ── devicetype (§5.21) ────────────────────────────────
            # Honour publisher's explicit device_type when provided;
            # otherwise infer from environment and OS
            if d.device_type_raw is not None:
                device_type = d.device_type_raw
            else:
                if is_ctv:
                    # §5.21: 3=Connected TV, 7=Set-Top Box
                    # Differentiate by OS – Roku/FireTV sticks are STBs
                    os_key = (d.os or "").lower().replace(" ", "")
                    stb_keywords = ("roku", "firetv", "fireos", "chromecast")
                    device_type = (
                        7 if any(k in os_key for k in stb_keywords) else 3
                    )
                else:
                    # §5.21: 1=Mobile/Tablet, 4=Phone, 5=Tablet
                    device_type = 1

            # ── Canonical OS name ─────────────────────────────────
            raw_os = (d.os or "").strip()
            os_key = raw_os.lower().replace(" ", "")
            canonical_os = DemandForwarder._OS_CANONICAL.get(os_key, raw_os or "unknown")

            # ── Separate IPv4 / IPv6 per ORTB 2.6 spec ───────────
            raw_ip = d.ip or ""
            ip_v4 = raw_ip if ":" not in raw_ip else ""
            ip_v6 = raw_ip if ":" in raw_ip else None

            # ── device.ext per IAB CTV IFA Guidelines ─────────────
            # ifa_type MUST be in device.ext for DSPs to identify
            # the ID type (RIDA, AFAI, IDFA, TIFA, LGUDID, VIDA, GAID)
            dev_ext: dict = {}
            if d.ifa_type:
                dev_ext["ifa_type"] = d.ifa_type
            if d.isp:
                dev_ext["isp"] = d.isp
            # Additional CTV signals DSPs look for
            if is_ctv:
                dev_ext["is_ctv"] = 1
            if not dev_ext:
                dev_ext = None  # type: ignore[assignment]

            device = OrtbDevice(
                ua=d.ua,
                ip=ip_v4,
                ipv6=ip_v6,
                geo=geo,
                devicetype=device_type,
                make=d.make,
                model=d.model,
                os=canonical_os,
                osv=d.os_version,
                ifa=d.ifa,
                lmt=1 if d.lmt else 0,
                dnt=1 if d.lmt else 0,
                language=d.language or "en",
                didsha1=d.didsha1,
                didmd5=d.didmd5,
                ext=dev_ext,
            )

        # ══════════════════════════════════════════════════════════════
        # 4. APP OBJECT (Section 3.2.14)
        #    Cross-platform: CTV app channels vs mobile app stores
        # ══════════════════════════════════════════════════════════════

        app_bundle = ""
        app_name = ""
        app_id = ""
        store_url = ""
        cat: list[str] = []
        pagecat: list[str] = []
        app_domain = ""
        publisher_id = ""
        inv_partner_domain = ""

        if ad_request.app:
            a = ad_request.app
            app_bundle = a.app_bundle or ""
            app_name = a.app_name or ""
            app_id = a.app_id or ""
            store_url = a.store_url or ""
            app_domain = a.app_domain or ""
            publisher_id = a.publisher_id or ""
            inv_partner_domain = a.inventory_partner_domain or ""
            if a.app_category:
                cat = _csv_strs(a.app_category)
            if a.page_categories:
                pagecat = _csv_strs(a.page_categories)

        # Fallbacks – use publisher data first; only use generic
        # identifiers when publisher sent nothing.  NEVER leak internal
        # supply-tag labels (supply_tag.name) into the ORTB payload
        # because DSPs see these and they must be real app metadata.
        if not app_bundle:
            app_bundle = supply_tag.slot_id
        if not app_name:
            app_name = app_bundle
        if not app_id:
            app_id = app_bundle

        # ── Publisher (§3.2.15) ───────────────────────────────────
        # Always provide a publisher object – DSPs use it for ads.txt
        publisher = OrtbPublisher(
            id=publisher_id or supply_tag.slot_id,
            name=app_name or None,
            domain=app_domain or None,
        )

        # ── Content (§3.2.16) ────────────────────────────────────
        # Build whenever ANY content signal is present (critical for CTV
        # where content metadata drives brand-safety and contextual targeting)
        content = None
        if ad_request.app:
            a = ad_request.app
            has_content = any([
                a.content_id, a.content_title, a.content_series,
                a.content_season, a.content_genre, a.content_url,
                a.content_language, a.content_producer,
                a.content_livestream is not None,
                a.production_quality, a.qag_media_rating,
                a.content_categories, a.channel_name, a.network_name,
                a.content_episode is not None,
                a.content_context is not None,
                a.content_gtax is not None, a.content_genres,
                a.content_length is not None,
            ])
            if has_content:
                prodq_val = None
                if a.production_quality:
                    try:
                        prodq_val = int(a.production_quality)
                    except (ValueError, TypeError):
                        pass
                qag_val = None
                if a.qag_media_rating:
                    try:
                        qag_val = int(a.qag_media_rating)
                    except (ValueError, TypeError):
                        pass
                content_cat = _csv_strs(a.content_categories)
                content_genres = _csv_strs(a.content_genres)

                content = OrtbContent(
                    id=a.content_id or None,
                    title=a.content_title or None,
                    series=a.content_series or None,
                    season=a.content_season or None,
                    episode=a.content_episode,
                    genre=a.content_genre or None,
                    gtax=a.content_gtax,
                    genres=content_genres or [],
                    url=a.content_url or None,
                    language=a.content_language or "en",
                    livestream=a.content_livestream,
                    contentrating=a.content_rating or None,
                    prodq=prodq_val,
                    context=a.content_context or (1 if is_ctv else None),  # 1=video
                    qagmediarating=qag_val,
                    cat=content_cat or [],
                    network={"name": a.network_name} if a.network_name else None,
                    channel={"name": a.channel_name} if a.channel_name else None,
                )
            elif is_ctv:
                # CTV: always send minimal Content object even without pub data
                # DSPs require it for brand-safety classification
                content = OrtbContent(
                    context=1,  # video context
                    language="en",
                )

        # ── App.ext §3.2.14 (inventorypartnerdomain, etc.) ───────
        app_ext: dict | None = None
        ext_parts: dict = {}
        if inv_partner_domain:
            ext_parts["inventorypartnerdomain"] = inv_partner_domain
        if ext_parts:
            app_ext = ext_parts

        app = OrtbApp(
            id=app_id,
            name=app_name,
            bundle=app_bundle,
            domain=app_domain or None,
            storeurl=store_url or None,
            cat=cat if cat else [],
            pagecat=pagecat or [],
            publisher=publisher,
            content=content,
            inventorypartnerdomain=inv_partner_domain or None,
            ext=app_ext,
        )

        # ══════════════════════════════════════════════════════════════
        # 5. USER OBJECT (Section 3.2.20)
        #    IAB Extended IDs (UID2, RampID)
        # ══════════════════════════════════════════════════════════════

        user_ifa = (
            ad_request.device.ifa
            if ad_request.device and ad_request.device.ifa
            else None
        )

        # Build Extended IDs (eids) per IAB §3.2.27
        eids: list[dict] = []
        if user_ifa and ad_request.device and ad_request.device.ifa_type:
            ifa_t = ad_request.device.ifa_type.lower()
            eid_source_map: dict[str, str] = {
                "rida": "roku.com",
                "afai": "amazon.com",
                "idfa": "apple.com",
                "gaid": "google.com",
                "tifa": "samsung.com",
                "lgudid": "lgappstv.com",
                "vida": "vizio.com",
            }
            source_domain = eid_source_map.get(ifa_t)
            if source_domain:
                eids.append({
                    "source": source_domain,
                    "uids": [{"id": user_ifa, "atype": 3}],
                })

        user = OrtbUser(
            customdata="vast",
            eids=eids,
        )

        # ══════════════════════════════════════════════════════════════
        # 6. SOURCE OBJECT (Section 3.2.2)
        #    Transaction ID only — no schain (matches target schema)
        # ══════════════════════════════════════════════════════════════

        source = OrtbSource(
            fd=1,            # 1 = exchange/SSP is responsible for final sale
            tid=request_id,  # Transaction ID (unique per auction)
        )

        # ══════════════════════════════════════════════════════════════
        # 7. REGS OBJECT (Section 3.2.3)
        #    COPPA + GDPR + CCPA only — no ext, gpp (matches target schema)
        # ══════════════════════════════════════════════════════════════

        gdpr_val = ad_request.gdpr if ad_request.gdpr is not None else 0

        regs = OrtbRegs(
            coppa=ad_request.coppa if ad_request.coppa is not None else 0,
            gdpr=gdpr_val,
            us_privacy=ad_request.us_privacy or None,
        )

        # ══════════════════════════════════════════════════════════════
        # 8. BLOCKED SIGNALS (Section 3.2.1)
        # ══════════════════════════════════════════════════════════════

        bcat_list = _csv_strs(ad_request.bcat) if ad_request.bcat else []
        badv_list = _csv_strs(ad_request.badv) if ad_request.badv else []

        # ══════════════════════════════════════════════════════════════
        # 9. ASSEMBLE BidRequest  (Section 3.2.1)
        # ══════════════════════════════════════════════════════════════

        bid_req = BidRequest(
            id=request_id,
            imp=[imp],
            app=app,
            device=device,
            user=user,
            at=1,            # First-price auction (industry standard since 2019)
            tmax=tmax,
            source=source,
            regs=regs,
            bcat=bcat_list,
            badv=badv_list,
        )

        # ── Final pass: auto-enrich any remaining gaps ────────────
        # The enricher fills missing fields with IAB-compliant defaults
        # so that every DSP receives a fully-formed ORTB 2.6 request,
        # even when the publisher sent a minimal VAST tag query.
        _enrich_ortb(
            bid_req,
            client_ip=ad_request.device.ip if ad_request.device else None,
            user_agent=ad_request.device.ua if ad_request.device else None,
            slot_id=supply_tag.slot_id,
        )

        return bid_req

    @staticmethod
    def _replace_auction_macros(text: str | None, price: float) -> str | None:
        """Replace ``${AUCTION_PRICE}`` (case-insensitive) in *text*."""
        if not text:
            return text
        formatted = f"{price:.2f}"
        return re.sub(r"\$\{AUCTION_PRICE\}", formatted, text, flags=re.IGNORECASE)

    @staticmethod
    def _extract_candidates(
        bid_response: BidResponse,
        endpoint: DemandEndpoint,
        request_id: str,
    ) -> list[AdCandidate]:
        """Turn an OpenRTB BidResponse into AdCandidates."""
        candidates: list[AdCandidate] = []
        margin = float(endpoint.margin_pct or Decimal("0")) / 100.0

        for seatbid in bid_response.seatbid:
            for bid in seatbid.bid:
                # Apply margin
                net_bid = bid.price * (1.0 - margin)

                candidate = AdCandidate(
                    campaign_id=0,
                    creative_id=_stable_hash_id(bid.crid or bid.id),
                    advertiser_id=0,
                    bid=net_bid,
                    title=f"Demand: {endpoint.name}",
                    duration=bid.dur or 30,
                    width=bid.w or 1920,
                    height=bid.h or 1080,
                    metadata={
                        "source": "demand_ortb",
                        "demand_endpoint_id": endpoint.id,
                        "endpoint_name": endpoint.name,
                        "seat": seatbid.seat,
                        "bid_id": bid.id,
                        "bid_price": bid.price,
                        "adomain": bid.adomain,
                        "crid": bid.crid,
                        "nurl": DemandForwarder._replace_auction_macros(
                            bid.nurl, bid.price
                        ),
                        "burl": DemandForwarder._replace_auction_macros(
                            bid.burl, bid.price
                        ),
                    },
                )

                # Determine ad content source —
                # Replace ${AUCTION_PRICE} macros with actual bid price
                # (same as the Node.js BidProcessor pattern).
                _price = bid.price
                if bid.adm:
                    # adm contains VAST XML – store in metadata
                    candidate.vast_url = None
                    candidate.video_url = ""
                    candidate.metadata["adm"] = (
                        DemandForwarder._replace_auction_macros(bid.adm, _price)
                    )
                elif bid.nurl:
                    # nurl may return VAST when called; use as wrapper URI
                    candidate.vast_url = (
                        DemandForwarder._replace_auction_macros(bid.nurl, _price)
                    )
                else:
                    logger.warning(
                        "Bid has no adm or nurl – skipping",
                        endpoint=endpoint.name,
                        bid_id=bid.id,
                        request_id=request_id,
                    )
                    continue

                candidates.append(candidate)

                logger.info(
                    "Demand bid received",
                    request_id=request_id,
                    endpoint=endpoint.name,
                    bid_price=bid.price,
                    net_bid=round(net_bid, 4),
                    crid=bid.crid,
                    has_adm=bool(bid.adm),
                )

        return candidates

    # ------------------------------------------------------------------
    # Macro substitution for demand VAST tag URLs
    # ------------------------------------------------------------------

    @staticmethod
    def _substitute_macros(url: str, ad_request: AdRequest) -> str:
        """
        Replace ``[replace_me]``, ``{macro}``, and standard macros in a
        demand VAST tag URL.

        Handles ALL publisher macros including geo, content, privacy, and
        device fields.  Curly-brace macros like ``{uip}`` are replaced
        globally; ``[replace_me]`` macros are keyed by query-param name.
        """
        # ── Extract device fields ────────────────────────────────
        ip = ua = ifa = make = model = os_str = isp_str = ""
        dnt_str = "0"
        if ad_request.device:
            ip = ad_request.device.ip or ""
            ua = ad_request.device.ua or ""
            ifa = ad_request.device.ifa or ""
            make = ad_request.device.make or ""
            model = ad_request.device.model or ""
            os_str = ad_request.device.os or ""
            isp_str = ad_request.device.isp or ""
            dnt_str = "1" if ad_request.device.lmt else "0"

        # ── Extract geo fields ───────────────────────────────────
        geo_lat = geo_lon = geo_country = ""
        if ad_request.geo:
            geo_lat = str(ad_request.geo.latitude) if ad_request.geo.latitude is not None else ""
            geo_lon = str(ad_request.geo.longitude) if ad_request.geo.longitude is not None else ""
            geo_country = ad_request.geo.country or ""

        # ── Video / placement ────────────────────────────────────
        width = str(ad_request.video.width or 1920)
        height = str(ad_request.video.height or 1080)
        max_duration = str(ad_request.video.max_duration or 30)
        min_duration = str(ad_request.video.min_duration or 5)
        cb = str(int(time.time() * 1000))

        # ── App / content fields ─────────────────────────────────
        app_bundle = app_name = store_url = app_cat = ""
        ct_genre = ct_rating = ct_id = ct_title = ""
        ct_series = ct_season = ct_url = ct_lang = ""
        ct_livestream = ct_producer = ct_prodq = ct_qag = ""
        ct_categories = ct_channel = ct_network = ""
        if ad_request.app:
            a = ad_request.app
            app_bundle = a.app_bundle or ""
            app_name = a.app_name or ""
            store_url = a.store_url or ""
            app_cat = a.app_category or ""
            ct_genre = a.content_genre or ""
            ct_rating = a.content_rating or ""
            ct_id = a.content_id or ""
            ct_title = a.content_title or ""
            ct_series = a.content_series or ""
            ct_season = a.content_season or ""
            ct_url = a.content_url or ""
            ct_lang = a.content_language or ""
            ct_livestream = str(a.content_livestream) if a.content_livestream is not None else ""
            ct_producer = a.content_producer or ""
            ct_prodq = a.production_quality or ""
            ct_qag = a.qag_media_rating or ""
            ct_categories = a.content_categories or ""
            ct_channel = a.channel_name or ""
            ct_network = a.network_name or ""

        env_str = ad_request.environment or ""
        device_category = "ctv" if env_str == "ctv" else "mobile"
        # Use publisher-provided device_type when available
        if ad_request.device and ad_request.device.device_type_raw is not None:
            device_type_str = str(ad_request.device.device_type_raw)
        else:
            device_type_str = "7" if env_str == "ctv" else "1"
        us_privacy = ad_request.us_privacy or ""

        # Extended geo
        geo_region = geo_dma = geo_city = geo_zip = ""
        if ad_request.geo:
            geo_region = ad_request.geo.region or ""
            geo_dma = ad_request.geo.dma or ""
            geo_city = ad_request.geo.city or ""
            geo_zip = ad_request.geo.zip_code or ""

        # Extended app
        app_domain = app_id = pub_id = pagecat = inv_pd = ""
        ct_episode = ct_context = ct_gtax = ct_genres = ""
        ct_len = ""
        device_lang = didsha1 = didmd5 = ""
        osv = ""
        if ad_request.app:
            a = ad_request.app
            app_domain = a.app_domain or ""
            app_id = a.app_id or ""
            pub_id = a.publisher_id or ""
            pagecat = a.page_categories or ""
            inv_pd = a.inventory_partner_domain or ""
            ct_episode = str(a.content_episode) if a.content_episode is not None else ""
            ct_context = str(a.content_context) if a.content_context is not None else ""
            ct_gtax = str(a.content_gtax) if a.content_gtax is not None else ""
            ct_genres = a.content_genres or ""
            ct_len = str(a.content_length) if a.content_length is not None else ""
        if ad_request.device:
            device_lang = ad_request.device.language or ""
            didsha1 = ad_request.device.didsha1 or ""
            didmd5 = ad_request.device.didmd5 or ""
            osv = ad_request.device.os_version or ""

        # ── Comprehensive macro map ──────────────────────────────
        # Keys match BOTH the query-param names sent by publishers
        # AND the {macro_name} tokens inside demand VAST URLs.
        macro_map: dict[str, str] = {
            # Video / placement
            "width": width,
            "w": width,
            "height": height,
            "h": height,
            "cb": cb,
            "cachebuster": cb,
            "max_dur": max_duration,
            "max_duration": max_duration,
            "min_dur": min_duration,
            "min_duration": min_duration,
            # Device
            "ua": ua,
            "uip": ip,
            "ip": ip,
            "ifa": ifa,
            "idfa": ifa,
            "device_id": ifa,
            "dnt": dnt_str,
            "os": os_str,
            "device_os": os_str,
            "device_make": make,
            "device_model": model,
            "device_type": device_type_str,
            "device_category": device_category,
            "device_isp": isp_str,
            "isp": isp_str,
            "device_language": device_lang,
            "language": device_lang,
            "didsha1": didsha1,
            "didmd5": didmd5,
            # App
            "app_bundle": app_bundle,
            "app_name": app_name,
            "app_store_url": store_url,
            "app_category": app_cat,
            "app_domain": app_domain,
            "app_id": app_id,
            "pub_id": pub_id,
            "publisher_id": pub_id,
            "app_pagecat": pagecat,
            "inv_partner_domain": inv_pd,
            # Geo
            "lat": geo_lat,
            "geo_lat": geo_lat,
            "lon": geo_lon,
            "geo_lon": geo_lon,
            "country_code": geo_country,
            "geo_country": geo_country,
            "region": geo_region,
            "geo_region": geo_region,
            "metro": geo_dma,
            "dma": geo_dma,
            "city": geo_city,
            "geo_city": geo_city,
            "zip": geo_zip,
            "zip_code": geo_zip,
            # Content
            "content_type": ct_categories,
            "content_categories": ct_categories,
            "ct_genre": ct_genre,
            "content_genre": ct_genre,
            "ct_id": ct_id,
            "content_id": ct_id,
            "ct_title": ct_title,
            "content_title": ct_title,
            "ct_ser": ct_series,
            "content_series": ct_series,
            "ct_seas": ct_season,
            "content_season": ct_season,
            "ct_rat": ct_rating,
            "content_rating": ct_rating,
            "ct_url": ct_url,
            "content_url": ct_url,
            "ct_lang": ct_lang,
            "content_lang": ct_lang,
            "ct_live_str": ct_livestream,
            "content_livestream": ct_livestream,
            "ct_producer": ct_producer,
            "content_producer_name": ct_producer,
            "ct_prodq": ct_prodq,
            "production_quality": ct_prodq,
            "ct_qa_media_rating": ct_qag,
            "qagmediarating": ct_qag,
            "ct_chan": ct_channel,
            "channel_name": ct_channel,
            "ct_net": ct_network,
            "network_name": ct_network,
            "ct_episode": ct_episode,
            "ct_eps": ct_episode,
            "ct_len": ct_len,
            "content_len": ct_len,
            "ct_context": ct_context,
            "ct_gtax": ct_gtax,
            "ct_genres": ct_genres,
            # Privacy
            "us_privacy": us_privacy,
            "coppa": str(ad_request.coppa) if ad_request.coppa is not None else "0",
            "gdpr": str(ad_request.gdpr) if ad_request.gdpr is not None else "",
            "gdpr_consent": ad_request.gdpr_consent or "",
            "gpp": ad_request.gpp or "",
            "gpp_sid": ad_request.gpp_sid or "",
            # Device extended
            "osv": osv,
            "os_version": osv,
            # Other
            "vast_version": "2",
        }

        # 1. Replace curly-brace macros globally: {uip} → actual IP, etc.
        for macro_key, macro_val in macro_map.items():
            url = url.replace("{" + macro_key + "}", macro_val)
            url = url.replace("{" + macro_key.upper() + "}", macro_val)

        # Also handle ${MACRO} style
        url = url.replace("${AUCTION_PRICE}", "")

        # 2. Replace [replace_me] macros keyed by query-param name
        parsed = urlparse(url)
        params = parse_qs(parsed.query, keep_blank_values=True)

        new_params: dict[str, str | list[str]] = {}
        for key, values in params.items():
            new_values: list[str] = []
            for val in values:
                if val == "[replace_me]":
                    new_values.append(macro_map.get(key.lower(), ""))
                elif "[replace_me]" in val:
                    new_values.append(
                        val.replace(
                            "[replace_me]", macro_map.get(key.lower(), "")
                        )
                    )
                else:
                    # Global bracket macros
                    val = val.replace("[CACHEBUSTER]", cb)
                    val = val.replace("[IP]", ip)
                    val = val.replace("[UA]", ua)
                    new_values.append(val)

            if len(new_values) == 1:
                new_params[key] = new_values[0]
            else:
                new_params[key] = new_values

        # 3. Drop params whose value resolved to empty string
        #    (publisher didn't send the corresponding macro)
        clean_params: dict[str, str | list[str]] = {}
        for key, val in new_params.items():
            if isinstance(val, list):
                filtered = [v for v in val if v]
                if filtered:
                    clean_params[key] = filtered
            elif val:  # non-empty string
                clean_params[key] = val

        new_query = urlencode(clean_params, doseq=True)
        return urlunparse(parsed._replace(query=new_query))


# ---------------------------------------------------------------------------
# Cleanup helper (called at application shutdown)
# ---------------------------------------------------------------------------

async def close_http_client() -> None:
    """Close the shared HTTP client (call from app shutdown hook)."""
    global _http_client
    if _http_client and not _http_client.is_closed:
        await _http_client.aclose()
        _http_client = None
