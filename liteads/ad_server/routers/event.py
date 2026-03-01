"""
Video event tracking and win/loss notification endpoints.

Supports:
- VAST 2.x-4.x video event tracking (impression, start, quartiles, complete, skip, etc.)
- nurl (win notification URL) with ${AUCTION_PRICE} macro
- burl (billing notification URL) for post-auction billing
- lurl (loss notification URL) with ${AUCTION_LOSS} macro
- Pixel (GET) – returns 1x1 transparent GIF (SSP/player compatible)
- JSON (POST) – returns structured JSON response

Compatible with: Magnite, Xandr, OpenX, Freewheel, GAM, Unruly, SmartHub,
Adtelligent, Project Limelight, DoubleVerify, and other exchanges.
"""

from __future__ import annotations

import base64

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from liteads.ad_server.services.event_service import EventService
from liteads.common.database import get_session
from liteads.common.logger import get_logger, log_context
from liteads.common.utils import current_timestamp
from liteads.schemas.request import EventRequest
from liteads.schemas.response import EventResponse

logger = get_logger(__name__)
router = APIRouter()

# 1x1 transparent GIF pixel (43 bytes) – industry standard for pixel tracking
_PIXEL_GIF = base64.b64decode(
    "R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
)

_PIXEL_HEADERS = {
    "Content-Type": "image/gif",
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
    "Access-Control-Allow-Origin": "*",
}


def _pixel_response() -> Response:
    """Return a 1x1 transparent GIF pixel.

    This is the standard response format for VAST tracking beacons.
    Video players and SSPs expect either this or HTTP 204.
    """
    return Response(
        content=_PIXEL_GIF,
        status_code=200,
        media_type="image/gif",
        headers=_PIXEL_HEADERS,
    )


def _parse_price(raw: str | None) -> float:
    """Parse an auction price string, handling unresolved macros."""
    if not raw or raw == "${AUCTION_PRICE}":
        return 0.0
    try:
        return float(raw)
    except ValueError:
        return 0.0


def get_event_service(session: AsyncSession = Depends(get_session)) -> EventService:
    """Dependency to get event service."""
    return EventService(session)


@router.post("/track", response_model=EventResponse)
async def track_event(
    event: EventRequest,
    event_service: EventService = Depends(get_event_service),
) -> EventResponse:
    """
    Track a VAST video ad event (POST).

    Supported event types (VAST 2.x-4.x):
    - impression: Ad served and rendered
    - start: Video playback started
    - firstQuartile: 25% of video viewed
    - midpoint: 50% of video viewed
    - thirdQuartile: 75% of video viewed
    - complete: 100% of video viewed
    - click: Click-through on ad
    - skip: User skipped the ad
    - mute/unmute/pause/resume/fullscreen: Player interaction events
    - error: Ad playback error

    Events are used for CPM billing, fill rate optimization, and training data.
    """
    log_context(
        request_id=event.request_id,
        ad_id=event.ad_id,
        event_type=event.event_type,
    )

    logger.info("Video event received", environment=event.environment)

    success = await event_service.track_event(
        request_id=event.request_id,
        ad_id=event.ad_id,
        event_type=event.event_type,
        user_id=event.user_id,
        timestamp=event.timestamp or current_timestamp(),
        environment=event.environment,
        video_position=event.video_position,
        extra=event.extra,
    )

    return EventResponse(
        success=success,
        message="Event recorded" if success else "Failed to record event",
    )


@router.get("/track")
async def track_event_get(
    request: Request,
    type: str = Query(..., alias="type", description="VAST event type"),
    req: str = Query(..., description="Request ID"),
    ad: str = Query(..., description="Ad ID"),
    env: str = Query("ctv", description="Environment (ctv/inapp)"),
    pos: int | None = Query(None, description="Video position in seconds"),
    err: str | None = Query(None, description="VAST error code ([ERRORCODE] macro)"),
    x_forwarded_for: str | None = Header(None, alias="X-Forwarded-For"),
    event_service: EventService = Depends(get_event_service),
) -> Response:
    """
    Track video event via GET request (pixel/beacon tracking).

    Used for VAST tracking URLs embedded in VAST XML.
    Returns a 1x1 transparent GIF pixel (industry standard).
    Supports all VAST 2.x-4.x event types and the [ERRORCODE] macro.
    """
    log_context(
        request_id=req,
        ad_id=ad,
        event_type=type,
    )

    logger.info("Pixel video event received", environment=env, error_code=err)

    extra = None
    if err and err != "[ERRORCODE]":
        extra = {"error_code": err}

    client_ip = (
        x_forwarded_for.split(",")[0].strip() if x_forwarded_for else None
    ) or (request.client.host if request.client else None)

    await event_service.track_event(
        request_id=req,
        ad_id=ad,
        event_type=type,
        user_id=None,
        timestamp=current_timestamp(),
        environment=env,
        video_position=pos,
        extra=extra,
        ip_address=client_ip,
    )

    # Return 1x1 pixel — SSPs and video players expect an image response
    return _pixel_response()


@router.get("/win")
async def win_notification(
    request: Request,
    req: str = Query(..., description="Request ID"),
    ad: str = Query(..., description="Ad ID"),
    price: str = Query("0", description="Auction clearing price (CPM)"),
    env: str = Query("ctv", description="Environment (ctv/inapp)"),
    x_forwarded_for: str | None = Header(None, alias="X-Forwarded-For"),
    event_service: EventService = Depends(get_event_service),
) -> Response:
    """
    Win notification endpoint (nurl).

    Called by the SSP/exchange when this bid wins the auction.
    The ${AUCTION_PRICE} macro in the nurl is replaced with
    the actual clearing price before calling this endpoint.

    This is the standard OpenRTB 2.6 win notification mechanism.
    Returns a 1x1 pixel (exchanges expect image or 204).
    """
    log_context(request_id=req, ad_id=ad)

    # Parse clearing price — handle unresolved macro
    clearing_price = _parse_price(price)

    logger.info(
        "Win notification received (nurl)",
        clearing_price=clearing_price,
        environment=env,
    )

    # Record as WIN event with auction clearing price.
    # NOTE: nurl = win notification, NOT impression billing.
    # Impressions are tracked via burl (billing notification) or VAST pixel.
    client_ip = (
        x_forwarded_for.split(",")[0].strip() if x_forwarded_for else None
    ) or (request.client.host if request.client else None)

    await event_service.track_event(
        request_id=req,
        ad_id=ad,
        event_type="win",
        user_id=None,
        timestamp=current_timestamp(),
        environment=env,
        video_position=None,
        extra={"clearing_price": clearing_price, "source": "nurl"},
        ip_address=client_ip,
        win_price=clearing_price,
    )

    return _pixel_response()


@router.get("/loss")
async def loss_notification(
    request: Request,
    req: str = Query(..., description="Request ID"),
    ad: str = Query(..., description="Ad ID"),
    reason: str = Query("0", description="Loss reason code (OpenRTB Table 5.25)"),
    price: str = Query("0", description="Clearing price that won (CPM)"),
    env: str = Query("ctv", description="Environment (ctv/inapp)"),
    x_forwarded_for: str | None = Header(None, alias="X-Forwarded-For"),
    event_service: EventService = Depends(get_event_service),
) -> Response:
    """
    Loss notification endpoint (lurl).

    Called by the SSP/exchange when this bid loses the auction.
    The ${AUCTION_LOSS} macro is replaced with the loss reason code.
    The ${AUCTION_PRICE} macro is replaced with the winning price.

    OpenRTB 2.6 Table 5.25 loss reason codes:
      0 = Bid Won, 1 = Internal Error, 2 = Impression Opportunity Expired,
      3 = Invalid Bid Response, 4 = Invalid Deal ID, 5 = Invalid Auction ID,
      100 = Bid was Below Auction Floor, 101 = Bid was Below Deal Floor,
      102 = Lost to Higher Bid, 103 = Lost to a Bid for a PMP Deal,
      104 = Buyer Seat Blocked, 200 = Creative Filtered (General),
      201 = Pending Processing, 202 = Disapproved, 203 = Blocked by Publisher,
      204 = Quality, 205 = Category, 206 = Attribute, 207 = Adomain
    """
    log_context(request_id=req, ad_id=ad)

    loss_reason = 0
    if reason and reason != "${AUCTION_LOSS}":
        try:
            loss_reason = int(reason)
        except ValueError:
            loss_reason = 0

    clearing_price = _parse_price(price)

    logger.info(
        "Loss notification received (lurl)",
        loss_reason=loss_reason,
        clearing_price=clearing_price,
        environment=env,
    )

    client_ip = (
        x_forwarded_for.split(",")[0].strip() if x_forwarded_for else None
    ) or (request.client.host if request.client else None)

    await event_service.track_event(
        request_id=req,
        ad_id=ad,
        event_type="loss",
        user_id=None,
        timestamp=current_timestamp(),
        environment=env,
        video_position=None,
        extra={
            "loss_reason": loss_reason,
            "clearing_price": clearing_price,
            "source": "lurl",
        },
        ip_address=client_ip,
    )

    return _pixel_response()


@router.get("/billing")
async def billing_notification(
    request: Request,
    req: str = Query(..., description="Request ID"),
    ad: str = Query(..., description="Ad ID"),
    price: str = Query("0", description="Billable price (CPM)"),
    env: str = Query("ctv", description="Environment (ctv/inapp)"),
    x_forwarded_for: str | None = Header(None, alias="X-Forwarded-For"),
    event_service: EventService = Depends(get_event_service),
) -> Response:
    """
    Billing notification endpoint (burl).

    Called when the ad is actually rendered/billed.
    This is the OpenRTB 2.6 billing notification — confirms the ad
    was rendered and billing should occur at the specified price.
    Returns a 1x1 pixel (exchanges expect image or 204).
    """
    log_context(request_id=req, ad_id=ad)

    billing_price = _parse_price(price)

    logger.info(
        "Billing notification received (burl)",
        billing_price=billing_price,
        environment=env,
    )

    client_ip = (
        x_forwarded_for.split(",")[0].strip() if x_forwarded_for else None
    ) or (request.client.host if request.client else None)

    await event_service.track_event(
        request_id=req,
        ad_id=ad,
        event_type="impression",
        user_id=None,
        timestamp=current_timestamp(),
        environment=env,
        video_position=None,
        extra={"billing_price": billing_price, "source": "burl"},
        ip_address=client_ip,
        win_price=billing_price,
    )

    return _pixel_response()
