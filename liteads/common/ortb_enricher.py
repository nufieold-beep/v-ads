"""
OpenRTB 2.6 Bid-Request Auto-Enrichment.

When a publisher sends an incomplete ORTB bid request, this module fills
essential missing fields with IAB-compliant defaults so that downstream DSPs
always receive a well-formed request.

Keeps the payload clean and slim — only fills truly missing essential fields.
Does NOT add verbose optional fields (schain, user.ext, regs.ext, etc.)
that bloat the payload and reduce DSP response rates.

Usage:
    from liteads.common.ortb_enricher import enrich_bid_request
    enriched = enrich_bid_request(bid_request)   # mutates & returns
"""

from __future__ import annotations

import uuid
from typing import Any, Optional

from liteads.common.logger import get_logger

logger = get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Default values matching the canonical schema
# ─────────────────────────────────────────────────────────────────────────────

_DEFAULTS_VIDEO = {
    "mimes": ["video/mp4"],
    "protocols": [2, 3, 4, 5, 6, 7, 8],
    "w": 1920,
    "h": 1080,
    "plcmt": 1,
    "placement": 1,
    "linearity": 1,
    "startdelay": 0,
    "minduration": 3,
    "maxduration": 30,
    "playbackmethod": [1],
    "delivery": [2],
}

_DEFAULTS_DEVICE = {
    "devicetype": 3,
    "lmt": 0,
    "dnt": 0,
    "language": "en",
}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _set_default(obj: Any, attr: str, default: Any) -> bool:
    """Set *attr* on a Pydantic model if it is ``None`` / empty.

    Returns ``True`` when a default was applied.
    """
    current = getattr(obj, attr, None)
    if current is None or (isinstance(current, (list, str)) and not current):
        setattr(obj, attr, default)
        return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def enrich_bid_request(
    br: Any,
    *,
    client_ip: Optional[str] = None,
    user_agent: Optional[str] = None,
    slot_id: Optional[str] = None,
) -> Any:
    """Enrich an OpenRTB ``BidRequest`` in-place and return it.

    Only fills truly essential missing fields:
    - Request ID, at, tmax
    - Imp basics (id, tagid, bidfloor)
    - Video defaults (mimes, protocols, dimensions)
    - Device IP/UA from headers
    - Geo from MaxMind
    - Basic app/publisher fallbacks

    Does NOT add: schain, user.ext, regs.ext, content defaults, etc.
    """
    enriched_fields: list[str] = []

    # ── 1. Top-level BidRequest fields ────────────────────────────────
    if not br.id:
        br.id = f"REQ-{uuid.uuid4().hex[:12]}"
        enriched_fields.append("id")

    if _set_default(br, "at", 1):
        enriched_fields.append("at")

    if _set_default(br, "tmax", 500):
        enriched_fields.append("tmax")

    # ── 2. Impression + Video ───────────────────────────────────
    # Detect CTV early so we can set instl correctly
    _is_ctv = False
    if br.device and br.device.devicetype in (3, 7):
        _is_ctv = True
    for imp in br.imp:
        if not imp.id:
            imp.id = "1"
            enriched_fields.append("imp.id")

        # NOTE: tagid intentionally NOT set — it leaks internal slot IDs
        # and the target (good) SSP format does not include tagid.

        if imp.bidfloor is None:
            imp.bidfloor = 0.01  # minimal floor — let DSP decide
            enriched_fields.append("imp.bidfloor")

        # secure (HTTPS) — always 1 for video
        if imp.secure is None:
            imp.secure = 1
            enriched_fields.append("imp.secure")

        # instl (full-screen) — CTV is always full-screen, in-app depends
        if imp.instl is None and _is_ctv:
            imp.instl = 1
            enriched_fields.append("imp.instl")

        # -- Video --
        if imp.video is None:
            from liteads.schemas.openrtb import Video as OrtbVideo
            imp.video = OrtbVideo(**_DEFAULTS_VIDEO)
            enriched_fields.append("imp.video (created)")
        else:
            v = imp.video
            for key, default in _DEFAULTS_VIDEO.items():
                current = getattr(v, key, None)
                if current is None or (isinstance(current, list) and not current):
                    setattr(v, key, default)
                    enriched_fields.append(f"imp.video.{key}")

    # ── 3. Device ─────────────────────────────────────────────────────
    if br.device is None:
        from liteads.schemas.openrtb import Device as OrtbDevice
        br.device = OrtbDevice(
            ip=client_ip or "",
            ua=user_agent or "",
            **_DEFAULTS_DEVICE,
        )
        enriched_fields.append("device (created)")
    else:
        dev = br.device
        for key, default in _DEFAULTS_DEVICE.items():
            if _set_default(dev, key, default):
                enriched_fields.append(f"device.{key}")

        # Fill IP from header if missing
        if not dev.ip and client_ip:
            dev.ip = client_ip
            enriched_fields.append("device.ip")

        # Fill UA from header if missing
        if not dev.ua and user_agent:
            dev.ua = user_agent
            enriched_fields.append("device.ua")

        # connectiontype: CTV → Ethernet (1), others → WiFi (2)
        if dev.connectiontype is None:
            env = "ctv" if dev.devicetype in (3, 7) else "inapp"
            dev.connectiontype = 1 if env == "ctv" else 2
            enriched_fields.append("device.connectiontype")

    # ── 4. Geo auto-enrichment from MaxMind ───────────────────────────
    _enrich_geo(br, enriched_fields)

    # ── 5. App / Publisher ────────────────────────────────────────────
    if br.app is None:
        from liteads.schemas.openrtb import App as OrtbApp, Publisher as OrtbPublisher
        br.app = OrtbApp(
            id=slot_id or "APP_ID",
            name="APP_NAME",
            bundle="com.example.ctv",
            publisher=OrtbPublisher(id=slot_id or "PUB_ID"),
        )
        enriched_fields.append("app (created)")
    else:
        app = br.app
        _set_default(app, "id", slot_id or "APP_ID")
        _set_default(app, "name", "APP_NAME")
        _set_default(app, "bundle", "com.example.ctv")

        # Publisher
        if app.publisher is None:
            from liteads.schemas.openrtb import Publisher as OrtbPublisher
            app.publisher = OrtbPublisher(id=slot_id or "PUB_ID")
            enriched_fields.append("app.publisher (created)")

    # ── 6. Source — only fill fd/tid if completely missing ─────────────
    if br.source is None:
        from liteads.schemas.openrtb import Source as OrtbSource
        br.source = OrtbSource(
            fd=1,
            tid=br.id,
        )
        enriched_fields.append("source (created)")

    # ── 7. Regs — only fill coppa if completely missing ───────────────
    if br.regs is None:
        from liteads.schemas.openrtb import Regs as OrtbRegs
        br.regs = OrtbRegs(coppa=0)
        enriched_fields.append("regs (created)")

    # Log summary
    if enriched_fields:
        logger.info(
            "ORTB request auto-enriched",
            request_id=br.id,
            enriched_count=len(enriched_fields),
            enriched_fields=enriched_fields,
        )

    return br


# ─────────────────────────────────────────────────────────────────────────────
# Private helpers
# ─────────────────────────────────────────────────────────────────────────────

def _enrich_geo(br: Any, enriched_fields: list[str]) -> None:
    """Auto-attach ``device.geo`` via MaxMind when the publisher omits it."""
    if br.device is None or not br.device.ip:
        return

    geo = br.device.geo
    # If geo already has a country, skip
    if geo is not None and geo.country:
        return

    try:
        from liteads.common.geoip import lookup as geoip_lookup
        from liteads.schemas.openrtb import Geo as OrtbGeo

        g = geoip_lookup(br.device.ip)
        if g and g.country:
            br.device.geo = OrtbGeo(
                country=g.country,
                region=g.region,
                city=g.city,
                metro=g.metro,
                lat=g.lat,
                lon=g.lon,
                zip=getattr(g, "zip", None),
                type=g.type,         # 2 = IP-based
                ipservice=g.ipservice,  # 3 = MaxMind
            )
            enriched_fields.append("device.geo (MaxMind)")
    except Exception as exc:
        logger.debug("GeoIP enrichment skipped", error=str(exc))
