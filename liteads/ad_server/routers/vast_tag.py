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

from typing import Any, Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from liteads.ad_server.services.ad_service import AdService
from liteads.common.config import get_settings
from liteads.common.database import get_session
from liteads.common.logger import get_logger, log_context
from liteads.common.utils import generate_request_id
from liteads.common.vast import TrackingEvent, build_vast_xml, build_vast_wrapper_xml
from liteads.schemas.request import (
    AdRequest,
    AppInfo,
    DeviceInfo,
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


def _detect_env(os_str: str, ua: str = "") -> str:
    """Infer environment from OS string and UA."""
    key = os_str.lower().replace(" ", "").replace("tv", "tv")
    # check known CTV OS
    for pattern, env in _OS_ENV_MAP.items():
        if pattern in key:
            return env
    # UA heuristics
    ua_lower = (ua or "").lower()
    if any(x in ua_lower for x in (
        "smarttv", "ctv", "roku", "tizen", "webos", "web0s",
        "firetv", "appletv", "aftb",
    )):
        return "ctv"
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
    ua: Optional[str] = Query(None, description="User-Agent"),
    ifa: Optional[str] = Query(None, description="Advertising ID"),
    dnt: int = Query(0, description="Do Not Track flag"),
    os: Optional[str] = Query(None, alias="os", description="Device OS"),
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
    # Geo
    lat: Optional[float] = Query(None, description="Latitude"),
    lon: Optional[float] = Query(None, description="Longitude"),
    # Privacy
    coppa: int = Query(0, description="COPPA flag"),
    us_privacy: Optional[str] = Query(None, description="US Privacy string (CCPA)"),
    gdpr: Optional[int] = Query(None, description="GDPR applies flag (0/1)"),
    gdpr_consent: Optional[str] = Query(None, description="TCF consent string"),
    gpp: Optional[str] = Query(None, description="IAB Global Privacy Platform string"),
    gpp_sid: Optional[str] = Query(None, description="GPP section IDs (comma-separated)"),
    # Misc
    cb: Optional[str] = Query(None, description="Cache buster"),
    isp: Optional[str] = Query(None, description="ISP name"),
) -> Response:
    """
    Handle VAST tag GET requests from CTV/In-App video players.

    Builds an internal AdRequest from query params, runs the ad pipeline,
    and returns VAST XML with tracking events, nurl, and burl.
    """
    request_id = generate_request_id()
    settings = get_settings()

    # Resolve OS from param or UA -----------------------------------------
    os_str = (os or "").strip()
    ua_str = (ua or "").strip()
    if not os_str and ua_str:
        os_str = _infer_os_from_ua(ua_str)

    env = _detect_env(os_str, ua_str)
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
        ip=ip,
    )

    # Build internal schemas -----------------------------------------------
    device = DeviceInfo(
        device_type="ctv" if env == "ctv" else "mobile",
        os=os_str.lower().replace(" ", "") or "unknown",
        os_version="",
        make=make,
        model=model,
        ifa=ifa,
        ifa_type=_detect_ifa_type(os_str, make),
        lmt=dnt == 1,
        ip=ip or (request.client.host if request.client else None),
        ua=ua_str,
    )

    app_info = AppInfo(
        app_name=app_name or ct_chan or "",
        app_bundle=app_bundle or "",
        store_url=app_store_url or "",
        content_genre=content_type or "",
        content_rating=ct_rat or "",
        content_id=ct_id or "",
        network=ct_net or "",
    )

    video = VideoPlacementInfo(
        placement=_placement_from_params(startdelay),
        min_duration=min_dur,
        max_duration=max_dur,
        skip_enabled=False,
        width=w,
        height=h,
        mimes=["video/mp4"],
    )

    ad_request = AdRequest(
        request_id=request_id,
        slot_id=sid,
        environment=env,
        user_id=ifa,
        device=device,
        app=app_info,
        video=video,
        num_ads=1,
    )

    # Run pipeline ---------------------------------------------------------
    try:
        candidates = await ad_service.serve_ads(
            request=ad_request,
            request_id=request_id,
        )
    except Exception:
        logger.exception("VAST tag pipeline error", request_id=request_id)
        return _empty_vast_response(request_id)

    if not candidates:
        logger.info("VAST tag no fill", request_id=request_id)
        return _empty_vast_response(request_id)

    # Take first candidate -------------------------------------------------
    candidate = candidates[0]
    ad_id = f"ad_{candidate.campaign_id}_{candidate.creative_id}"
    base_url = str(request.base_url).rstrip("/")

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
        )
        tracking_events.append(TrackingEvent(event=event_name, url=url))

    impression_url = (
        f"{base_url}/api/v1/event/track?"
        f"type=impression&req={request_id}&ad={ad_id}&env={env}"
    )
    error_url = (
        f"{base_url}/api/v1/event/track?"
        f"type=error&req={request_id}&ad={ad_id}&env={env}"
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
    if candidate.vast_url:
        # Wrapper – redirect player to external VAST tag with our tracking
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
            impression_urls=[impression_url],
            error_urls=[error_url],
            tracking_events=tracking_events,
            click_tracking=[click_tracking_url],
            nurl=nurl,
            burl=burl,
            price=round(candidate.bid, 4),
        )
    else:
        # InLine – direct video creative
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
    app_bundle: str | None = Field(None, description="App bundle ID")
    app_name: str | None = Field(None, description="App name")
    coppa: int = Field(0, description="COPPA flag (0/1)")
    gdpr: int | None = Field(None, description="GDPR applies (0/1)")
    us_privacy: str | None = Field(None, description="US Privacy / CCPA string")

    # These will be replaced by the video player at runtime
    include_device_macros: bool = Field(
        True,
        description="Include player-replaceable macros for IP, UA, IFA, DNT, etc.",
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
    if body.gdpr is not None:
        params["gdpr"] = body.gdpr
    if body.us_privacy:
        params["us_privacy"] = body.us_privacy

    # Add cache buster macro (most players replace [CACHEBUSTER] at runtime)
    params["cb"] = "[CACHEBUSTER]"

    macro_note = ""
    if body.include_device_macros:
        # Standard macros that video players / SDKs replace at runtime
        params["ip"] = "[IP]"
        params["ua"] = "[UA]"
        params["ifa"] = "[IFA]"
        params["dnt"] = "[DNT]"
        params["os"] = "[OS]"
        params["device_make"] = "[MAKE]"
        params["device_model"] = "[MODEL]"
        macro_note = (
            "Replace [IP], [UA], [IFA], [DNT], [OS], [MAKE], [MODEL], "
            "and [CACHEBUSTER] with actual runtime values. "
            "Most SSAI / IMA SDK / PAL implementations handle this automatically."
        )

    tag_url = f"{base}/api/vast?{urlencode(params, safe='[]')}"

    # Example cURL (with macros resolved to sample values)
    sample = tag_url.replace("[CACHEBUSTER]", "123456789")
    sample = sample.replace("[IP]", "203.0.113.42")
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
