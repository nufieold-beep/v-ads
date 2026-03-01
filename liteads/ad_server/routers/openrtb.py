"""
OpenRTB 2.6 Router – CPM CTV & In-App Video Only.

Endpoints:
    POST /api/v1/openrtb/bid   – Receive OpenRTB 2.6 bid request, return bid response
    POST /api/v1/openrtb/nobid – Explicit no-bid notification (optional)

Compatible with: Magnite, Xandr, OpenX, Freewheel, GAM, Unruly,
SmartHub, Adtelligent, Project Limelight, DoubleVerify, and other
programmatic CTV/in-app video exchanges.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Header, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from liteads.ad_server.services.ad_service import AdService
from liteads.ad_server.services.event_service import EventService
from liteads.ad_server.services.openrtb_service import OpenRTBService
from liteads.ad_server.middleware.metrics import record_no_bid
from liteads.common.database import get_session
from liteads.common.logger import get_logger
from liteads.schemas.openrtb import BidRequest, BidResponse, NoBidReason

logger = get_logger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# OpenRTB 2.6 standard response headers
# ---------------------------------------------------------------------------
_OPENRTB_VERSION = "2.6"

_OPENRTB_RESPONSE_HEADERS: dict[str, str] = {
    "Content-Type": "application/json; charset=utf-8",
    "X-OpenRTB-Version": _OPENRTB_VERSION,
    "Connection": "keep-alive",
}


def _get_openrtb_service(session: AsyncSession = Depends(get_session)) -> OpenRTBService:
    """Dependency to get OpenRTB service with DB session."""
    return OpenRTBService(ad_service=AdService(session))


@router.post(
    "/bid",
    response_model=BidResponse,
    summary="OpenRTB 2.6 Bid Request",
    description=(
        "Accepts an OpenRTB 2.6 bid request for CTV or in-app video inventory "
        "and returns a bid response with VAST XML markup, nurl, burl, and lurl. "
        "Returns HTTP 204 when no bid is available. "
        "Compatible with Magnite, Xandr, OpenX, Freewheel, GAM, Unruly, "
        "SmartHub, Adtelligent, Project Limelight, and other exchanges."
    ),
    responses={
        200: {"content": {"application/json": {}}, "description": "Bid response with VAST XML"},
        204: {"description": "No bid available"},
        400: {"description": "Invalid bid request"},
    },
)
async def openrtb_bid(
    bid_request: BidRequest,
    request: Request,
    openrtb_service: OpenRTBService = Depends(_get_openrtb_service),
    x_openrtb_version: str | None = Header(None, alias="X-OpenRTB-Version"),
    x_forwarded_for: str | None = Header(None, alias="X-Forwarded-For"),
    user_agent: str | None = Header(None, alias="User-Agent"),
) -> Response:
    """
    Process an OpenRTB 2.6 bid request.

    **Required request headers:**
    - ``Content-Type: application/json``
    - ``Accept: application/json``
    - ``X-OpenRTB-Version: 2.6``
    - ``User-Agent: <buyer-platform-ua>``
    - ``X-Forwarded-For: <end-user-ip>`` (for geo/fraud)
    - ``Connection: keep-alive``

    The request must contain at least one impression (``imp``) with a ``video``
    object.  Device type 3 (CTV) or 7 (Set-Top Box) routes to the CTV pipeline;
    all other device types route to the in-app pipeline.

    **Win notification flow:**
    1. SSP receives this response with ``nurl``, ``burl``, and ``lurl`` in each bid
    2. On auction win → SSP calls ``nurl`` replacing ``${AUCTION_PRICE}``
    3. On confirmed render → SSP calls ``burl`` replacing ``${AUCTION_PRICE}``
    4. On auction loss → SSP calls ``lurl`` replacing ``${AUCTION_LOSS}``

    **Macros supported:**
    - ``${AUCTION_PRICE}`` – Clearing price (in nurl/burl)
    - ``${AUCTION_LOSS}`` – Loss reason code (in lurl)
    - ``[ERRORCODE]`` – VAST error code (in VAST Error elements)

    **VAST tracking:**
    The ``adm`` field contains VAST XML (InLine or Wrapper) with embedded
    tracking events for start, quartiles, complete, skip, mute/unmute,
    pause/resume, close, fullscreen, etc.
    """
    start_time = time.monotonic()

    # ---- Log & enrich from HTTP headers ----
    client_ip = (
        x_forwarded_for.split(",")[0].strip() if x_forwarded_for else None
    ) or (request.client.host if request.client else None)

    logger.info(
        "OpenRTB bid request received",
        request_id=bid_request.id,
        openrtb_version=x_openrtb_version,
        client_ip=client_ip,
        user_agent=user_agent,
    )

    # Enrich device IP from X-Forwarded-For when not provided in payload
    if client_ip and bid_request.device and not bid_request.device.ip:
        bid_request.device.ip = client_ip
    # Enrich device UA from User-Agent header when not provided
    if user_agent and bid_request.device and not bid_request.device.ua:
        bid_request.device.ua = user_agent

    # Validate that we have at least one video impression
    has_video = any(imp.video is not None for imp in bid_request.imp)
    if not has_video:
        logger.warning(
            "OpenRTB request has no video impression",
            request_id=bid_request.id,
        )
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=BidResponse(
                id=bid_request.id,
                nbr=NoBidReason.INVALID_REQUEST,
            ).model_dump(exclude_none=True),
            headers=_OPENRTB_RESPONSE_HEADERS,
        )

    # Check tmax — if we've already exceeded the allowed response time,
    # return no-bid immediately to avoid exchange timeouts
    tmax_ms = bid_request.tmax or 200
    elapsed_ms = (time.monotonic() - start_time) * 1000
    if elapsed_ms > tmax_ms * 0.9:  # 90% of tmax as safety margin
        logger.warning(
            "OpenRTB tmax exceeded before processing",
            request_id=bid_request.id,
            tmax=tmax_ms,
            elapsed_ms=round(elapsed_ms, 2),
        )
        return Response(
            status_code=status.HTTP_204_NO_CONTENT,
            headers={"X-OpenRTB-Version": _OPENRTB_VERSION},
        )

    # Process bid request through pipeline
    bid_response = await openrtb_service.process_bid_request(bid_request)

    # ---- Track ad request / opportunity metrics ----
    if bid_response and bid_response.seatbid:
        # Extract campaign IDs from winning bids
        filled_campaign_ids: list[int] = []
        for sb in bid_response.seatbid:
            for b in sb.bid:
                try:
                    filled_campaign_ids.append(int(b.cid))  # type: ignore[arg-type]
                except (ValueError, TypeError):
                    pass
        await EventService.track_ad_request(filled_campaign_ids or None)
        await EventService.track_ad_opportunity(filled_campaign_ids)
    else:
        await EventService.track_ad_request(None)

    # No-bid → HTTP 204 (per OpenRTB spec)
    if bid_response is None or not bid_response.seatbid:
        record_no_bid("no_fill")
        return Response(
            status_code=status.HTTP_204_NO_CONTENT,
            headers={"X-OpenRTB-Version": _OPENRTB_VERSION},
        )

    processing_ms = (time.monotonic() - start_time) * 1000

    logger.info(
        "OpenRTB bid response",
        request_id=bid_request.id,
        environment=bid_request.environment,
        num_bids=sum(len(sb.bid) for sb in bid_response.seatbid),
        processing_ms=round(processing_ms, 2),
    )

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content=bid_response.model_dump(exclude_none=True),
        headers={
            **_OPENRTB_RESPONSE_HEADERS,
            "X-Processing-Time": f"{processing_ms:.2f}ms",
        },
    )


@router.post(
    "/nobid",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Explicit No-Bid Notification",
    description="Optional endpoint for SSPs to notify of no-bid explicitly.",
)
async def openrtb_nobid(
    bid_request: BidRequest,
    x_openrtb_version: str | None = Header(None, alias="X-OpenRTB-Version"),
) -> Response:
    """
    Accept no-bid notification.

    Some SSPs send explicit no-bid notifications for analytics purposes.
    This endpoint logs the event and returns 204.
    """
    logger.info(
        "OpenRTB no-bid notification received",
        request_id=bid_request.id,
        environment=bid_request.environment,
    )
    return Response(
        status_code=status.HTTP_204_NO_CONTENT,
        headers={"X-OpenRTB-Version": _OPENRTB_VERSION},
    )
