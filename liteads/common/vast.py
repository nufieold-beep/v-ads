"""
VAST XML Generator – Versions 2.0 through 4.2.

Generates standards-compliant VAST (Video Ad Serving Template) XML markup
for CTV and in-app video ad delivery.

References:
  - VAST 2.0: https://www.iab.com/guidelines/vast/
  - VAST 3.0: https://www.iab.com/guidelines/vast/
  - VAST 4.0 / 4.1 / 4.2: https://iabtechlab.com/standards/vast/
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
from xml.etree.ElementTree import Element, SubElement, tostring


# ---------------------------------------------------------------------------
# Data models for VAST generation
# ---------------------------------------------------------------------------

class VASTVersion(str, Enum):
    V2_0 = "2.0"
    V3_0 = "3.0"
    V4_0 = "4.0"
    V4_1 = "4.1"
    V4_2 = "4.2"


@dataclass
class TrackingEvent:
    """A single VAST tracking event."""
    event: str          # e.g. "start", "firstQuartile", "midpoint", "thirdQuartile", "complete"
    url: str


@dataclass
class CompanionAd:
    """Companion banner alongside video."""
    width: int
    height: int
    static_resource: str                # Image URL
    resource_type: str = "image/png"    # MIME type of the companion
    click_through: Optional[str] = None
    tracking_events: list[TrackingEvent] = field(default_factory=list)


@dataclass
class MediaFile:
    """A single video media file."""
    url: str
    delivery: str = "progressive"       # "progressive" | "streaming"
    type: str = "video/mp4"             # MIME type
    bitrate: int = 2500                 # kbps
    width: int = 1920
    height: int = 1080
    codec: Optional[str] = None
    scalable: bool = True
    maintain_aspect_ratio: bool = True


@dataclass
class VASTCreative:
    """All data needed to build one <Creative> / <Linear> element."""

    ad_id: str
    creative_id: str
    ad_title: str = "Video Ad"
    description: str = ""
    advertiser: str = ""
    duration: int = 30                  # seconds
    skip_offset: Optional[int] = None   # seconds (None = non-skippable)
    click_through: Optional[str] = None
    click_tracking: list[str] = field(default_factory=list)

    # Impression / error / survey URLs
    impression_urls: list[str] = field(default_factory=list)
    error_urls: list[str] = field(default_factory=list)
    survey_url: Optional[str] = None

    # nurl / burl
    nurl: Optional[str] = None
    burl: Optional[str] = None

    # Media
    media_files: list[MediaFile] = field(default_factory=list)
    companion_ads: list[CompanionAd] = field(default_factory=list)

    # Tracking pixels
    tracking_events: list[TrackingEvent] = field(default_factory=list)

    # VAST 4.x extensions
    ad_serving_id: Optional[str] = None
    category: Optional[str] = None
    viewable_impression: Optional[str] = None   # Viewable impression URL
    not_viewable_url: Optional[str] = None
    view_undetermined_url: Optional[str] = None

    # Ad verification (DoubleVerify, IAS, MOAT, etc.)
    verification_vendors: list[dict] = field(default_factory=list)
    # Each entry: {"vendor": "doubleverify.com", "js_url": "...", "params": "..."}

    # Pricing (VAST 4.x)
    price: Optional[float] = None
    price_model: str = "cpm"
    price_currency: str = "USD"


# ---------------------------------------------------------------------------
# VAST XML Builder
# ---------------------------------------------------------------------------

def _format_duration(seconds: int) -> str:
    """Format seconds as HH:MM:SS."""
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def _cdata(text: str) -> str:
    """Wrap text in CDATA."""
    return f"<![CDATA[{text}]]>"


def _add_cdata_element(parent: Element, tag: str, text: str) -> Element:
    """Add a child element whose text content is CDATA-wrapped."""
    el = SubElement(parent, tag)
    # ElementTree doesn't support CDATA natively; we'll post-process
    el.text = f"__CDATA__{text}__ENDCDATA__"
    return el


class VASTBuilder:
    """
    Build VAST XML documents (versions 2.0 – 4.2).

    Usage::

        builder = VASTBuilder(version="4.0")
        xml_str = builder.build(creative)
    """

    def __init__(self, version: str = "4.0"):
        self.version = VASTVersion(version)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build(self, creative: VASTCreative) -> str:
        """Generate the full VAST XML string."""
        root = self._build_tree(creative)
        raw_xml = tostring(root, encoding="unicode", xml_declaration=False)

        # Post-process CDATA markers
        raw_xml = raw_xml.replace("__CDATA__", "<![CDATA[").replace("__ENDCDATA__", "]]>")

        # Unescape HTML entities that ElementTree may have introduced inside CDATA
        # (ElementTree escapes & < > etc.)
        # This is safe because our CDATA content is URLs which we control
        return f'<?xml version="1.0" encoding="UTF-8"?>\n{raw_xml}'

    def build_wrapper(self, vast_tag_uri: str, creative: VASTCreative) -> str:
        """Generate a VAST Wrapper pointing to another VAST tag (daisy-chaining).

        A Wrapper element redirects the video player to another VAST tag
        (``vast_tag_uri``) while allowing the ad server to inject its own
        tracking pixels for impressions, errors, quartile events, clicks,
        and other VAST events.

        This is used when a creative has an external ``vast_url`` instead of
        a direct ``video_url``.
        """
        root = Element("VAST", version=self.version.value)
        ad = SubElement(root, "Ad", id=creative.ad_id)

        # Wrapper attributes
        wrapper_attrs: dict[str, str] = {}
        # fallbackOnNoAd: tells the player to show a blank slate if the
        # downstream VAST tag returns no ad (Magnite/Xandr expect this)
        if self.version != VASTVersion.V2_0:
            wrapper_attrs["fallbackOnNoAd"] = "true"
        wrapper = SubElement(ad, "Wrapper", **wrapper_attrs)

        # Ad system
        ad_system = SubElement(wrapper, "AdSystem", version="1.0")
        ad_system.text = "LiteAds"

        # VASTAdTagURI – the downstream VAST tag
        _add_cdata_element(wrapper, "VASTAdTagURI", vast_tag_uri)

        # Impression
        for url in creative.impression_urls:
            _add_cdata_element(wrapper, "Impression", url)

        # Error with [ERRORCODE] macro (IAB VAST spec requirement)
        for url in creative.error_urls:
            error_url = url
            if "[ERRORCODE]" not in error_url:
                sep = "&" if "?" in error_url else "?"
                error_url = f"{error_url}{sep}err=[ERRORCODE]"
            _add_cdata_element(wrapper, "Error", error_url)

        # Creatives – tracking events and click tracking in wrapper
        has_tracking = bool(creative.tracking_events)
        has_clicks = bool(creative.click_tracking)
        if has_tracking or has_clicks:
            creatives_el = SubElement(wrapper, "Creatives")
            creative_el = SubElement(creatives_el, "Creative")
            linear = SubElement(creative_el, "Linear")

            # Tracking events
            if has_tracking:
                tracking_events = SubElement(linear, "TrackingEvents")
                for te in creative.tracking_events:
                    t = SubElement(tracking_events, "Tracking", event=te.event)
                    t.text = f"__CDATA__{te.url}__ENDCDATA__"

            # VideoClicks (ClickTracking only – Wrapper cannot override ClickThrough)
            if has_clicks:
                video_clicks = SubElement(linear, "VideoClicks")
                for ct_url in creative.click_tracking:
                    _add_cdata_element(video_clicks, "ClickTracking", ct_url)

        raw_xml = tostring(root, encoding="unicode", xml_declaration=False)
        raw_xml = raw_xml.replace("__CDATA__", "<![CDATA[").replace("__ENDCDATA__", "]]>")
        return f'<?xml version="1.0" encoding="UTF-8"?>\n{raw_xml}'

    # ------------------------------------------------------------------
    # Internal tree construction
    # ------------------------------------------------------------------

    def _build_tree(self, c: VASTCreative) -> Element:
        root = Element("VAST", version=self.version.value)
        ad = SubElement(root, "Ad", id=c.ad_id)

        if self.version in (VASTVersion.V4_0, VASTVersion.V4_1, VASTVersion.V4_2) and c.ad_serving_id:
            ad.set("adServingId", c.ad_serving_id)

        inline = SubElement(ad, "InLine")

        # AdSystem
        ad_system = SubElement(inline, "AdSystem", version="1.0")
        ad_system.text = "LiteAds"

        # AdTitle
        ad_title = SubElement(inline, "AdTitle")
        ad_title.text = c.ad_title

        # Description (VAST 3.0+)
        if c.description and self.version != VASTVersion.V2_0:
            desc = SubElement(inline, "Description")
            desc.text = c.description

        # Advertiser (VAST 3.0+)
        if c.advertiser and self.version != VASTVersion.V2_0:
            adv = SubElement(inline, "Advertiser")
            adv.text = c.advertiser

        # Category (VAST 4.0+)
        if c.category and self._is_v4():
            cat = SubElement(inline, "Category", authority="https://iabtechlab.com")
            cat.text = c.category

        # Pricing (VAST 4.0+)
        if c.price is not None and self._is_v4():
            pricing = SubElement(
                inline, "Pricing",
                model=c.price_model,
                currency=c.price_currency,
            )
            pricing.text = f"{c.price:.4f}"

        # Survey (VAST 3.0+)
        if c.survey_url and self.version != VASTVersion.V2_0:
            _add_cdata_element(inline, "Survey", c.survey_url)

        # Error URLs with [ERRORCODE] macro (IAB VAST spec requirement)
        for url in c.error_urls:
            error_url = url
            if "[ERRORCODE]" not in error_url:
                sep = "&" if "?" in error_url else "?"
                error_url = f"{error_url}{sep}err=[ERRORCODE]"
            _add_cdata_element(inline, "Error", error_url)

        # Impression URLs
        for url in c.impression_urls:
            _add_cdata_element(inline, "Impression", url)

        # ViewableImpression (VAST 4.0+)
        if self._is_v4() and c.viewable_impression:
            vi = SubElement(inline, "ViewableImpression")
            _add_cdata_element(vi, "Viewable", c.viewable_impression)
            if c.not_viewable_url:
                _add_cdata_element(vi, "NotViewable", c.not_viewable_url)
            if c.view_undetermined_url:
                _add_cdata_element(vi, "ViewUndetermined", c.view_undetermined_url)

        # AdVerifications (VAST 4.x – DoubleVerify, IAS, MOAT compatibility)
        if self._is_v4() and c.verification_vendors:
            ad_verifications = SubElement(inline, "AdVerifications")
            for vendor in c.verification_vendors:
                verification = SubElement(ad_verifications, "Verification")
                if vendor.get("vendor"):
                    verification.set("vendor", vendor["vendor"])
                if vendor.get("js_url"):
                    js_resource = SubElement(
                        verification, "JavaScriptResource",
                        apiFramework="omid",
                        browserOptional="true",
                    )
                    js_resource.text = f"__CDATA__{vendor['js_url']}__ENDCDATA__"
                if vendor.get("params"):
                    vp = SubElement(verification, "VerificationParameters")
                    vp.text = f"__CDATA__{vendor['params']}__ENDCDATA__"

        # Creatives
        creatives_el = SubElement(inline, "Creatives")
        self._build_linear_creative(creatives_el, c)

        # Companion ads
        if c.companion_ads:
            self._build_companion_ads(creatives_el, c)

        return root

    def _build_linear_creative(self, creatives_el: Element, c: VASTCreative) -> None:
        creative_el = SubElement(creatives_el, "Creative", id=c.creative_id)

        linear_attrs: dict[str, str] = {}
        if c.skip_offset is not None:
            linear_attrs["skipoffset"] = _format_duration(c.skip_offset)

        linear = SubElement(creative_el, "Linear", **linear_attrs)

        # Duration
        duration = SubElement(linear, "Duration")
        duration.text = _format_duration(c.duration)

        # Tracking Events
        if c.tracking_events:
            tracking_events = SubElement(linear, "TrackingEvents")
            for te in c.tracking_events:
                t = SubElement(tracking_events, "Tracking", event=te.event)
                t.text = f"__CDATA__{te.url}__ENDCDATA__"

        # Video Clicks
        video_clicks = SubElement(linear, "VideoClicks")
        if c.click_through:
            _add_cdata_element(video_clicks, "ClickThrough", c.click_through)
        for ct_url in c.click_tracking:
            _add_cdata_element(video_clicks, "ClickTracking", ct_url)

        # Media Files
        media_files = SubElement(linear, "MediaFiles")
        for mf in c.media_files:
            attrs = {
                "delivery": mf.delivery,
                "type": mf.type,
                "bitrate": str(mf.bitrate),
                "width": str(mf.width),
                "height": str(mf.height),
                "scalable": str(mf.scalable).lower(),
                "maintainAspectRatio": str(mf.maintain_aspect_ratio).lower(),
            }
            if mf.codec:
                attrs["codec"] = mf.codec
            mf_el = SubElement(media_files, "MediaFile", **attrs)
            mf_el.text = f"__CDATA__{mf.url}__ENDCDATA__"

    def _build_companion_ads(self, creatives_el: Element, c: VASTCreative) -> None:
        creative_el = SubElement(creatives_el, "Creative")
        companion_ads = SubElement(creative_el, "CompanionAds")

        for comp in c.companion_ads:
            companion = SubElement(
                companion_ads, "Companion",
                width=str(comp.width),
                height=str(comp.height),
            )
            static = SubElement(
                companion, "StaticResource",
                creativeType=comp.resource_type,
            )
            static.text = f"__CDATA__{comp.static_resource}__ENDCDATA__"

            if comp.click_through:
                _add_cdata_element(companion, "CompanionClickThrough", comp.click_through)

            if comp.tracking_events:
                te_el = SubElement(companion, "TrackingEvents")
                for te in comp.tracking_events:
                    t = SubElement(te_el, "Tracking", event=te.event)
                    t.text = f"__CDATA__{te.url}__ENDCDATA__"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _is_v4(self) -> bool:
        return self.version in (VASTVersion.V4_0, VASTVersion.V4_1, VASTVersion.V4_2)


# ---------------------------------------------------------------------------
# Convenience factory
# ---------------------------------------------------------------------------

def build_vast_xml(
    *,
    version: str = "4.0",
    ad_id: str,
    creative_id: str,
    ad_title: str = "Video Ad",
    duration: int = 30,
    video_url: str,
    video_mime: str = "video/mp4",
    bitrate: int = 2500,
    width: int = 1920,
    height: int = 1080,
    click_through: Optional[str] = None,
    skip_offset: Optional[int] = None,
    impression_urls: Optional[list[str]] = None,
    error_urls: Optional[list[str]] = None,
    tracking_events: Optional[list[TrackingEvent]] = None,
    companion_image_url: Optional[str] = None,
    companion_width: int = 300,
    companion_height: int = 250,
    nurl: Optional[str] = None,
    burl: Optional[str] = None,
    price: Optional[float] = None,
    advertiser: str = "",
    category: Optional[str] = None,
) -> str:
    """
    One-shot convenience function to produce VAST XML.

    Example::

        xml = build_vast_xml(
            ad_id="123",
            creative_id="456",
            video_url="https://cdn.example.com/video.mp4",
            duration=30,
            impression_urls=["https://track.example.com/imp"],
            tracking_events=[
                TrackingEvent("start", "https://track.example.com/start"),
                TrackingEvent("complete", "https://track.example.com/complete"),
            ],
        )
    """
    media = MediaFile(
        url=video_url,
        type=video_mime,
        bitrate=bitrate,
        width=width,
        height=height,
    )

    companions: list[CompanionAd] = []
    if companion_image_url:
        companions.append(CompanionAd(
            width=companion_width,
            height=companion_height,
            static_resource=companion_image_url,
        ))

    creative = VASTCreative(
        ad_id=ad_id,
        creative_id=creative_id,
        ad_title=ad_title,
        advertiser=advertiser,
        duration=duration,
        skip_offset=skip_offset,
        click_through=click_through,
        impression_urls=impression_urls or [],
        error_urls=error_urls or [],
        media_files=[media],
        companion_ads=companions,
        tracking_events=tracking_events or [],
        nurl=nurl,
        burl=burl,
        price=price,
        price_model="cpm",
        category=category,
    )

    builder = VASTBuilder(version=version)
    return builder.build(creative)


def build_vast_wrapper_xml(
    *,
    version: str = "4.0",
    ad_id: str,
    creative_id: str,
    vast_tag_uri: str,
    ad_title: str = "Video Ad",
    impression_urls: Optional[list[str]] = None,
    error_urls: Optional[list[str]] = None,
    tracking_events: Optional[list[TrackingEvent]] = None,
    click_tracking: Optional[list[str]] = None,
    nurl: Optional[str] = None,
    burl: Optional[str] = None,
    price: Optional[float] = None,
) -> str:
    """
    One-shot convenience function to produce a VAST Wrapper XML document.

    Use this when the creative has an external VAST tag URL (``vast_url``)
    rather than a direct video file.  The wrapper redirects the video player
    to ``vast_tag_uri`` while injecting the ad server's own impression and
    tracking pixels.

    Example::

        xml = build_vast_wrapper_xml(
            ad_id="123",
            creative_id="456",
            vast_tag_uri="https://dsp.example.com/vast?id=abc",
            impression_urls=["https://track.example.com/imp"],
            tracking_events=[
                TrackingEvent("start", "https://track.example.com/start"),
                TrackingEvent("complete", "https://track.example.com/complete"),
            ],
        )
    """
    creative = VASTCreative(
        ad_id=ad_id,
        creative_id=creative_id,
        ad_title=ad_title,
        impression_urls=impression_urls or [],
        error_urls=error_urls or [],
        tracking_events=tracking_events or [],
        click_tracking=click_tracking or [],
        nurl=nurl,
        burl=burl,
        price=price,
        price_model="cpm",
    )

    builder = VASTBuilder(version=version)
    return builder.build_wrapper(vast_tag_uri, creative)
