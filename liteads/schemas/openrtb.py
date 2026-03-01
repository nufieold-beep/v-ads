"""
OpenRTB 2.6 Bid Request / Response schemas – CPM CTV & In-App Video Only.

Reference: IAB OpenRTB 2.6 Specification
https://iabtechlab.com/standards/openrtb/

Only video-related objects are modelled; display / audio / native are omitted
because LiteAds exclusively serves CTV and in-app video inventory.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field, model_validator


# ============================================================================
# OpenRTB 2.6 – Bid Request objects
# ============================================================================

class Geo(BaseModel):
    """Geographic location (Section 3.2.19)."""

    lat: Optional[float] = None
    lon: Optional[float] = None
    type: Optional[int] = None          # 1=GPS, 2=IP, 3=User
    accuracy: Optional[int] = None
    ipservice: Optional[int] = None     # IP geolocation service (1=ip2location, 2=Neustar, 3=MaxMind, 4=NetAcuity)
    country: Optional[str] = None       # ISO-3166-1 Alpha-3
    region: Optional[str] = None
    metro: Optional[str] = None         # Nielsen DMA code
    city: Optional[str] = None
    zip: Optional[str] = None
    utcoffset: Optional[int] = None
    ext: Optional[dict[str, Any]] = None


class Device(BaseModel):
    """Device information (Section 3.2.18)."""

    ua: Optional[str] = None            # User-Agent
    dnt: Optional[int] = None           # Do Not Track (0 or 1)
    ip: Optional[str] = None            # IPv4
    ipv6: Optional[str] = None
    geo: Optional[Geo] = None
    devicetype: Optional[int] = None    # IAB: 3=CTV, 1=Mobile/Tablet, 7=Set-Top Box
    make: Optional[str] = None          # e.g. "ROKU", "LG", "Amazon", "Samsung"
    model: Optional[str] = None         # e.g. "DIGITAL VIDEO PLAYER", "50UN6950ZUF"
    os: Optional[str] = None            # e.g. "Roku", "webOS TV", "Fire OS", "tvOS", "Tizen"
    osv: Optional[str] = None           # OS version
    hwv: Optional[str] = None           # Hardware version
    w: Optional[int] = None             # Screen width pixels
    h: Optional[int] = None             # Screen height pixels
    ppi: Optional[int] = None           # Screen DPI
    pxratio: Optional[float] = None     # Pixel ratio
    js: Optional[int] = None            # JS support
    language: Optional[str] = None      # ISO-639-1
    ifa: Optional[str] = None           # Advertising ID (RIDA / AFAI / IDFA / TIFA etc.)
    didsha1: Optional[str] = None
    didmd5: Optional[str] = None
    lmt: Optional[int] = None           # Limit Ad Tracking (0 or 1)
    connectiontype: Optional[int] = None  # 1=Ethernet, 2=Wifi, etc.
    ext: Optional[dict[str, Any]] = None

    @property
    def ifa_type(self) -> Optional[str]:
        """Extract ifa_type from ext if present (e.g. Roku sends ext.ifa_type='rida')."""
        if self.ext and "ifa_type" in self.ext:
            return self.ext["ifa_type"]
        return None


class Video(BaseModel):
    """Video impression object (Section 3.2.7)."""

    mimes: list[str] = Field(
        default_factory=lambda: ["video/mp4", "video/webm"],
        description="Supported MIME types",
    )
    protocols: list[int] = Field(
        default_factory=lambda: [2, 3, 6, 7, 8],
        description="VAST protocol versions: 2=VAST2.0, 3=VAST3.0, 6=VAST4.0, 7=VAST4.1, 8=VAST4.2",
    )
    minduration: Optional[int] = None   # Minimum duration (seconds)
    maxduration: Optional[int] = None   # Maximum duration (seconds)
    w: Optional[int] = None             # Width pixels
    h: Optional[int] = None             # Height pixels
    startdelay: Optional[int] = None    # 0=pre-roll, >0=mid-roll, -1=generic mid, -2=generic post
    placement: Optional[int] = None     # 1=In-Stream, 2=In-Banner, 3=In-Article, 4=In-Feed, 5=Floating
    plcmt: Optional[int] = None         # OpenRTB 2.6 video placement type
    linearity: Optional[int] = None     # 1=Linear (in-stream), 2=Non-linear (overlay)
    skip: Optional[int] = None          # 0=not skippable, 1=skippable
    skipmin: Optional[int] = None       # Min seconds before skip
    skipafter: Optional[int] = None     # Seconds until skip button
    sequence: Optional[int] = None      # Sequence number for multi-ad (pod position)
    battr: list[int] = Field(default_factory=list)  # Blocked creative attributes
    maxextended: Optional[int] = None
    minbitrate: Optional[int] = None
    maxbitrate: Optional[int] = None
    boxingallowed: Optional[int] = 1
    playbackmethod: list[int] = Field(default_factory=list)
    playbackend: Optional[int] = None   # 1=video-completion, 2=leaving-viewport, 3=floating/slider
    delivery: list[int] = Field(default_factory=list)  # 1=streaming, 2=progressive, 3=download
    pos: Optional[int] = None           # Ad position
    # Ad Pod fields (OpenRTB 2.6)
    poddur: Optional[int] = None        # Total pod duration (seconds)
    maxseq: Optional[int] = None        # Max ads in the pod
    podid: Optional[str] = None         # Pod identifier
    podseq: Optional[int] = None        # Pod sequence position (0=any, 1=first, -1=last)
    poddedupe: list[int] = Field(default_factory=list)  # Deduplication signals (1=same creative, 2=IAB cat, 3=adomain)
    api: list[int] = Field(default_factory=list)     # Supported API frameworks
    companionad: list[dict[str, Any]] = Field(default_factory=list)
    companiontype: list[int] = Field(default_factory=list)
    ext: Optional[dict[str, Any]] = None


class Publisher(BaseModel):
    """Publisher object (Section 3.2.15)."""

    id: Optional[str] = None
    name: Optional[str] = None
    cat: list[str] = Field(default_factory=list)  # IAB content categories
    domain: Optional[str] = None
    ext: Optional[dict[str, Any]] = None


class Content(BaseModel):
    """Content object (Section 3.2.16)."""

    id: Optional[str] = None
    episode: Optional[int] = None
    title: Optional[str] = None
    series: Optional[str] = None
    season: Optional[str] = None
    artist: Optional[str] = None
    genre: Optional[str] = None
    album: Optional[str] = None
    isrc: Optional[str] = None
    producer: Optional[dict[str, Any]] = None
    url: Optional[str] = None
    cat: list[str] = Field(default_factory=list)
    cattax: Optional[int] = None        # Category taxonomy (1=IAB Content Cat 1.0, 2=IAB Content Cat 2.0, etc.)
    prodq: Optional[int] = None
    videoquality: Optional[int] = None  # Deprecated in 2.6 but still sent
    context: Optional[int] = None       # 1=video, 2=game, 3=music, 4=app
    contentrating: Optional[str] = None
    userrating: Optional[str] = None
    qagmediarating: Optional[int] = None
    keywords: Optional[str] = None
    livestream: Optional[int] = None
    sourcerelationship: Optional[int] = None
    len: Optional[int] = None           # Content length (seconds)
    language: Optional[str] = None
    embeddable: Optional[int] = None
    data: list[dict[str, Any]] = Field(default_factory=list)
    ext: Optional[dict[str, Any]] = None


class App(BaseModel):
    """App object for CTV / In-App (Section 3.2.14)."""

    id: Optional[str] = None
    name: Optional[str] = None
    bundle: Optional[str] = None        # App bundle ID / package name
    domain: Optional[str] = None
    storeurl: Optional[str] = None
    cat: list[str] = Field(default_factory=list)  # IAB content categories
    sectioncat: list[str] = Field(default_factory=list)
    pagecat: list[str] = Field(default_factory=list)
    ver: Optional[str] = None
    privacypolicy: Optional[int] = None
    paid: Optional[int] = None
    publisher: Optional[Publisher] = None
    content: Optional[Content] = None
    keywords: Optional[str] = None
    inventorypartnerdomain: Optional[str] = None  # Inventory partner domain (OpenRTB 2.6)
    ext: Optional[dict[str, Any]] = None


class Segment(BaseModel):
    """Audience segment (Section 3.2.22)."""

    id: Optional[str] = None
    name: Optional[str] = None
    value: Optional[str] = None


class Data(BaseModel):
    """User data segment (Section 3.2.21)."""

    id: Optional[str] = None
    name: Optional[str] = None
    segment: list[Segment] = Field(default_factory=list)


class User(BaseModel):
    """User object (Section 3.2.20)."""

    id: Optional[str] = None
    buyeruid: Optional[str] = None
    yob: Optional[int] = None
    gender: Optional[str] = None        # M / F / O
    keywords: Optional[str] = None
    data: list[Data] = Field(default_factory=list)
    ext: Optional[dict[str, Any]] = None


class Regs(BaseModel):
    """Regulatory signals (Section 3.2.3)."""

    coppa: Optional[int] = None         # 0 or 1
    gdpr: Optional[int] = None          # 0 or 1 (via ext in practice)
    us_privacy: Optional[str] = None    # IAB US Privacy string (CCPA)
    gpp: Optional[str] = None           # IAB Global Privacy Platform string
    gpp_sid: Optional[list[int]] = Field(
        default=None,
        description="Section IDs for applicable GPP sections",
    )
    ext: Optional[dict[str, Any]] = None

    @property
    def gdpr_applies(self) -> bool:
        """Check if GDPR applies (from gdpr field or ext.gdpr)."""
        if self.gdpr is not None:
            return self.gdpr == 1
        if self.ext and "gdpr" in self.ext:
            return self.ext["gdpr"] == 1
        return False

    @property
    def consent_string(self) -> Optional[str]:
        """Extract TCF consent string from ext."""
        if self.ext and "consent" in self.ext:
            return self.ext["consent"]
        return None


class SupplyChainNode(BaseModel):
    """Supply chain node (IAB SupplyChain / ads.txt spec)."""

    asi: str = Field(..., description="Canonical domain of the SSP/exchange")
    sid: str = Field(..., description="Seller ID / account ID on the SSP")
    hp: int = Field(..., description="1 = directly paid, 0 = intermediary")
    rid: Optional[str] = None           # Request ID assigned by this node
    name: Optional[str] = None          # Name of the entity
    domain: Optional[str] = None        # Business domain
    ext: Optional[dict[str, Any]] = None


class SupplyChain(BaseModel):
    """Supply chain object (Section 3.2.25)."""

    complete: int = Field(..., description="1 = full chain, 0 = partial")
    ver: str = Field("1.0", description="Supply chain spec version")
    nodes: list[SupplyChainNode] = Field(..., description="Array of supply chain nodes")
    ext: Optional[dict[str, Any]] = None


class Source(BaseModel):
    """Supply chain / source (Section 3.2.2)."""

    fd: Optional[int] = None            # Entity responsible for final impression sale
    tid: Optional[str] = None           # Transaction ID
    pchain: Optional[str] = None        # TAG Payment ID chain
    schain: Optional[SupplyChain] = None  # Supply chain object
    ext: Optional[dict[str, Any]] = None

    @model_validator(mode="after")
    def _extract_schain_from_ext(self) -> "Source":
        """Some SSPs put schain inside ext instead of top-level."""
        if self.schain is None and self.ext and "schain" in self.ext:
            try:
                self.schain = SupplyChain(**self.ext["schain"])
            except Exception:
                pass
        return self


class Deal(BaseModel):
    """Deal object for private marketplace (Section 3.2.12)."""

    id: str = Field(..., description="Unique deal ID")
    bidfloor: float = 0.0
    bidfloorcur: str = "USD"
    at: Optional[int] = None            # Auction type override for this deal
    wseat: list[str] = Field(default_factory=list)
    wadomain: list[str] = Field(default_factory=list)
    ext: Optional[dict[str, Any]] = None


class PMP(BaseModel):
    """Private marketplace container (Section 3.2.11)."""

    private_auction: int = Field(0, alias="private_auction")
    deals: list[Deal] = Field(default_factory=list)
    ext: Optional[dict[str, Any]] = None

    model_config = {"populate_by_name": True}


class Imp(BaseModel):
    """Impression object (Section 3.2.4) – video only."""

    id: str = "1"
    video: Optional[Video] = None
    displaymanager: Optional[str] = None
    displaymanagerver: Optional[str] = None
    instl: Optional[int] = None         # 1 = interstitial / full-screen
    tagid: Optional[str] = None
    bidfloor: float = 0.0               # Minimum CPM bid
    bidfloorcur: str = "USD"
    secure: Optional[int] = 1           # 0 = non-secure, 1 = secure
    exp: Optional[int] = None           # Impression expiry (seconds after auction)
    rwdd: Optional[int] = None          # 1 = rewarded video (OpenRTB 2.6)
    pmp: Optional[PMP] = None           # Private marketplace
    metric: list[dict[str, Any]] = Field(default_factory=list)  # Impression-level metrics
    ext: Optional[dict[str, Any]] = None


class Site(BaseModel):
    """Site object – used when inventory is web-based CTV (Section 3.2.13).

    Some CTV inventory (e.g. Samsung TV Plus web-based player, LG Channels
    via browser) sends a Site object instead of App.
    """

    id: Optional[str] = None
    name: Optional[str] = None
    domain: Optional[str] = None
    cat: list[str] = Field(default_factory=list)
    sectioncat: list[str] = Field(default_factory=list)
    pagecat: list[str] = Field(default_factory=list)
    page: Optional[str] = None
    ref: Optional[str] = None           # Referrer URL
    search: Optional[str] = None
    mobile: Optional[int] = None        # 1 = mobile-optimized
    privacypolicy: Optional[int] = None
    publisher: Optional[Publisher] = None
    content: Optional[Content] = None
    keywords: Optional[str] = None
    ext: Optional[dict[str, Any]] = None


class BidRequest(BaseModel):
    """
    OpenRTB 2.6 Bid Request (Section 3.2.1).

    LiteAds processes video impressions for CTV and in-app inventory.
    Compatible with: Magnite, Xandr, OpenX, Freewheel, GAM, Unruly,
    SmartHub, Adtelligent, Project Limelight, and other exchanges.
    """

    id: str = Field(..., description="Unique auction ID")
    imp: list[Imp] = Field(..., min_length=1, description="Array of impression objects")
    site: Optional[Site] = None         # Web-based CTV inventory
    app: Optional[App] = None           # App-based CTV / In-App inventory
    device: Optional[Device] = None
    user: Optional[User] = None
    at: int = Field(default=2, description="Auction type: 1=first-price, 2=second-price")
    tmax: Optional[int] = Field(default=200, description="Max response time (ms)")
    wseat: list[str] = Field(default_factory=list, description="Allowed buyer seats")
    bseat: list[str] = Field(default_factory=list, description="Blocked buyer seats")
    allimps: Optional[int] = None       # 1 = exchange can verify all impressions
    cur: list[str] = Field(default_factory=lambda: ["USD"], description="Allowed currencies")
    wlang: list[str] = Field(default_factory=list, description="Allowed languages")
    wlangb: list[str] = Field(default_factory=list, description="Allowed languages (BCP-47, OpenRTB 2.6)")
    bcat: list[str] = Field(default_factory=list, description="Blocked IAB categories")
    cattax: Optional[int] = None        # Category taxonomy version
    badv: list[str] = Field(default_factory=list, description="Blocked advertiser domains")
    bapp: list[str] = Field(default_factory=list, description="Blocked app bundles")
    source: Optional[Source] = None
    regs: Optional[Regs] = None
    ext: Optional[dict[str, Any]] = None

    model_config = {"extra": "allow"}

    @property
    def environment(self) -> str:
        """Infer environment from device type, app/site presence, and OS.

        CTV detection priority:
        1. Device type 3 (CTV) or 7 (Set-Top Box)
        2. OS is a known CTV platform (Roku, Fire OS, tvOS, Tizen, webOS)
        3. App present with CTV-specific bundle patterns
        """
        # Device-type based detection (most reliable)
        if self.device and self.device.devicetype in (3, 7):
            return "ctv"

        # OS-based detection
        if self.device and self.device.os:
            os_lower = self.device.os.lower().replace(" ", "")
            ctv_os = (
                "roku", "firetv", "fireos", "tvos", "tizen",
                "webos", "webostv", "vizio", "androidtv",
                "chromecast", "playstation", "xbox",
            )
            if any(cos in os_lower for cos in ctv_os):
                return "ctv"

        # UA-based detection (fallback)
        if self.device and self.device.ua:
            ua_lower = self.device.ua.lower()
            ctv_ua = (
                "smarttv", "smart-tv", "ctv", "roku", "tizen",
                "webos", "web0s", "firetv", "aftb", "aftm",
                "appletv", "apple tv", "bravia", "crkey",
                "chromecast", "vizio", "hbbtv",
            )
            if any(marker in ua_lower for marker in ctv_ua):
                return "ctv"

        return "inapp"

    @property
    def is_coppa(self) -> bool:
        """Check if COPPA applies."""
        return bool(self.regs and self.regs.coppa == 1)

    @property
    def supply_chain(self) -> Optional[SupplyChain]:
        """Get supply chain from source, if present."""
        if self.source and self.source.schain:
            return self.source.schain
        return None


# ============================================================================
# OpenRTB 2.6 – Bid Response objects
# ============================================================================

class Bid(BaseModel):
    """Single bid (Section 4.2.3).

    Fields are compatible with OpenX, Magnite, Xandr, GAM, Freewheel,
    Unruly, SmartHub, Adtelligent, and Project Limelight exchanges.
    """

    id: str = Field(..., description="Bidder-generated bid ID")
    impid: str = Field(..., description="Impression ID from request")
    price: float = Field(..., description="Bid price in CPM")
    nurl: Optional[str] = None          # Win notice URL (${AUCTION_PRICE} macro)
    burl: Optional[str] = None          # Billing notice URL (${AUCTION_PRICE} macro)
    lurl: Optional[str] = None          # Loss notice URL (${AUCTION_LOSS} macro)
    adm: Optional[str] = None           # VAST XML markup (ad response)
    adid: Optional[str] = None          # Pre-loaded ad ID
    adomain: list[str] = Field(default_factory=list, description="Advertiser domains")
    bundle: Optional[str] = None        # App bundle for the creative
    iurl: Optional[str] = None          # Sample image URL for QA
    cid: Optional[str] = None           # Campaign ID
    crid: Optional[str] = None          # Creative ID
    cat: list[str] = Field(default_factory=list)     # IAB content categories
    attr: list[int] = Field(default_factory=list)    # Creative attributes
    api: Optional[int] = None           # Supported API framework
    protocol: Optional[int] = None      # VAST protocol of the markup
    qagmediarating: Optional[int] = None
    language: Optional[str] = None
    dealid: Optional[str] = None        # Deal ID if bid relates to a PMP deal
    w: Optional[int] = None
    h: Optional[int] = None
    wratio: Optional[int] = None
    hratio: Optional[int] = None
    exp: Optional[int] = None           # Advisory expiration (seconds)
    dur: Optional[int] = None           # Video duration of the creative
    mtype: Optional[int] = None         # 1=banner, 2=video, 4=audio
    apis: list[int] = Field(default_factory=list)  # Supported API frameworks (OpenRTB 2.6)
    cattax: Optional[int] = None        # Category taxonomy version
    ext: Optional[dict[str, Any]] = None


class SeatBid(BaseModel):
    """Seat bid (Section 4.2.2)."""

    bid: list[Bid] = Field(..., min_length=1)
    seat: Optional[str] = None          # Buyer seat ID
    group: int = 0                      # 0 = impressions can be won individually
    ext: Optional[dict[str, Any]] = None


class BidResponse(BaseModel):
    """
    OpenRTB 2.6 Bid Response (Section 4.2.1).

    A valid response MUST include at least one seatbid with one bid.
    To indicate no-bid, return HTTP 204 (handled by the router).
    """

    id: str = Field(..., description="Matches BidRequest.id")
    seatbid: list[SeatBid] = Field(default_factory=list)
    bidid: Optional[str] = None         # Bidder-generated response ID
    cur: str = "USD"
    customdata: Optional[str] = None
    nbr: Optional[int] = None           # No-bid reason code (Section 5.24)
    ext: Optional[dict[str, Any]] = None


# ============================================================================
# No-Bid reason codes (Section 5.24)
# ============================================================================

class NoBidReason:
    """OpenRTB no-bid reason codes (Section 5.24)."""

    UNKNOWN_ERROR = 0
    TECHNICAL_ERROR = 1
    INVALID_REQUEST = 2
    KNOWN_SPIDER = 3
    SUSPECTED_NON_HUMAN = 4
    CLOUD_OR_PROXY_IP = 5
    UNSUPPORTED_DEVICE = 6
    BLOCKED_PUBLISHER = 7
    UNMATCHED_USER = 8
    DAILY_READER_CAP = 9
    DAILY_DOMAIN_CAP = 10
    # Extended codes commonly used by exchanges
    BELOW_FLOOR = 100
    NO_FILL = 101
    TIMEOUT = 102
    BLOCKED_ADVERTISER = 103
    BLOCKED_CATEGORY = 104
