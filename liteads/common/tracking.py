"""
Shared tracking-URL construction for VAST events, nurl / burl / lurl.

Consolidates URL templates that were previously duplicated across
ad.py, vast_tag.py, and openrtb_service.py (3-4 copies each).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from liteads.common.vast import TrackingEvent


# ---------------------------------------------------------------------------
# Standard VAST event names  (VAST 2.0 – 4.x)
# ---------------------------------------------------------------------------

VAST_EVENT_NAMES: tuple[str, ...] = (
    "start", "firstQuartile", "midpoint", "thirdQuartile",
    "complete", "mute", "unmute", "pause", "resume",
    "skip", "fullscreen", "exitFullscreen",
    "close", "acceptInvitation",
)


# ---------------------------------------------------------------------------
# Tracking-event URL builder
# ---------------------------------------------------------------------------

def build_tracking_event_url(
    base_url: str,
    event_type: str,
    request_id: str,
    ad_id: str,
    env: str,
    extra_params: str = "",
) -> str:
    """Build a single VAST tracking-event pixel URL.

    Parameters
    ----------
    base_url : str
        Server origin, e.g. ``"https://ads.example.com"``.
    event_type : str
        VAST event name (``"impression"``, ``"start"``, …).
    request_id, ad_id, env : str
        Standard identifiers embedded in the query string.
    extra_params : str
        Optional pre-encoded ``"&key=val&…"`` suffix for demand analytics.
    """
    return (
        f"{base_url}/api/v1/event/track?"
        f"type={event_type}&req={request_id}&ad={ad_id}&env={env}"
        f"{extra_params}"
    )


def build_tracking_events(
    base_url: str,
    request_id: str,
    ad_id: str,
    env: str,
    extra_params: str = "",
) -> list[TrackingEvent]:
    """Build the full list of VAST video tracking events.

    Returns a list of :class:`TrackingEvent` objects ready for
    ``build_vast_xml()`` / ``build_vast_wrapper_xml()``.
    """
    return [
        TrackingEvent(
            event=name,
            url=build_tracking_event_url(
                base_url, name, request_id, ad_id, env, extra_params,
            ),
        )
        for name in VAST_EVENT_NAMES
    ]


def build_impression_url(
    base_url: str, request_id: str, ad_id: str, env: str,
    extra_params: str = "",
) -> str:
    """Build the impression pixel URL."""
    return build_tracking_event_url(
        base_url, "impression", request_id, ad_id, env, extra_params,
    )


def build_error_url(
    base_url: str, request_id: str, ad_id: str, env: str,
    extra_params: str = "",
) -> str:
    """Build the VAST error pixel URL."""
    return build_tracking_event_url(
        base_url, "error", request_id, ad_id, env, extra_params,
    )


# ---------------------------------------------------------------------------
# nurl / burl / lurl builders
# ---------------------------------------------------------------------------

def build_nurl(base_url: str, request_id: str, ad_id: str, env: str) -> str:
    """Win notification URL with ``${AUCTION_PRICE}`` macro."""
    return (
        f"{base_url}/api/v1/event/win?"
        f"req={request_id}&ad={ad_id}"
        f"&price=${{AUCTION_PRICE}}&env={env}"
    )


def build_burl(base_url: str, request_id: str, ad_id: str, env: str) -> str:
    """Billing notification URL with ``${AUCTION_PRICE}`` macro."""
    return (
        f"{base_url}/api/v1/event/billing?"
        f"req={request_id}&ad={ad_id}"
        f"&price=${{AUCTION_PRICE}}&env={env}"
    )


def build_lurl(base_url: str, request_id: str, ad_id: str, env: str) -> str:
    """Loss notification URL with ``${AUCTION_LOSS}`` + ``${AUCTION_PRICE}`` macros."""
    return (
        f"{base_url}/api/v1/event/loss?"
        f"req={request_id}&ad={ad_id}"
        f"&price=${{AUCTION_PRICE}}&loss=${{AUCTION_LOSS}}&env={env}"
    )


# ---------------------------------------------------------------------------
# Click-tracking URL
# ---------------------------------------------------------------------------

def build_click_tracking_url(
    base_url: str, request_id: str, ad_id: str, env: str,
    extra_params: str = "",
) -> str:
    """Build VAST click-tracking pixel URL."""
    return build_tracking_event_url(
        base_url, "click", request_id, ad_id, env, extra_params,
    )


# ---------------------------------------------------------------------------
# Ad ID builder
# ---------------------------------------------------------------------------

def build_ad_id(campaign_id: int, creative_id: int) -> str:
    """Build the standard ad identifier used in tracking URLs and VAST XML.

    Format: ``"ad_{campaign_id}_{creative_id}"``

    Previously duplicated across ad.py, openrtb_service.py, and vast_tag.py.
    """
    return f"ad_{campaign_id}_{creative_id}"


# ---------------------------------------------------------------------------
# Demand-analytics suffix builder
# ---------------------------------------------------------------------------

def build_demand_extra_params(
    *,
    source: str | None = None,
    adomain: str | None = None,
    bundle: str | None = None,
    country: str | None = None,
    bid_price: float | None = None,
) -> str:
    """Build a URL-encoded suffix for demand-analytics tracking params.

    Returns ``""`` when no params are present, or ``"&key=val&…"`` otherwise.
    """
    raw: dict[str, str] = {}
    if source:
        raw["src"] = source
    if adomain:
        raw["dom"] = adomain
    if bundle:
        raw["bnd"] = bundle
    if country:
        raw["cc"] = country
    if bid_price is not None:
        raw["bp"] = str(bid_price)
    if not raw:
        return ""
    return "&" + urlencode(raw)


# ---------------------------------------------------------------------------
# Empty VAST response helper
# ---------------------------------------------------------------------------

_EMPTY_VAST_XML = '<?xml version="1.0" encoding="UTF-8"?>\n<VAST version="4.0"/>'

_VAST_RESPONSE_HEADERS = {
    "Content-Type": "application/xml; charset=utf-8",
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
}


def empty_vast_xml() -> str:
    """Return a minimal empty VAST 4.0 document (no-fill)."""
    return _EMPTY_VAST_XML


def empty_vast_headers(request_id: str = "") -> dict[str, str]:
    """Return standard response headers for a VAST XML response."""
    headers = dict(_VAST_RESPONSE_HEADERS)
    if request_id:
        headers["X-Request-ID"] = request_id
    return headers
