"""
VAST Tag Router – GET /api/vast endpoint for CTV & In-App Video.

This endpoint is called directly by video players and SSPs that support
VAST tag URLs (as opposed to OpenRTB programmatic).  It parses query
parameters, resolves the ad through the internal pipeline, and returns
VAST XML (2.0 – 4.2).

Example request (LG webOS / Fawesome):
    GET /api/vast?sid=125&imp=0&w=1920&h=1080&cb=9727167868012
        &ip=2603:9000:ba00:1eba::149a
        &ua=Mozilla/5.0 (Web0S; Linux/SmartTV) ...
        &app_bundle=lgiptv.fawesome-freemoviesandtvshows
        &app_name=Fawesome - Free Movies and TV Shows
        &app_store_url=https://us.lgappstv.com/main/tvapp/detail?appId=458741
        &max_dur=32&min_dur=5
        &content_type=IAB1-5&coppa=0
        &device_make=LG&device_model=50UN6950ZUF
        &dnt=0&ifa=7424c8e0-...&os=webOS TV&us_privacy=1YNN&isp=Spectrum
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from liteads.ad_server.services.ad_service import AdService
from liteads.ad_server.services.demand_forwarder import DemandForwarder
from liteads.ad_server.services.event_service import EventService
from liteads.common.config import get_settings
from liteads.common.database import get_session
from liteads.common.geoip import lookup as geoip_lookup
from liteads.common.logger import get_logger, log_context
from liteads.common.utils import generate_request_id
from liteads.common.vast import TrackingEvent, build_vast_xml, build_vast_wrapper_xml
from liteads.schemas.request import (
    AdRequest,
    AppInfo,
    DeviceInfo,
    GeoInfo,
    VideoPlacementInfo,
)

logger = get_logger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def _get_ad_service(session: AsyncSession = Depends(get_session)) -> AdService:
    """Dependency to get ad service with DB session."""
    return AdService(session)


def _get_demand_forwarder() -> DemandForwarder:
    """Dependency to get demand forwarder (uses its own DB session)."""
    return DemandForwarder()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_OS_ENV_MAP: dict[str, str] = {
    "roku": "ctv",
    "firetv": "ctv",
    "fireos": "ctv",
    "tvos": "ctv",
    "tizen": "ctv",
    "webos": "ctv",
    "webostv": "ctv",
    "vizio": "ctv",
    "androidtv": "ctv",
    "chromecast": "ctv",
    "playstation": "ctv",
    "xbox": "ctv",
    "android": "inapp",
    "ios": "inapp",
}


def _detect_env(os_str: str, ua: str = "", device_type: int | None = None) -> str:
    """Infer environment from OS string, UA, and publisher device_type."""
    key = os_str.lower().replace(" ", "")

    # 0. Publisher explicitly told us it's CTV (device_type 3=CTV, 7=STB)
    if device_type in (3, 7):
        return "ctv"

    # 1. Explicit CTV OS identifiers (check before generic 'android')
    _CTV_KEYWORDS = (
        "roku", "firetv", "fireos", "tvos", "tizen", "webos",
        "webostv", "vizio", "androidtv", "chromecast",
        "playstation", "xbox", "googletv",
    )
    for kw in _CTV_KEYWORDS:
        if kw in key:
            return "ctv"

    # 2. UA heuristics – catches GoogleTV, SmartTV, Fire TV models, etc.
    ua_lower = (ua or "").lower()
    if any(x in ua_lower for x in (
        "smarttv", "smart tv", "ctv", "roku", "tizen", "webos",
        "web0s", "firetv", "appletv", "googletv",
        "google tv", "bravia", "philipstv", "hisense",
        "vizio", "crkey",  # Chromecast
        "mitv",            # Xiaomi TV (MiTV-AYFR0, etc.)
    )):
        return "ctv"

    # 2b. Amazon Fire TV devices expose model names starting with AFT*
    #     (AFTR, AFTB, AFTGAZL, AFTN, AFTMM, AFTS, AFTK, etc.)
    import re as _re
    if _re.search(r'\bAFT[A-Z0-9]', ua or ""):
        return "ctv"

    # 3. Fall back to OS map for non-CTV
    if key in _OS_ENV_MAP:
        return _OS_ENV_MAP[key]

    return "inapp"


def _detect_ifa_type(os_str: str, make: str = "") -> str:
    """Infer IFA type from OS / make."""
    key = os_str.lower().replace(" ", "")
    if "roku" in key:
        return "rida"
    if "fire" in key or "amazon" in make.lower():
        return "afai"
    if "tvos" in key or "apple" in key:
        return "idfa"
    if "tizen" in key or "samsung" in make.lower():
        return "tifa"
    if "webos" in key or "lg" in make.lower():
        return "lgudid"
    if "android" in key:
        return "gaid"
    if "vizio" in key:
        return "vida"
    if "googletv" in key or "google tv" in key:
        return "gaid"
    if "ios" in key:
        return "idfa"
    return "unknown"


def _placement_from_params(startdelay: Optional[int] = None) -> str:
    if startdelay is not None:
        if startdelay == 0:
            return "pre_roll"
        if startdelay > 0 or startdelay == -1:
            return "mid_roll"
        if startdelay == -2:
            return "post_roll"
    return "pre_roll"


# ---------------------------------------------------------------------------
# GET /api/vast – VAST Tag endpoint
# ---------------------------------------------------------------------------

@router.get(
    "",
    summary="VAST Tag Endpoint",
    description=(
        "Returns VAST XML for CTV/In-App video players. "
        "Accepts device, app, content, and video placement parameters as query strings. "
        "Supports VAST versions 2.0 through 4.2."
    ),
    responses={
        200: {"content": {"application/xml": {}}, "description": "VAST XML document"},
    },
)
async def vast_tag(
    request: Request,
    ad_service: AdService = Depends(_get_ad_service),
    demand_forwarder: DemandForwarder = Depends(_get_demand_forwarder),
    # Slot / impression
    sid: str = Query("default", description="Slot / placement ID"),
    imp: int = Query(0, description="Impression sequence index"),
    # Video
    w: int = Query(1920, description="Video width"),
    h: int = Query(1080, description="Video height"),
    min_dur: int = Query(5, description="Minimum duration (seconds)"),
    max_dur: int = Query(30, description="Maximum duration (seconds)"),
    startdelay: Optional[int] = Query(None, description="Start delay (0=pre, >0=mid, -1=mid, -2=post)"),
    # Device
    ip: Optional[str] = Query(None, description="Client IP address"),
    uip: Optional[str] = Query(None, description="User IP address (Adtelligent-compatible alias for ip)"),
    ua: Optional[str] = Query(None, description="User-Agent"),
    ifa: Optional[str] = Query(None, description="Advertising ID"),
    dnt: Optional[int] = Query(None, description="Do Not Track flag"),
    os: Optional[str] = Query(None, alias="os", description="Device OS"),
    osv: Optional[str] = Query(None, description="OS version (e.g. 12.0)"),
    device_make: Optional[str] = Query(None, description="Device manufacturer"),
    device_model: Optional[str] = Query(None, description="Device model"),
    # App / Content
    app_bundle: Optional[str] = Query(None, description="App bundle ID"),
    app_name: Optional[str] = Query(None, description="App name"),
    app_store_url: Optional[str] = Query(None, description="App store URL"),
    content_type: Optional[str] = Query(None, description="IAB content category"),
    ct_chan: Optional[str] = Query(None, description="Content channel name"),
    ct_id: Optional[str] = Query(None, description="Content ID"),
    ct_title: Optional[str] = Query(None, description="Content title"),
    ct_ser: Optional[str] = Query(None, description="Content series"),
    ct_seas: Optional[str] = Query(None, description="Content season"),
    ct_eps: Optional[str] = Query(None, description="Content episode"),
    ct_lang: Optional[str] = Query(None, description="Content language"),
    ct_len: Optional[int] = Query(None, description="Content length (seconds)"),
    ct_live_str: Optional[int] = Query(None, description="Live stream (0/1)"),
    ct_rat: Optional[str] = Query(None, description="Content rating"),
    ct_net: Optional[str] = Query(None, description="Content network"),
    ct_genre: Optional[str] = Query(None, description="Content genre"),
    ct_prodq: Optional[str] = Query(None, description="Production quality"),
    ct_producer: Optional[str] = Query(None, description="Content producer name"),
    ct_qa_media_rating: Optional[str] = Query(None, description="QAG media rating"),
    ct_url: Optional[str] = Query(None, description="Content URL"),
    # Geo
    lat: Optional[float] = Query(None, description="Latitude"),
    lon: Optional[float] = Query(None, description="Longitude"),
    country_code: Optional[str] = Query(None, description="Country code (ISO 3166-1 alpha-2)"),
    # Privacy
    coppa: Optional[int] = Query(None, description="COPPA flag"),
    us_privacy: Optional[str] = Query(None, description="US Privacy string (CCPA)"),
    gdpr: Optional[int] = Query(None, description="GDPR applies flag (0/1)"),
    gdpr_consent: Optional[str] = Query(None, description="TCF consent string"),
    gpp: Optional[str] = Query(None, description="IAB Global Privacy Platform string"),
    gpp_sid: Optional[str] = Query(None, description="GPP section IDs (comma-separated)"),
    # Misc
    cb: Optional[str] = Query(None, description="Cache buster"),
    isp: Optional[str] = Query(None, description="ISP name"),
    app_cat: Optional[str] = Query(None, description="App IAB category"),
    device_type: Optional[int] = Query(None, description="Device type (1=mobile/tablet, 3=CTV, 7=set-top-box)"),
    # Extended device
    device_language: Optional[str] = Query(None, description="Device language (e.g. en_US)"),
    didsha1: Optional[str] = Query(None, description="Hardware device ID SHA1 hash"),
    didmd5: Optional[str] = Query(None, description="Hardware device ID MD5 hash"),
    # Extended geo
    region: Optional[str] = Query(None, description="Geo region/state code"),
    metro: Optional[str] = Query(None, description="DMA / metro code"),
    city: Optional[str] = Query(None, description="Geo city name"),
    zip_code: Optional[str] = Query(None, alias="zip", description="Postal / ZIP code"),
    geo_type: Optional[int] = Query(None, description="Geo location type (1=GPS, 2=IP, 3=User)"),
    ipservice: Optional[int] = Query(None, description="IP geolocation service (1=ip2location, 2=Neustar, 3=MaxMind)"),
    # Extended app
    app_id: Optional[str] = Query(None, description="Publisher app ID"),
    app_domain: Optional[str] = Query(None, description="App domain (e.g. verylocal.com)"),
    pub_id: Optional[str] = Query(None, description="Publisher ID"),
    app_pagecat: Optional[str] = Query(None, description="Page-level IAB categories (comma-separated)"),
    inv_partner_domain: Optional[str] = Query(None, description="Inventory partner domain"),
    # Extended content
    ct_episode: Optional[int] = Query(None, description="Content episode number"),
    ct_context: Optional[int] = Query(None, description="Content context (1=video, 2=game, 3=music, 4=app)"),
    ct_gtax: Optional[int] = Query(None, description="Content genre taxonomy ID"),
    ct_genres: Optional[str] = Query(None, description="Genre codes from taxonomy (comma-separated)"),
    # Extended video
    plcmt: Optional[int] = Query(None, description="OpenRTB 2.6 video placement type"),
    linearity: Optional[int] = Query(None, description="1=Linear, 2=Non-linear"),
    sequence: Optional[int] = Query(None, description="Sequence number in pod"),
    minbitrate: Optional[int] = Query(None, description="Minimum bitrate (kbps)"),
    maxbitrate: Optional[int] = Query(None, description="Maximum bitrate (kbps)"),
    playbackmethod: Optional[str] = Query(None, description="Playback methods (comma-separated ints)"),
    delivery: Optional[str] = Query(None, description="Delivery methods (comma-separated ints)"),
    protocols: Optional[str] = Query(None, description="VAST protocols (comma-separated ints)"),
    # Pod fields
    poddur: Optional[int] = Query(None, description="Total pod duration (seconds)"),
    maxseq: Optional[int] = Query(None, description="Max ads in the pod"),
    podid: Optional[str] = Query(None, description="Pod identifier"),
    podseq: Optional[int] = Query(None, description="Pod sequence (0=any, 1=first, -1=last)"),
    poddedupe: Optional[str] = Query(None, description="Pod deduplication signals (comma-separated ints)"),
    # Impression
    tagid: Optional[str] = Query(None, description="Publisher tag / placement ID"),
    bidfloor: Optional[float] = Query(None, description="Bid floor override (CPM)"),
    exp: Optional[int] = Query(None, description="Impression expiry (seconds)"),
    # Blocked signals
    bcat: Optional[str] = Query(None, description="Blocked IAB categories (comma-separated)"),
    badv: Optional[str] = Query(None, description="Blocked advertiser domains (comma-separated)"),
) -> Response:
    """
    Handle VAST tag GET requests from CTV/In-App video players.

    Builds an internal AdRequest from query params, runs the ad pipeline,
    and returns VAST XML with tracking events, nurl, and burl.
    """
    request_id = generate_request_id()
    settings = get_settings()

    # ── Resolve user IP: prefer `uip` (Adtelligent-compatible) over `ip` ──
    ip = uip or ip

    # ── Normalise dnt / coppa (may arrive as None from middleware) ─
    dnt = dnt if dnt is not None else 0
    coppa = coppa if coppa is not None else 0

    # ── Clean embedded IFA from UA ───────────────────────────────
    # Some publishers URL-encode the '&' between ua and ifa as %26,
    # which glues ifa=xxx into the UA value instead of a separate param.
    # Always strip it from the UA; use it as IFA only when no explicit
    # ifa param was provided.
    if ua:
        _ifa_match = re.search(r'[&;]ifa[=:]\s*([0-9a-fA-F-]{20,})', ua)
        if not _ifa_match:
            _ifa_match = re.search(r'ifa=([0-9a-fA-F-]{20,})', ua)
        if _ifa_match:
            if not ifa:
                ifa = _ifa_match.group(1)
            # Always strip the embedded ifa from the UA
            _ifa_pos = ua.find('&ifa=')
            if _ifa_pos == -1:
                _ifa_pos = ua.find(';ifa=')
            if _ifa_pos != -1:
                ua = ua[:_ifa_pos].rstrip()

    # ── Clean app_name if publisher forgot '&' before cb= ─────────
    if app_name and 'cb=' in app_name:
        app_name = app_name[:app_name.index('cb=')].rstrip()

    # Resolve OS from param or UA -----------------------------------------
    os_str = (os or "").strip()
    ua_str = (ua or "").strip()
    # Fallback: use the HTTP User-Agent header when publisher didn't send ua=
    if not ua_str:
        ua_str = (request.headers.get("user-agent") or "").strip()
    if not os_str and ua_str:
        os_str = _infer_os_from_ua(ua_str)

    env = _detect_env(os_str, ua_str, device_type)
    make = (device_make or "").strip()
    model = (device_model or "").strip()

    log_context(
        request_id=request_id,
        slot_id=sid,
        environment=env,
    )

    logger.info(
        "VAST tag request received",
        request_id=request_id,
        environment=env,
        os=os_str,
        make=make,
        model=model,
        app_bundle=app_bundle,
        app_name=app_name,
        app_id=app_id,
        ip=ip,
        ua=ua,
        ifa=ifa,
        device_type=device_type,
        country=country_code,
        ct_genre=ct_genre,
        app_cat=app_cat,
    )

    # Build internal schemas -----------------------------------------------
    # Sanitise IP: if it looks like an unresolved macro, use real client IP
    raw_ip = (ip or "").strip()
    if not raw_ip or "{" in raw_ip or "[" in raw_ip or "%7B" in raw_ip.upper():
        # Behind nginx/proxy: prefer X-Real-IP (set by nginx from $remote_addr,
        # not spoofable) over X-Forwarded-For (first entry can be forged by clients).
        _xri = request.headers.get("x-real-ip", "")
        _xff = request.headers.get("x-forwarded-for", "")
        raw_ip = (
            _xri.strip() if _xri
            else _xff.split(",")[-1].strip() if _xff
            else (request.client.host if request.client else "")
        )

    device = DeviceInfo(
        device_type="ctv" if env == "ctv" else "mobile",
        os=os_str.lower().replace(" ", "") or "unknown",
        os_version=osv or None,
        make=make or None,
        model=model or None,
        ifa=ifa,
        ifa_type=_detect_ifa_type(os_str, make),
        lmt=dnt == 1,
        ip=raw_ip,
        ua=ua_str,
        isp=isp or None,
        device_type_raw=device_type,
        language=device_language or None,
        didsha1=didsha1 or None,
        didmd5=didmd5 or None,
        screen_width=w,
        screen_height=h,
    )

    # Build GeoInfo — use publisher data if sent, otherwise enrich from IP
    _has_geo = any([
        lat is not None, lon is not None, country_code,
        region, metro, city, zip_code,
    ])
    if _has_geo:
        geo = GeoInfo(
            ip=raw_ip,
            country=(country_code or "").strip() or None,
            region=region or None,
            city=city or None,
            dma=metro or None,
            latitude=lat,
            longitude=lon,
            zip_code=zip_code or None,
            geo_type=geo_type,
            ipservice=ipservice,
        )
    else:
        # ----- GeoIP enrichment from MaxMind -----
        g = geoip_lookup(raw_ip)
        geo = GeoInfo(
            ip=raw_ip,
            country=g.country or None,
            region=g.region or None,
            city=g.city or None,
            dma=g.metro or None,
            latitude=g.lat,
            longitude=g.lon,
            zip_code=g.zip or None,
            geo_type=g.type,
            ipservice=g.ipservice,
        )

    app_info = AppInfo(
        app_name=app_name or ct_chan or None,
        app_bundle=app_bundle or None,
        store_url=app_store_url or None,
        app_category=app_cat or None,
        content_genre=ct_genre or content_type or None,
        content_rating=ct_rat or None,
        content_id=ct_id or None,
        content_title=ct_title or None,
        content_series=ct_ser or None,
        content_season=ct_seas or None,
        content_url=ct_url or None,
        content_language=ct_lang or None,
        content_livestream=ct_live_str,
        content_producer=ct_producer or None,
        production_quality=ct_prodq or None,
        qag_media_rating=ct_qa_media_rating or None,
        content_categories=app_cat or None,
        channel_name=ct_chan or None,
        network_name=ct_net or None,
        app_domain=app_domain or None,
        publisher_id=pub_id or None,
        page_categories=app_pagecat or None,
        content_episode=ct_episode or (int(ct_eps) if ct_eps and ct_eps.strip().isdigit() else None),
        content_length=ct_len,
        content_context=ct_context,
        content_gtax=ct_gtax,
        content_genres=ct_genres or None,
        inventory_partner_domain=inv_partner_domain or None,
        app_id=app_id or None,
    )

    video = VideoPlacementInfo(
        placement=_placement_from_params(startdelay),
        min_duration=min_dur,
        max_duration=max_dur,
        skip_enabled=False,
        width=w,
        height=h,
        mimes=["video/mp4"],
        startdelay_raw=startdelay,
        plcmt=plcmt,
        linearity=linearity,
        sequence=sequence,
        minbitrate=minbitrate,
        maxbitrate=maxbitrate,
        playbackmethod=playbackmethod or None,
        delivery=delivery or None,
        video_protocols=protocols or None,
        pod_duration=poddur,
        max_ads_in_pod=maxseq,
        podid=podid or None,
        podseq=podseq,
        poddedupe=poddedupe or None,
    )

    ad_request = AdRequest(
        request_id=request_id,
        slot_id=sid,
        environment=env,
        user_id=ifa,
        device=device,
        geo=geo,
        app=app_info,
        video=video,
        num_ads=1,
        geo_country=country_code or "",
        geo_region=region or None,
        geo_dma=metro or None,
        us_privacy=us_privacy or None,
        coppa=coppa,
        gdpr=gdpr,
        gdpr_consent=gdpr_consent or None,
        gpp=gpp or None,
        gpp_sid=gpp_sid or None,
        bcat=bcat or None,
        badv=badv or None,
        tagid=tagid or None,
        imp_exp=exp,
        bidfloor_override=bidfloor,
    )

    # Run pipeline ---------------------------------------------------------
    # Run local campaigns and demand forwarding in parallel.
    try:
        local_task = asyncio.ensure_future(
            ad_service.serve_ads(request=ad_request, request_id=request_id)
        )
        demand_task = asyncio.ensure_future(
            demand_forwarder.forward(ad_request=ad_request, request_id=request_id)
        )

        local_candidates, demand_candidates = await asyncio.gather(
            local_task, demand_task, return_exceptions=True,
        )

        # Handle exceptions from either task
        if isinstance(local_candidates, Exception):
            logger.exception(
                "Local pipeline error",
                request_id=request_id,
                error=str(local_candidates),
            )
            local_candidates = []
        if isinstance(demand_candidates, Exception):
            logger.warning(
                "Demand forwarding error",
                request_id=request_id,
                error=str(demand_candidates),
            )
            demand_candidates = []

        # Merge and sort by bid (highest first)
        candidates = list(local_candidates) + list(demand_candidates)
        candidates.sort(key=lambda c: c.bid, reverse=True)

        logger.info(
            "Pipeline results merged",
            request_id=request_id,
            local_count=len(local_candidates),
            demand_count=len(demand_candidates),
            total=len(candidates),
        )
    except Exception:
        logger.exception("VAST tag pipeline error", request_id=request_id)
        # Track the ad request even on pipeline failure (no-fill)
        asyncio.ensure_future(EventService.track_ad_request(None))
        return _empty_vast_response(request_id)

    # ── Track ad_requests & ad_opportunities in Redis ─────────────
    # Local campaign candidates have campaign_id > 0.
    # Demand ORTB/VAST candidates have campaign_id == 0 (tracked globally).
    local_campaign_ids = [
        c.campaign_id for c in candidates if c.campaign_id > 0
    ]
    has_demand_fill = any(c.campaign_id == 0 for c in candidates)

    # ad_requests: track per local campaign + global bucket
    tracking_ids = list(local_campaign_ids)
    if has_demand_fill or not local_campaign_ids:
        tracking_ids.append(0)  # 0 = global / demand bucket
    asyncio.ensure_future(
        EventService.track_ad_request(tracking_ids if tracking_ids else None)
    )

    # ad_opportunities: track for every campaign that produced a candidate
    opp_ids = list(local_campaign_ids)
    if has_demand_fill:
        opp_ids.append(0)  # demand fill also counts as an opportunity
    if opp_ids:
        asyncio.ensure_future(
            EventService.track_ad_opportunity(opp_ids)
        )

    if not candidates:
        logger.info("VAST tag no fill", request_id=request_id)
        return _empty_vast_response(request_id)

    # Take first candidate -------------------------------------------------
    candidate = candidates[0]
    ad_id = f"ad_{candidate.campaign_id}_{candidate.creative_id}"
    base_url = _resolve_base_url(request, settings)

    # Build tracking query params with demand source info for analytics
    _meta = candidate.metadata or {}
    _adomain_list = _meta.get("adomain") or []
    _adomain = _adomain_list[0] if _adomain_list else ""
    # Always brand source as viadsmedia.com (cross-platform ad server)
    _src = "viadsmedia.com"
    _bundle = ad_request.app.app_bundle or ""
    _country = ad_request.geo.country or ""
    _bid_price = round(candidate.bid, 4)

    # URL-safe extra params for demand analytics
    import urllib.parse as _urlparse
    _extra_params = _urlparse.urlencode({
        k: v for k, v in {
            "src": _src,
            "dom": _adomain,
            "bnd": _bundle,
            "cc": _country,
            "bp": str(_bid_price),
        }.items() if v
    })
    _tracking_suffix = f"&{_extra_params}" if _extra_params else ""

    # Build VAST tracking events
    tracking_events: list[TrackingEvent] = []
    for event_name in (
        "start", "firstQuartile", "midpoint", "thirdQuartile",
        "complete", "mute", "unmute", "pause", "resume",
        "skip", "fullscreen", "exitFullscreen",
        "close", "acceptInvitation",
    ):
        url = (
            f"{base_url}/api/v1/event/track?"
            f"type={event_name}&req={request_id}&ad={ad_id}&env={env}"
            f"{_tracking_suffix}"
        )
        tracking_events.append(TrackingEvent(event=event_name, url=url))

    impression_url = (
        f"{base_url}/api/v1/event/track?"
        f"type=impression&req={request_id}&ad={ad_id}&env={env}"
        f"{_tracking_suffix}"
    )
    error_url = (
        f"{base_url}/api/v1/event/track?"
        f"type=error&req={request_id}&ad={ad_id}&env={env}"
        f"{_tracking_suffix}"
    )

    # nurl / burl (auction price notification)
    nurl = (
        f"{base_url}/api/v1/event/win?"
        f"req={request_id}&ad={ad_id}"
        f"&price=${{AUCTION_PRICE}}&env={env}"
    )
    burl = (
        f"{base_url}/api/v1/event/billing?"
        f"req={request_id}&ad={ad_id}"
        f"&price=${{AUCTION_PRICE}}&env={env}"
    )

    # Determine VAST version (prefer latest supported)
    vast_version = (
        settings.vast.supported_versions[-1]
        if settings.vast.supported_versions
        else "4.0"
    )

    # Choose InLine vs Wrapper depending on creative type
    if candidate.metadata.get("adm"):
        # Demand ORTB bid with inline VAST XML (adm field)
        # Parse DSP VAST to extract real Ad/Creative IDs and verify media
        _adm_raw = candidate.metadata["adm"]
        _parsed = _parse_adm_vast(_adm_raw)

        if not _parsed["has_media"]:
            # No media file in DSP response – skip tracking, return no-fill
            logger.warning(
                "DSP adm has no MediaFile – skipping",
                request_id=request_id,
            )
            return _empty_vast_response(request_id)

        # Use the Ad id and Creative id from the DSP's VAST if available
        if _parsed["ad_id"]:
            ad_id = _parsed["ad_id"]
        if _parsed["creative_id"]:
            # Rebuild tracking URLs with the real creative info
            _real_crid = _parsed["creative_id"]
            ad_id = f"ad_{candidate.campaign_id}_{_real_crid}"
            tracking_events = []
            for event_name in (
                "start", "firstQuartile", "midpoint", "thirdQuartile",
                "complete", "mute", "unmute", "pause", "resume",
                "skip", "fullscreen", "exitFullscreen",
                "close", "acceptInvitation",
            ):
                url = (
                    f"{base_url}/api/v1/event/track?"
                    f"type={event_name}&req={request_id}&ad={ad_id}&env={env}"
                    f"{_tracking_suffix}"
                )
                tracking_events.append(TrackingEvent(event=event_name, url=url))
            impression_url = (
                f"{base_url}/api/v1/event/track?"
                f"type=impression&req={request_id}&ad={ad_id}&env={env}"
                f"{_tracking_suffix}"
            )
            error_url = (
                f"{base_url}/api/v1/event/track?"
                f"type=error&req={request_id}&ad={ad_id}&env={env}"
                f"{_tracking_suffix}"
            )

        # Inject our error pixel + tracking events into the DSP's VAST XML.
        # We do NOT inject <Impression> here because the DSP's adm already
        # contains its own <Impression> tag; adding a second one causes
        # CTV players to fire double impressions.
        vast_xml = _inject_tracking_into_adm(
            adm=_adm_raw,
            impression_url="",          # deliberately empty — avoid double impression
            error_url=error_url,
            tracking_events=tracking_events,
        )
        # Fire the demand partner's nurl (win notification) in background
        demand_nurl = candidate.metadata.get("nurl")
        if demand_nurl:
            asyncio.ensure_future(
                _fire_win_notice(demand_nurl, candidate.bid)
            )
    elif candidate.vast_url:
        # Wrapper – redirect player to external VAST tag.
        # No <Impression> in wrapper: the downstream VAST already has
        # its own impression pixel; including ours would double-fire.
        click_tracking_url = (
            f"{base_url}/api/v1/event/track?"
            f"type=click&req={request_id}&ad={ad_id}&env={env}"
        )
        vast_xml = build_vast_wrapper_xml(
            version=vast_version,
            ad_id=ad_id,
            creative_id=str(candidate.creative_id),
            vast_tag_uri=candidate.vast_url,
            ad_title=candidate.title or "Video Ad",
            impression_urls=[],          # no impression — avoid double-fire
            error_urls=[error_url],
            tracking_events=tracking_events,
            click_tracking=[click_tracking_url],
            nurl=nurl,
            burl=burl,
            price=round(candidate.bid, 4),
        )
    else:
        # InLine – direct video creative
        # Only serve impression/tracking when a real MediaFile exists;
        # this prevents phantom impressions on empty creatives.
        if not candidate.video_url:
            logger.warning(
                "InLine candidate has no video_url – returning no-fill",
                request_id=request_id,
                ad_id=ad_id,
            )
            return _empty_vast_response(request_id)

        vast_xml = build_vast_xml(
            version=vast_version,
            ad_id=ad_id,
            creative_id=str(candidate.creative_id),
            ad_title=candidate.title or "Video Ad",
            duration=candidate.duration or 30,
            video_url=candidate.video_url,
            video_mime=candidate.mime_type or "video/mp4",
            bitrate=candidate.bitrate or 2500,
            width=w,
            height=h,
            click_through=candidate.landing_url,
            skip_offset=candidate.skip_after if candidate.skippable else None,
            impression_urls=[impression_url],
            error_urls=[error_url],
            tracking_events=tracking_events,
            companion_image_url=candidate.companion_image_url,
            nurl=nurl,
            burl=burl,
            price=round(candidate.bid, 4),
        )

    logger.info(
        "VAST tag served",
        request_id=request_id,
        ad_id=ad_id,
        creative_id=candidate.creative_id,
        cpm=round(candidate.bid, 4),
        environment=env,
        source=candidate.metadata.get("source", "local"),
    )

    return Response(
        content=vast_xml,
        media_type="application/xml",
        headers={
            "Content-Type": "application/xml; charset=utf-8",
            "X-Request-ID": request_id,
            "X-LiteAds-Environment": env,
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
        },
    )


def _inject_tracking_into_adm(
    adm: str,
    impression_url: str,
    error_url: str,
    tracking_events: list[TrackingEvent],
) -> str:
    """
    Inject LiteAds tracking pixels into a DSP's VAST XML (adm).

    **CTV double-impression prevention:**  An ``<Impression>`` tag is only
    injected when *impression_url* is non-empty.  When the caller passes
    an empty string the DSP's own ``<Impression>`` remains the single
    source of truth, avoiding double-fire on CTV players.

    Tracking events (start, quartile, complete …) and ``<Error>`` are
    always injected so that LiteAds can still measure video engagement.
    """
    parts: list[str] = []

    # Only add our impression pixel if explicitly provided
    if impression_url:
        parts.append(
            f'<Impression><![CDATA[{impression_url}]]></Impression>'
        )
    parts.append(
        f'<Error><![CDATA[{error_url}]]></Error>'
    )

    inject_block = "\n        ".join(parts)
    inject_block = f"\n        {inject_block}"

    # Build tracking events XML block to inject into <Linear>
    if tracking_events:
        te_lines = ["<TrackingEvents>"]
        for te in tracking_events:
            te_lines.append(
                f'              <Tracking event="{te.event}"><![CDATA[{te.url}]]></Tracking>'
            )
        te_lines.append("            </TrackingEvents>")
        te_block = "\n            ".join(te_lines)
        # Inject tracking events before </Linear>
        if "</Linear>" in adm:
            adm = adm.replace("</Linear>", f"  {te_block}\n          </Linear>", 1)

    # Try to insert impression/error after <InLine> or <Wrapper> opening tag
    for tag in ("<InLine>", "<Wrapper>"):
        if tag in adm:
            return adm.replace(tag, f"{tag}{inject_block}", 1)

    # Fallback: insert after <Ad ...> tag
    ad_match = re.search(r"(<Ad[^>]*>)", adm)
    if ad_match:
        pos = ad_match.end()
        return adm[:pos] + inject_block + adm[pos:]

    # Last resort: return as-is
    return adm


def _parse_adm_vast(adm: str) -> dict:
    """Parse a DSP's VAST XML (adm) to extract Ad ID, Creative ID,
    and check for media files.

    Returns:
        dict with keys:
            ad_id: str or None - the <Ad id="..."> value
            creative_id: str or None - the <Creative id="..."> value
            has_media: bool - True if <MediaFile> is present
    """
    result = {"ad_id": None, "creative_id": None, "has_media": False}

    # Extract <Ad id="...">
    ad_match = re.search(r'<Ad[^>]+id=["\']([^"\']+)["\']', adm)
    if ad_match:
        result["ad_id"] = ad_match.group(1)

    # Extract <Creative id="..."> (first one found)
    crid_match = re.search(r'<Creative[^>]+id=["\']([^"\']+)["\']', adm)
    if crid_match:
        result["creative_id"] = crid_match.group(1)

    # Check for <MediaFile> presence (any tag containing media content)
    result["has_media"] = bool(
        re.search(r'<MediaFile[\s>]', adm)
        or re.search(r'<VASTAdTagURI', adm)  # Wrapper pointing to media
    )

    return result


async def _fire_win_notice(nurl: str, price: float) -> None:
    """
    Fire the demand partner's win notification URL in the background.

    Replaces ``${AUCTION_PRICE}`` macro with the actual clearing price.
    """
    try:
        from liteads.ad_server.services.demand_forwarder import _get_http_client

        resolved_url = nurl.replace("${AUCTION_PRICE}", str(round(price, 4)))
        client = _get_http_client()
        await client.get(resolved_url, timeout=2.0)
    except Exception as exc:
        logger.warning("Win notice failed: %s", str(exc))


def _infer_os_from_ua(ua: str) -> str:
    """Try to infer OS from User-Agent string."""
    ua_lower = ua.lower()
    if "roku" in ua_lower:
        return "Roku"
    if "web0s" in ua_lower or "webos" in ua_lower:
        return "webOS TV"
    if "tizen" in ua_lower:
        return "Tizen"
    if "firetv" in ua_lower or "aftb" in ua_lower:
        return "Fire OS"
    if "appletv" in ua_lower or "apple tv" in ua_lower:
        return "tvOS"
    if "crkey" in ua_lower:
        return "Chromecast"
    if "smarttv" in ua_lower:
        return "Smart TV"
    if "android" in ua_lower:
        return "Android"
    if "iphone" in ua_lower or "ipad" in ua_lower:
        return "iOS"
    return ""


def _resolve_base_url(request: Request, settings: Any) -> str:
    """Determine the public-facing base URL for tracking pixels.

    Priority order:
    1. ``settings.vast.tracking_base_url`` (explicit config override)
    2. ``X-Forwarded-Host`` + ``X-Forwarded-Proto`` headers (nginx proxy)
    3. ``Host`` header (direct or proxied with ``proxy_set_header Host``)
    4. ``request.base_url`` (fallback)

    This ensures tracking URLs always use the external domain rather than
    ``localhost`` or the internal container hostname.
    """
    # 1. Explicit config
    configured = getattr(settings, "vast", None)
    if configured and getattr(configured, "tracking_base_url", ""):
        return configured.tracking_base_url.rstrip("/")

    # 2. X-Forwarded-* from reverse proxy
    fwd_host = request.headers.get("x-forwarded-host")
    fwd_proto = request.headers.get("x-forwarded-proto", "http")
    if fwd_host:
        return f"{fwd_proto}://{fwd_host}".rstrip("/")

    # 3. Host header (nginx sets this via proxy_set_header Host $host)
    host_header = request.headers.get("host", "")
    if host_header and "localhost" not in host_header:
        scheme = request.url.scheme or "http"
        return f"{scheme}://{host_header}".rstrip("/")

    # 4. Fallback to request.base_url
    return str(request.base_url).rstrip("/")


def _empty_vast_response(request_id: str = "") -> Response:
    """Return an empty VAST document (no fill).

    Per VAST spec, return HTTP 200 with an empty VAST element — not 204.
    This is critical for SSP/exchange compatibility (Magnite, Xandr, etc.).
    """
    empty_vast = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<VAST version="4.0"/>'
    )
    return Response(
        content=empty_vast,
        media_type="application/xml",
        status_code=200,
        headers={
            "Content-Type": "application/xml; charset=utf-8",
            "X-Request-ID": request_id,
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Access-Control-Allow-Origin": "*",
        },
    )


# ===========================================================================
# Publisher Tag Builder
# ===========================================================================

class TagBuilderRequest(BaseModel):
    """Request body for generating a VAST tag URL for a publisher to embed."""

    base_url: str = Field(
        ...,
        description="Server base URL (e.g. https://ads.example.com)",
        json_schema_extra={"example": "https://ads.example.com"},
    )
    slot_id: str = Field(
        "default", description="Ad slot / zone identifier"
    )
    environment: str = Field(
        "ctv", description="Target environment: ctv | inapp"
    )
    width: int = Field(1920, description="Video player width")
    height: int = Field(1080, description="Video player height")
    min_duration: int = Field(5, description="Minimum ad duration (s)")
    max_duration: int = Field(30, description="Maximum ad duration (s)")
    app_bundle: str | None = Field(None, description="App bundle ID (required for CTV/InApp)")
    app_name: str | None = Field(None, description="App name (required for CTV/InApp)")
    app_store_url: str | None = Field(None, description="App store URL (required for CTV/InApp per Adtelligent)")
    coppa: int = Field(0, description="COPPA flag (0/1)")
    gdpr: int | None = Field(None, description="GDPR applies (0/1)")
    us_privacy: str | None = Field(None, description="US Privacy / CCPA string")

    # These will be replaced by the video player at runtime
    include_device_macros: bool = Field(
        True,
        description="Include player-replaceable macros for UIP, UA, IFA, DNT, etc.",
    )


class TagBuilderResponse(BaseModel):
    """Generated VAST tag URL and embed instructions."""

    vast_tag_url: str = Field(..., description="Complete VAST tag URL to embed")
    macro_note: str = Field(
        "",
        description="Note about runtime macros that the player must replace",
    )
    example_curl: str = Field("", description="Example cURL command for testing")
    html_embed: str = Field("", description="HTML snippet for IMA SDK integration")


@router.post(
    "/tag-builder",
    response_model=TagBuilderResponse,
    summary="Generate VAST tag URL for publishers",
    description=(
        "Generates a ready-to-use VAST tag URL with the correct query parameters "
        "for a publisher's CTV or in-app video player. Returns the URL, an "
        "example cURL, and an HTML/IMA-SDK embed snippet."
    ),
)
async def build_publisher_tag(body: TagBuilderRequest) -> TagBuilderResponse:
    """Build a VAST tag URL that a publisher can embed in their video player."""
    base = body.base_url.rstrip("/")

    params: dict[str, Any] = {
        "sid": body.slot_id,
        "w": body.width,
        "h": body.height,
        "min_dur": body.min_duration,
        "max_dur": body.max_duration,
        "coppa": body.coppa,
    }

    if body.app_bundle:
        params["app_bundle"] = body.app_bundle
    if body.app_name:
        params["app_name"] = body.app_name
    if body.app_store_url:
        params["app_store_url"] = body.app_store_url
    if body.gdpr is not None:
        params["gdpr"] = body.gdpr
    if body.us_privacy:
        params["us_privacy"] = body.us_privacy

    # Add cache buster macro (most players replace [CACHEBUSTER] at runtime)
    params["cb"] = "[CACHEBUSTER]"

    macro_note = ""
    if body.include_device_macros:
        # Standard macros that video players / SDKs replace at runtime.
        # Uses Adtelligent-standard `uip` for user IP (not legacy `ip`).
        params["uip"] = "[UIP]"
        params["ua"] = "[UA]"
        params["ifa"] = "[IFA]"
        params["dnt"] = "[DNT]"
        params["os"] = "[OS]"
        params["device_make"] = "[MAKE]"
        params["device_model"] = "[MODEL]"
        macro_note = (
            "Replace [UIP], [UA], [IFA], [DNT], [OS], [MAKE], [MODEL], "
            "and [CACHEBUSTER] with actual runtime values. "
            "Most SSAI / IMA SDK / PAL implementations handle this automatically."
        )

    tag_url = f"{base}/api/vast?{urlencode(params, safe='[]')}"

    # Example cURL (with macros resolved to sample values)
    sample = tag_url.replace("[CACHEBUSTER]", "123456789")
    sample = sample.replace("[UIP]", "203.0.113.42")
    sample = sample.replace("[UA]", "Mozilla/5.0")
    sample = sample.replace("[IFA]", "00000000-0000-0000-0000-000000000000")
    sample = sample.replace("[DNT]", "0")
    sample = sample.replace("[OS]", "Roku")
    sample = sample.replace("[MAKE]", "Roku")
    sample = sample.replace("[MODEL]", "Ultra")

    html_embed = (
        '<script src="https://imasdk.googleapis.com/js/sdkloader/ima3.js"></script>\n'
        "<script>\n"
        "  var adsRequest = new google.ima.AdsRequest();\n"
        f'  adsRequest.adTagUrl = "{tag_url}";\n'
        "  adsLoader.requestAds(adsRequest);\n"
        "</script>"
    )

    return TagBuilderResponse(
        vast_tag_url=tag_url,
        macro_note=macro_note,
        example_curl=f'curl -s "{sample}"',
        html_embed=html_embed,
    )
