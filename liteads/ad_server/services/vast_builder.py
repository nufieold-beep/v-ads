"""
VAST XML builder helper – shared by OpenRTB and VAST-tag pipelines.

Eliminates the duplicated Wrapper-vs-InLine selection logic that previously
existed independently in ``openrtb_service.py`` and ``vast_tag.py``.
"""

from __future__ import annotations

from typing import Optional

from liteads.common.tracking import build_click_tracking_url
from liteads.common.vast import TrackingEvent, build_vast_xml, build_vast_wrapper_xml
from liteads.schemas.internal import AdCandidate


def build_vast_for_candidate(
    candidate: AdCandidate,
    *,
    vast_version: str,
    ad_id: str,
    tracking_events: list[TrackingEvent],
    impression_url: str,
    error_url: str,
    base_url: str,
    request_id: str,
    env: str,
    width: int = 1920,
    height: int = 1080,
    nurl: Optional[str] = None,
    burl: Optional[str] = None,
) -> Optional[str]:
    """Build VAST XML (InLine or Wrapper) for a single ad candidate.

    **Creative routing:**

    * ``candidate.vast_url`` is set → emit a VAST **Wrapper** that redirects
      the player to the external VAST tag.  No ``<Impression>`` is included
      to prevent double-fire when the downstream VAST fires its own.
    * ``candidate.video_url`` is set → emit a VAST **InLine** with a
      ``<MediaFile>`` and full impression/tracking pixels.
    * Neither is set → return ``None`` so the caller can skip or return
      a no-fill response.

    Parameters
    ----------
    candidate : AdCandidate
        The winning ad candidate from the pipeline.
    vast_version : str
        VAST version string, e.g. ``"4.0"``.
    ad_id : str
        Tracking identifier, e.g. ``"ad_1_42"``.
    tracking_events : list[TrackingEvent]
        Pre-built video tracking events (start, quartile, complete, …).
    impression_url : str
        Impression pixel URL (used for InLine only).
    error_url : str
        VAST ``<Error>`` pixel URL.
    base_url : str
        Server origin used to construct the click-tracking URL.
    request_id : str
        Request identifier for click-tracking URL construction.
    env : str
        Environment string, ``"ctv"`` or ``"inapp"``.
    width : int
        Video player width in pixels (defaults to 1920).
    height : int
        Video player height in pixels (defaults to 1080).
    nurl : str or None
        Win-notification URL embedded in the VAST ``<Pricing>`` block
        (VAST-tag flow only; OpenRTB carries this in the Bid object).
    burl : str or None
        Billing-notification URL (VAST-tag flow only).

    Returns
    -------
    str or None
        VAST XML string, or ``None`` when the candidate has no media.
    """
    if candidate.vast_url:
        # Wrapper – external VAST tag (demand/DSP).
        # Omit <Impression> to avoid double-fire with the downstream VAST.
        click_tracking_url = build_click_tracking_url(
            base_url, request_id, ad_id, env,
        )
        return build_vast_wrapper_xml(
            version=vast_version,
            ad_id=ad_id,
            creative_id=str(candidate.creative_id),
            vast_tag_uri=candidate.vast_url,
            ad_title=candidate.title or "Video Ad",
            impression_urls=[],  # no impression — avoid double-fire
            error_urls=[error_url],
            tracking_events=tracking_events,
            click_tracking=[click_tracking_url],
            nurl=nurl,
            burl=burl,
            price=round(candidate.bid, 4),
        )

    if candidate.video_url:
        # InLine – direct video creative (MediaFile present).
        return build_vast_xml(
            version=vast_version,
            ad_id=ad_id,
            creative_id=str(candidate.creative_id),
            ad_title=candidate.title or "Video Ad",
            duration=candidate.duration or 30,
            video_url=candidate.video_url,
            video_mime=candidate.mime_type or "video/mp4",
            bitrate=candidate.bitrate or 2500,
            width=width,
            height=height,
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

    # No media — caller should skip this candidate or return no-fill.
    return None
