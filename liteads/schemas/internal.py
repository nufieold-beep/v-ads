"""
Internal data schemas for CPM CTV and In-App video ad serving pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AdCandidate:
    """Video ad candidate for CPM ranking."""

    campaign_id: int
    creative_id: int
    advertiser_id: int
    bid: float           # CPM bid amount
    bid_type: int = 1    # Always CPM (1)

    # Targeting match info
    targeting_score: float = 1.0

    # Predicted scores (for view-through optimization)
    pvtr: float = 0.0   # Predicted view-through rate (video completion)
    pctr: float = 0.0   # Predicted click-through rate

    # Calculated scores
    ecpm: float = 0.0   # For CPM, ecpm == bid
    score: float = 0.0  # Final ranking score

    # Video creative info
    title: str | None = None
    description: str | None = None
    video_url: str = ""
    vast_url: str | None = None
    companion_image_url: str | None = None
    landing_url: str = ""
    creative_type: int = 1       # CreativeType.CTV_VIDEO or INAPP_VIDEO
    duration: int = 30           # Video duration in seconds
    width: int = 1920
    height: int = 1080
    bitrate: int | None = None
    mime_type: str = "video/mp4"
    skippable: bool = True
    skip_after: int = 5
    placement: int = 1           # VideoPlacement.PRE_ROLL

    # Environment
    environment: int = 1         # Environment.CTV or INAPP

    # Extra info
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class UserContext:
    """User context for CTV/In-App video ad serving."""

    user_id: str | None = None
    user_hash: int = 0  # Hash for bucketing

    # Environment
    environment: str = "ctv"  # "ctv" or "inapp"

    # Device
    device_type: str = ""    # ctv, mobile, tablet, set_top_box
    os: str = ""             # roku, firetv, tvos, tizen, android, ios
    os_version: str = ""
    device_model: str = ""
    device_brand: str = ""
    ifa: str | None = None   # Identifier for advertising
    ifa_type: str | None = None

    # Geo
    ip: str = ""
    country: str = ""
    region: str = ""
    city: str = ""
    dma: str = ""            # Designated Market Area
    latitude: float | None = None
    longitude: float | None = None

    # App/Content
    app_id: str = ""
    app_name: str = ""
    app_bundle: str = ""
    content_genre: str = ""
    content_rating: str = ""
    content_id: str = ""
    network: str = ""

    # Video placement request
    placement: str = "pre_roll"
    min_duration: int | None = None
    max_duration: int | None = None
    skip_enabled: bool = True

    # Features (for ML)
    age: int | None = None
    gender: str | None = None
    interests: list[str] = field(default_factory=list)
    app_categories: list[str] = field(default_factory=list)
    custom_features: dict[str, Any] = field(default_factory=dict)


@dataclass
class FeatureVector:
    """Feature vector for ML prediction."""

    sparse_features: dict[str, int] = field(default_factory=dict)
    dense_features: list[float] = field(default_factory=list)
    feature_names: list[str] = field(default_factory=list)


@dataclass
class PredictionResult:
    """ML prediction result for video ads."""

    campaign_id: int
    creative_id: int
    pvtr: float = 0.0    # Predicted view-through rate
    pctr: float = 0.0    # Predicted click-through rate
    model_version: str = ""
    latency_ms: float = 0.0


@dataclass
class FrequencyInfo:
    """Frequency control information."""

    user_id: str
    campaign_id: int
    daily_count: int = 0
    hourly_count: int = 0
    daily_cap: int | None = None
    hourly_cap: int | None = None

    @property
    def is_capped(self) -> bool:
        """Check if frequency cap is reached."""
        if self.daily_cap and self.daily_count >= self.daily_cap:
            return True
        if self.hourly_cap and self.hourly_count >= self.hourly_cap:
            return True
        return False


@dataclass
class BudgetInfo:
    """Budget information for CPM campaigns."""

    campaign_id: int
    budget_daily: float | None = None
    budget_total: float | None = None
    spent_today: float = 0.0
    spent_total: float = 0.0

    @property
    def remaining_daily(self) -> float | None:
        """Get remaining daily budget."""
        if self.budget_daily is None:
            return None
        return max(0.0, self.budget_daily - self.spent_today)

    @property
    def remaining_total(self) -> float | None:
        """Get remaining total budget."""
        if self.budget_total is None:
            return None
        return max(0.0, self.budget_total - self.spent_total)

    @property
    def has_budget(self) -> bool:
        """Check if campaign has remaining budget."""
        if self.budget_daily and self.spent_today >= self.budget_daily:
            return False
        if self.budget_total and self.spent_total >= self.budget_total:
            return False
        return True
