"""
Ad-related database models for CPM CTV and In-App video ad serving.

Defines: Advertiser, Campaign, Creative, TargetingRule, HourlyStat, AdEvent
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from liteads.models.base import (
    Base,
    BidType,
    CreativeType,
    Status,
    TimestampMixin,
    VideoPlacement,
)


class Advertiser(Base, TimestampMixin):
    """Advertiser account for video ad campaigns."""

    __tablename__ = "advertisers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    company: Mapped[str | None] = mapped_column(String(255), nullable=True)
    contact_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    balance: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=Decimal("0"))
    daily_budget: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=Decimal("0"))
    status: Mapped[int] = mapped_column(Integer, default=Status.ACTIVE)

    # Relationships
    campaigns: Mapped[list["Campaign"]] = relationship(
        "Campaign", back_populates="advertiser", lazy="selectin"
    )


class Campaign(Base, TimestampMixin):
    """CPM video advertising campaign for CTV and In-App."""

    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    advertiser_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("advertisers.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Budget
    budget_daily: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=Decimal("0"))
    budget_total: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=Decimal("0"))
    spent_today: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=Decimal("0"))
    spent_total: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=Decimal("0"))

    # Bidding — CPM only
    bid_type: Mapped[int] = mapped_column(Integer, default=BidType.CPM)
    bid_amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 4), default=Decimal("0"),
        comment="CPM bid amount (cost per 1000 impressions)"
    )

    # Environment targeting (CTV, INAPP, or both)
    environment: Mapped[int | None] = mapped_column(
        Integer, nullable=True,
        comment="Target environment: 1=CTV, 2=INAPP, NULL=both"
    )

    # Floor pricing
    bid_floor: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), default=Decimal("0"),
        comment="Minimum CPM floor for this campaign",
    )
    floor_config: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True, default=None,
        comment="Dynamic floor rules JSON: {geo: {US: 5.0}, daypart: {prime: 8.0}, app: {com.roku: 6.0}}",
    )

    # Advertiser domain & IAB categories (for competitive separation in pods)
    adomain: Mapped[str | None] = mapped_column(
        String(255), nullable=True,
        comment="Advertiser domain for competitive separation (e.g. brand.com)",
    )
    iab_categories: Mapped[list[str] | None] = mapped_column(
        JSON, nullable=True, default=None,
        comment="IAB content categories for competitive separation",
    )

    # Frequency cap
    freq_cap_daily: Mapped[int] = mapped_column(Integer, default=10)
    freq_cap_hourly: Mapped[int] = mapped_column(Integer, default=3)

    # Schedule
    start_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    end_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Status
    status: Mapped[int] = mapped_column(Integer, default=Status.ACTIVE)

    # Stats (cached)
    impressions: Mapped[int] = mapped_column(Integer, default=0)
    completions: Mapped[int] = mapped_column(Integer, default=0)
    clicks: Mapped[int] = mapped_column(Integer, default=0)

    # Relationships
    advertiser: Mapped["Advertiser"] = relationship(
        "Advertiser", back_populates="campaigns"
    )
    creatives: Mapped[list["Creative"]] = relationship(
        "Creative", back_populates="campaign", lazy="selectin"
    )
    targeting_rules: Mapped[list["TargetingRule"]] = relationship(
        "TargetingRule", back_populates="campaign", lazy="selectin"
    )

    @property
    def is_active(self) -> bool:
        """Check if campaign is active and within schedule."""
        if self.status != Status.ACTIVE:
            return False
        now = datetime.now(timezone.utc)
        if self.start_time and now < self.start_time:
            return False
        if self.end_time and now > self.end_time:
            return False
        return True

    @property
    def cpm_cost(self) -> Decimal:
        """Get the CPM cost per single impression."""
        return self.bid_amount / Decimal("1000")


class Creative(Base, TimestampMixin):
    """Video ad creative for CTV and In-App environments."""

    __tablename__ = "creatives"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    campaign_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("campaigns.id"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Video creative fields
    video_url: Mapped[str] = mapped_column(
        String(1024), nullable=False, comment="Video file URL (MP4/HLS/DASH)"
    )
    vast_url: Mapped[str | None] = mapped_column(
        String(1024), nullable=True, comment="VAST tag URL for video ad serving"
    )
    companion_image_url: Mapped[str | None] = mapped_column(
        String(1024), nullable=True, comment="Companion banner image (optional)"
    )
    landing_url: Mapped[str] = mapped_column(String(1024), nullable=False)

    # Video metadata
    creative_type: Mapped[int] = mapped_column(
        Integer, default=CreativeType.INAPP_VIDEO
    )
    duration: Mapped[int] = mapped_column(
        Integer, default=30, comment="Video duration in seconds"
    )
    width: Mapped[int] = mapped_column(Integer, default=1920)
    height: Mapped[int] = mapped_column(Integer, default=1080)
    bitrate: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="Video bitrate in kbps"
    )
    mime_type: Mapped[str] = mapped_column(
        String(50), default="video/mp4", comment="Video MIME type"
    )
    skippable: Mapped[bool] = mapped_column(
        Boolean, default=True, comment="Whether the ad can be skipped"
    )
    skip_after: Mapped[int] = mapped_column(
        Integer, default=5, comment="Seconds before skip is allowed"
    )

    # Placement
    placement: Mapped[int] = mapped_column(
        Integer, default=VideoPlacement.PRE_ROLL, comment="Video placement type"
    )

    # Status
    status: Mapped[int] = mapped_column(Integer, default=Status.ACTIVE)

    # Quality score (0-100)
    quality_score: Mapped[int] = mapped_column(Integer, default=80)

    # Relationships
    campaign: Mapped["Campaign"] = relationship("Campaign", back_populates="creatives")


class TargetingRule(Base, TimestampMixin):
    """Targeting rules for video campaigns.

    Supports CTV and In-App specific targeting:
    - geo: {"countries": ["US", "GB"], "dma": ["501", "803"]}
    - device: {"types": ["ctv", "mobile"], "os": ["roku", "firetv", "ios", "android"]}
    - app_bundle: {"bundles": ["com.roku.app", "com.example.app"]}
    - content_genre: {"genres": ["news", "sports", "entertainment"]}
    - environment: {"values": ["ctv", "inapp"]}
    - daypart: {"hours": [18, 19, 20, 21], "days": ["mon", "tue"]}
    """

    __tablename__ = "targeting_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    campaign_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("campaigns.id"), nullable=False
    )

    rule_type: Mapped[str] = mapped_column(String(50), nullable=False)
    rule_value: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    is_include: Mapped[bool] = mapped_column(Boolean, default=True)

    # Relationships
    campaign: Mapped["Campaign"] = relationship(
        "Campaign", back_populates="targeting_rules"
    )


class AdEvent(Base, TimestampMixin):
    """Video ad event tracking for billing & analytics."""

    __tablename__ = "ad_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    campaign_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("campaigns.id", ondelete="SET NULL"), nullable=True
    )
    creative_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("creatives.id", ondelete="SET NULL"), nullable=True
    )
    event_type: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="EventType enum value"
    )
    event_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)

    # CPM billing: cost is recorded on IMPRESSION events
    cost: Mapped[Decimal] = mapped_column(
        Numeric(10, 6), default=Decimal("0.000000"),
        comment="Cost charged for this event (CPM-based, on impression)"
    )

    # Win price from exchange (AUCTION_PRICE after macro expansion)
    win_price: Mapped[Decimal] = mapped_column(
        Numeric(10, 6), default=Decimal("0.000000"),
        comment="Auction clearing price from exchange nurl/burl"
    )

    # Demand / supply dimensions for analytics
    adomain: Mapped[str | None] = mapped_column(
        String(255), nullable=True, index=True,
        comment="Advertiser domain (e.g. brand.com)"
    )
    source_name: Mapped[str | None] = mapped_column(
        String(255), nullable=True, index=True,
        comment="Supply source / SSP name"
    )
    bundle_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True, index=True,
        comment="App bundle ID (e.g. com.roku.app)"
    )
    country_code: Mapped[str | None] = mapped_column(
        String(3), nullable=True, index=True,
        comment="ISO 3166-1 alpha-2 country code"
    )

    # Video-specific event data
    video_position: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="Video playback position in seconds"
    )
    environment: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="1=CTV, 2=INAPP"
    )

    __table_args__ = (
        # Composite index for demand/supply report queries
        Index("ix_ad_events_campaign_type_time", "campaign_id", "event_type", "event_time"),
    )


class HourlyStat(Base):
    """Hourly statistics for video campaigns."""

    __tablename__ = "hourly_stats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    campaign_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("campaigns.id"), nullable=False
    )
    stat_hour: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    # Request-level counters
    ad_requests: Mapped[int] = mapped_column(
        Integer, default=0, comment="Number of bid requests received"
    )
    ad_opportunities: Mapped[int] = mapped_column(
        Integer, default=0, comment="Number of eligible impressions (bid opportunities)"
    )
    wins: Mapped[int] = mapped_column(
        Integer, default=0, comment="Auction wins (nurl callbacks)"
    )

    # Event counters
    impressions: Mapped[int] = mapped_column(Integer, default=0)
    starts: Mapped[int] = mapped_column(Integer, default=0)
    first_quartiles: Mapped[int] = mapped_column(Integer, default=0)
    midpoints: Mapped[int] = mapped_column(Integer, default=0)
    third_quartiles: Mapped[int] = mapped_column(Integer, default=0)
    completions: Mapped[int] = mapped_column(Integer, default=0)
    clicks: Mapped[int] = mapped_column(Integer, default=0)
    skips: Mapped[int] = mapped_column(Integer, default=0)

    # Revenue / spend
    spend: Mapped[Decimal] = mapped_column(
        Numeric(12, 4), default=Decimal("0"),
        comment="Gross revenue (sum of CPM costs)"
    )
    win_price_sum: Mapped[Decimal] = mapped_column(
        Numeric(12, 4), default=Decimal("0"),
        comment="Sum of auction win prices (for avg_win_price calc)"
    )

    # Calculated metrics
    vtr: Mapped[Decimal] = mapped_column(
        Numeric(8, 6), default=Decimal("0"),
        comment="View-through rate (completions/impressions)"
    )

    __table_args__ = (
        # Prevent duplicate rows on re-flush of the same hour
        UniqueConstraint("campaign_id", "stat_hour", name="uq_hourly_stat_campaign_hour"),
    )


# =========================================================================
# Supply / Demand management models
# =========================================================================


class SupplyTag(Base, TimestampMixin):
    """Publisher-facing supply VAST tag configuration.

    Publishers embed these VAST tag URLs in their video players.
    Each supply tag can target specific demand endpoints to fill ads.
    """

    __tablename__ = "supply_tags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    slot_id: Mapped[str] = mapped_column(
        String(100), nullable=False, unique=True,
        comment="Unique slot/zone identifier used in VAST tag URL",
    )

    # Pricing
    bid_floor: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), default=Decimal("0"),
        comment="Minimum CPM floor price for this supply tag",
    )
    margin_pct: Mapped[Decimal] = mapped_column(
        Numeric(6, 2), default=Decimal("0"),
        comment="Margin percentage the ad server takes (e.g. 20.00 = 20%)",
    )

    # Video settings
    environment: Mapped[int | None] = mapped_column(
        Integer, nullable=True,
        comment="Target environment: 1=CTV, 2=INAPP, NULL=both",
    )
    min_duration: Mapped[int] = mapped_column(Integer, default=5)
    max_duration: Mapped[int] = mapped_column(Integer, default=30)
    width: Mapped[int] = mapped_column(Integer, default=1920)
    height: Mapped[int] = mapped_column(Integer, default=1080)

    # Status
    status: Mapped[int] = mapped_column(Integer, default=Status.ACTIVE)

    # Relationships
    demand_mappings: Mapped[list["SupplyDemandMapping"]] = relationship(
        "SupplyDemandMapping", back_populates="supply_tag", lazy="selectin",
        cascade="all, delete-orphan",
    )


class DemandEndpoint(Base, TimestampMixin):
    """Third-party OpenRTB demand endpoint (DSP / bridge ad server).

    The ad server sends bid requests to these endpoints to obtain creatives.
    """

    __tablename__ = "demand_endpoints"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    endpoint_url: Mapped[str] = mapped_column(
        String(1024), nullable=False,
        comment="Full URL for OpenRTB 2.6 bid requests",
    )

    # Pricing
    bid_floor: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), default=Decimal("0"),
        comment="Minimum CPM bid floor to send in bid requests",
    )
    margin_pct: Mapped[Decimal] = mapped_column(
        Numeric(6, 2), default=Decimal("0"),
        comment="Margin percentage on revenue from this demand source",
    )

    # Request settings
    timeout_ms: Mapped[int] = mapped_column(
        Integer, default=500,
        comment="Request timeout in milliseconds",
    )
    qps_limit: Mapped[int] = mapped_column(
        Integer, default=0,
        comment="Max queries per second (0 = unlimited)",
    )

    # Status
    status: Mapped[int] = mapped_column(Integer, default=Status.ACTIVE)

    # Relationships
    supply_mappings: Mapped[list["SupplyDemandMapping"]] = relationship(
        "SupplyDemandMapping",
        back_populates="demand_endpoint",
        lazy="selectin",
        foreign_keys="SupplyDemandMapping.demand_endpoint_id",
    )


class DemandVastTag(Base, TimestampMixin):
    """Third-party demand VAST tag source.

    Instead of OpenRTB, some demand sources provide a VAST tag URL
    that can be used as a wrapper/redirect.
    """

    __tablename__ = "demand_vast_tags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    vast_url: Mapped[str] = mapped_column(
        String(2048), nullable=False,
        comment="Third-party VAST tag URL (supports macros)",
    )

    # Pricing
    bid_floor: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), default=Decimal("0"),
        comment="Minimum CPM floor for this demand VAST tag",
    )
    margin_pct: Mapped[Decimal] = mapped_column(
        Numeric(6, 2), default=Decimal("0"),
        comment="Margin percentage on revenue from this demand VAST tag",
    )
    cpm_value: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), default=Decimal("0"),
        comment="Fixed CPM value / estimated value if known",
    )

    # Status
    status: Mapped[int] = mapped_column(Integer, default=Status.ACTIVE)

    # Relationships
    supply_mappings: Mapped[list["SupplyDemandMapping"]] = relationship(
        "SupplyDemandMapping",
        back_populates="demand_vast_tag",
        lazy="selectin",
        foreign_keys="SupplyDemandMapping.demand_vast_tag_id",
    )


class SupplyDemandMapping(Base, TimestampMixin):
    """Links supply tags to their demand sources (ORTB endpoints or VAST tags).

    A supply tag can target multiple demand sources with priority/weight.
    """

    __tablename__ = "supply_demand_mappings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    supply_tag_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("supply_tags.id", ondelete="CASCADE"), nullable=False,
    )

    # One of these two should be set (not both)
    demand_endpoint_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("demand_endpoints.id", ondelete="CASCADE"), nullable=True,
    )
    demand_vast_tag_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("demand_vast_tags.id", ondelete="CASCADE"), nullable=True,
    )

    # Routing
    priority: Mapped[int] = mapped_column(
        Integer, default=1,
        comment="Lower number = higher priority (1 = highest)",
    )
    weight: Mapped[int] = mapped_column(
        Integer, default=100,
        comment="Weight for load balancing among same-priority sources",
    )

    # Status
    status: Mapped[int] = mapped_column(Integer, default=Status.ACTIVE)

    # Relationships
    supply_tag: Mapped["SupplyTag"] = relationship(
        "SupplyTag", back_populates="demand_mappings", lazy="selectin",
    )
    demand_endpoint: Mapped["DemandEndpoint | None"] = relationship(
        "DemandEndpoint",
        back_populates="supply_mappings",
        foreign_keys=[demand_endpoint_id],
        lazy="selectin",
    )
    demand_vast_tag: Mapped["DemandVastTag | None"] = relationship(
        "DemandVastTag",
        back_populates="supply_mappings",
        foreign_keys=[demand_vast_tag_id],
        lazy="selectin",
    )
