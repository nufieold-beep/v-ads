"""
Video ad serving endpoints for CPM CTV and In-App.

Supports VAST 2.x-4.x tracking URLs and OpenRTB 2.6 compatible responses.
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from liteads.ad_server.services.ad_service import AdService
from liteads.common.cache import CacheKeys, redis_client
from liteads.common.config import get_settings
from liteads.common.database import get_session
from liteads.common.logger import get_logger, log_context
from liteads.common.utils import generate_request_id, json_dumps, json_loads
from liteads.schemas.request import AdRequest
from liteads.schemas.response import (
    AdListResponse,
    AdResponse,
    VideoCreativeResponse,
    VideoTrackingUrls,
)

logger = get_logger(__name__)
router = APIRouter()


def get_ad_service(session: AsyncSession = Depends(get_session)) -> AdService:
    """Dependency to get ad service."""
    return AdService(session)


def _build_tracking_urls(
    base_url: str,
    request_id: str,
    ad_id: str,
    environment: str,
) -> VideoTrackingUrls:
    """Build VAST-standard video tracking URLs.

    Compatible with VAST 2.0 through 4.x event tracking.
    """
    def _url(event_type: str) -> str:
        return (
            f"{base_url}/api/v1/event/track"
            f"?type={event_type}&req={request_id}&ad={ad_id}&env={environment}"
        )

    return VideoTrackingUrls(
        impression_url=_url("impression"),
        start_url=_url("start"),
        first_quartile_url=_url("firstQuartile"),
        midpoint_url=_url("midpoint"),
        third_quartile_url=_url("thirdQuartile"),
        complete_url=_url("complete"),
        click_url=_url("click"),
        skip_url=_url("skip"),
        mute_url=_url("mute"),
        unmute_url=_url("unmute"),
        pause_url=_url("pause"),
        resume_url=_url("resume"),
        error_url=_url("error"),
    )


def _get_creative_type_name(creative_type: int) -> str:
    """Convert creative type enum to string."""
    types = {1: "ctv_video", 2: "inapp_video"}
    return types.get(creative_type, "ctv_video")


@router.post("/request", response_model=AdListResponse)
async def request_ads(
    request: Request,
    ad_request: AdRequest,
    ad_service: AdService = Depends(get_ad_service),
) -> AdListResponse:
    """
    Request video ads for CTV or In-App environment.

    Pipeline:
    1. Retrieves candidate video ads based on CTV/In-App targeting
    2. Filters by budget, frequency, video quality
    3. Predicts fill rate / VTR using optimization models
    4. Ranks by CPM with VTR weighting
    5. Returns video ads with VAST 2.x-4.x tracking URLs

    Supports nurl/burl auction price notification via tracking URLs.
    """
    request_id = generate_request_id()
    settings = get_settings()

    log_context(
        request_id=request_id,
        slot_id=ad_request.slot_id,
        user_id=ad_request.user_id,
        environment=ad_request.environment,
    )

    logger.info(
        "Video ad request received",
        num_requested=ad_request.num_ads,
        environment=ad_request.environment,
        device_os=ad_request.device.os if ad_request.device else None,
        device_type=ad_request.device.device_type if ad_request.device else None,
    )

    # Get client IP
    client_ip = request.client.host if request.client else None
    if ad_request.geo and not ad_request.geo.ip:
        ad_request.geo.ip = client_ip

    # Serve ads
    candidates = await ad_service.serve_ads(
        request=ad_request,
        request_id=request_id,
    )

    # Build response
    ads = []
    base_url = str(request.base_url).rstrip("/")

    for candidate in candidates[: ad_request.num_ads]:
        ad_id = f"ad_{candidate.campaign_id}_{candidate.creative_id}"

        # Build VAST tracking URLs
        tracking = _build_tracking_urls(
            base_url=base_url,
            request_id=request_id,
            ad_id=ad_id,
            environment=ad_request.environment,
        )

        # Build video creative response
        creative = VideoCreativeResponse(
            title=candidate.title,
            description=candidate.description,
            video_url=candidate.video_url,
            vast_url=candidate.vast_url,
            companion_image_url=candidate.companion_image_url,
            landing_url=candidate.landing_url,
            duration=candidate.duration,
            width=candidate.width,
            height=candidate.height,
            bitrate=candidate.bitrate,
            mime_type=candidate.mime_type,
            creative_type=_get_creative_type_name(candidate.creative_type),
            skippable=candidate.skippable,
            skip_after=candidate.skip_after,
        )

        # Auction price = CPM bid (nurl/burl compatible)
        # The ${AUCTION_PRICE} macro is replaced with actual clearing price
        nurl = (
            f"{base_url}/api/v1/event/win"
            f"?req={request_id}&ad={ad_id}"
            f"&price=${{AUCTION_PRICE}}&env={ad_request.environment}"
        )

        ad = AdResponse(
            ad_id=ad_id,
            campaign_id=candidate.campaign_id,
            creative_id=candidate.creative_id,
            creative=creative,
            tracking=tracking,
            environment=ad_request.environment,
            cpm=round(candidate.bid, 4),
            metadata={
                "ecpm": round(candidate.ecpm, 4),
                "pvtr": round(candidate.pvtr, 6),
                "pctr": round(candidate.pctr, 6),
                "nurl": nurl,
                "burl": f"{base_url}/api/v1/event/billing?req={request_id}&ad={ad_id}&price=${{AUCTION_PRICE}}",
            }
            if settings.debug
            else {
                "nurl": nurl,
            },
        )
        ads.append(ad)

    # ── Cache each served candidate for the VAST endpoint ─────
    # When the player later calls /vast/{request_id}/{ad_id} we
    # need the original demand URLs (video_url, vast_url, etc.)
    # so they can be returned unchanged.  TTL = 1 hour.
    for candidate in candidates[: ad_request.num_ads]:
        _ad_id = f"ad_{candidate.campaign_id}_{candidate.creative_id}"
        cache_key = CacheKeys.vast_candidate(request_id, _ad_id)
        payload = json_dumps({
            "video_url": candidate.video_url,
            "vast_url": candidate.vast_url,
            "mime_type": candidate.mime_type,
            "width": candidate.width,
            "height": candidate.height,
            "duration": candidate.duration,
            "title": candidate.title,
            "landing_url": candidate.landing_url,
            "skippable": candidate.skippable,
            "skip_after": candidate.skip_after,
            "bitrate": candidate.bitrate,
            "companion_image_url": candidate.companion_image_url,
            "bid": candidate.bid,
        })
        try:
            await redis_client.set(cache_key, payload, ttl=3600)
        except Exception:
            pass  # Best-effort; VAST endpoint will still work with empty response

    logger.info(
        "Video ad request completed",
        num_returned=len(ads),
        environment=ad_request.environment,
    )

    return AdListResponse(
        request_id=request_id,
        ads=ads,
        count=len(ads),
        environment=ad_request.environment,
    )


@router.get("/vast/{request_id}/{ad_id}")
async def get_vast_xml(
    request: Request,
    request_id: str,
    ad_id: str,
    env: str = "ctv",
    v: str = "4.0",
) -> Response:
    """
    Get VAST XML for a video ad.

    Retrieves the original demand creative data that was cached at
    serve time and returns VAST XML with the **original URLs unchanged**.
    No hardcoded or rewritten URLs — demand URLs pass through as-is.

    Supports VAST versions 2.0, 3.0, and 4.x.

    Args:
        request_id: Original request ID
        ad_id: Ad identifier
        env: Environment (ctv/inapp)
        v: VAST version (2.0, 3.0, 4.0, 4.1, 4.2)
    """
    base_url = str(request.base_url).rstrip("/")

    # Parse ad_id
    parts = ad_id.split("_")
    creative_id = parts[2] if len(parts) >= 3 else "0"

    tracking = _build_tracking_urls(base_url, request_id, ad_id, env)

    # ── Retrieve original demand creative from cache ──────────
    # The /request endpoint caches the full candidate data (including
    # the demand's original video_url / vast_url) at serve time.
    # We retrieve it here so that demand URLs are never rewritten.
    cache_key = CacheKeys.vast_candidate(request_id, ad_id)
    cached_raw = await redis_client.get(cache_key)

    if not cached_raw:
        # No cached candidate — return empty VAST (no fill)
        logger.warning(
            "No cached candidate for VAST request",
            request_id=request_id,
            ad_id=ad_id,
        )
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
            },
        )

    cand = json_loads(cached_raw)

    # Use the demand's original URLs without any modification
    video_url = cand.get("video_url", "")
    vast_url = cand.get("vast_url")
    video_mime = cand.get("mime_type", "video/mp4")
    video_width = cand.get("width", 1920)
    video_height = cand.get("height", 1080)
    duration = cand.get("duration") or 30
    landing_url = cand.get("landing_url", "")
    title = cand.get("title") or f"Video Ad {ad_id}"
    skippable = cand.get("skippable", True)
    skip_after = cand.get("skip_after", 5)

    h, remainder = divmod(duration, 3600)
    m, s = divmod(remainder, 60)
    duration_str = f"{h:02d}:{m:02d}:{s:02d}"

    # Generate VAST XML (version-aware)
    vast_version = v if v in ("2.0", "3.0", "4.0", "4.1", "4.2") else "4.0"

    # If the demand provided a vast_url, build a VAST Wrapper that
    # redirects the player to the demand's original VAST tag while
    # injecting our tracking pixels.  Otherwise build InLine VAST
    # with the demand's original video_url.
    if vast_url:
        vast_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<VAST version="{vast_version}">
  <Ad id="{ad_id}">
    <Wrapper>
      <AdSystem>LiteAds</AdSystem>
      <VASTAdTagURI><![CDATA[{vast_url}]]></VASTAdTagURI>
      <Impression><![CDATA[{tracking.impression_url}]]></Impression>
      <Creatives>
        <Creative>
          <Linear>
            <TrackingEvents>
              <Tracking event="start"><![CDATA[{tracking.start_url}]]></Tracking>
              <Tracking event="firstQuartile"><![CDATA[{tracking.first_quartile_url}]]></Tracking>
              <Tracking event="midpoint"><![CDATA[{tracking.midpoint_url}]]></Tracking>
              <Tracking event="thirdQuartile"><![CDATA[{tracking.third_quartile_url}]]></Tracking>
              <Tracking event="complete"><![CDATA[{tracking.complete_url}]]></Tracking>
              <Tracking event="skip"><![CDATA[{tracking.skip_url}]]></Tracking>
            </TrackingEvents>
          </Linear>
        </Creative>
      </Creatives>
      <Error><![CDATA[{tracking.error_url}]]></Error>
    </Wrapper>
  </Ad>
</VAST>"""
    else:
        skip_offset_attr = f' skipoffset="00:00:{skip_after:02d}"' if skippable else ""
        vast_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<VAST version="{vast_version}">
  <Ad id="{ad_id}">
    <InLine>
      <AdSystem>LiteAds</AdSystem>
      <AdTitle>{title}</AdTitle>
      <Impression><![CDATA[{tracking.impression_url}]]></Impression>
      <Creatives>
        <Creative id="{creative_id}">
          <Linear{skip_offset_attr}>
            <Duration>{duration_str}</Duration>
            <TrackingEvents>
              <Tracking event="start"><![CDATA[{tracking.start_url}]]></Tracking>
              <Tracking event="firstQuartile"><![CDATA[{tracking.first_quartile_url}]]></Tracking>
              <Tracking event="midpoint"><![CDATA[{tracking.midpoint_url}]]></Tracking>
              <Tracking event="thirdQuartile"><![CDATA[{tracking.third_quartile_url}]]></Tracking>
              <Tracking event="complete"><![CDATA[{tracking.complete_url}]]></Tracking>
              <Tracking event="skip"><![CDATA[{tracking.skip_url}]]></Tracking>
              <Tracking event="mute"><![CDATA[{tracking.mute_url}]]></Tracking>
              <Tracking event="unmute"><![CDATA[{tracking.unmute_url}]]></Tracking>
              <Tracking event="pause"><![CDATA[{tracking.pause_url}]]></Tracking>
              <Tracking event="resume"><![CDATA[{tracking.resume_url}]]></Tracking>
            </TrackingEvents>
            <VideoClicks>
              <ClickThrough><![CDATA[{landing_url}]]></ClickThrough>
            </VideoClicks>
            <MediaFiles>
              <MediaFile delivery="progressive" type="{video_mime}" width="{video_width}" height="{video_height}">
                <![CDATA[{video_url}]]>
              </MediaFile>
            </MediaFiles>
          </Linear>
        </Creative>
      </Creatives>
      <Error><![CDATA[{tracking.error_url}]]></Error>
    </InLine>
  </Ad>
</VAST>"""

    return Response(
        content=vast_xml,
        media_type="application/xml",
        headers={
            "Content-Type": "application/xml; charset=utf-8",
            "X-Request-ID": request_id,
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        },
    )
