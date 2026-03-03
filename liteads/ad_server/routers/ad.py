"""
Video ad serving endpoints for CPM CTV and In-App.

Supports VAST 2.x-4.x tracking URLs and OpenRTB 2.6 compatible responses.
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from liteads.ad_server.services.ad_service import AdService
from liteads.common.config import get_settings
from liteads.common.database import get_session
from liteads.common.logger import get_logger, log_context
from liteads.common.tracking import (
    build_burl,
    build_nurl,
    empty_vast_headers,
    empty_vast_xml,
)
from liteads.common.utils import generate_request_id
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

    Delegates to ``common.tracking`` for the URL template so the pattern
    lives in one place.
    """
    from liteads.common.tracking import build_tracking_event_url

    def _url(event_type: str) -> str:
        return build_tracking_event_url(
            base_url, event_type, request_id, ad_id, environment,
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
        nurl = build_nurl(base_url, request_id, ad_id, ad_request.environment)
        burl = build_burl(base_url, request_id, ad_id, ad_request.environment)

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
                "burl": burl,
            }
            if settings.debug
            else {
                "nurl": nurl,
            },
        )
        ads.append(ad)

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

    CTV-optimised: no cookies, no cache lookups.
    Impressions / tracking events are only emitted when the VAST
    actually contains a ``<MediaFile>`` so that double-impression
    counting cannot occur.

    Returns empty ``<VAST/>`` (no-fill) because the ad-candidate
    cache has been removed for CTV reliability.
    """
    logger.info(
        "VAST endpoint called (no-cache mode)",
        request_id=request_id,
        ad_id=ad_id,
    )

    return Response(
        content=empty_vast_xml(),
        media_type="application/xml",
        status_code=200,
        headers=empty_vast_headers(request_id),
    )
