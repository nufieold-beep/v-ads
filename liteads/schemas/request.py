"""
API request schemas for CPM CTV and In-App video ad serving.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class DeviceInfo(BaseModel):
    """Device information for CTV and mobile/tablet."""

    device_type: str = Field(
        ..., description="Device type: ctv, mobile, tablet, set_top_box, phone"
    )
    os: str = Field(
        ..., description="Operating system (roku/firetv/tvos/tizen/webos/android/ios)"
    )
    os_version: str | None = Field(None, description="OS version")
    make: str | None = Field(None, description="Device manufacturer (e.g., ROKU, LG, Amazon, Samsung)")
    model: str | None = Field(None, description="Device model (e.g., DIGITAL VIDEO PLAYER, 50UN6950ZUF)")
    brand: str | None = Field(None, description="Device brand (alias for make)")
    screen_width: int | None = Field(None, description="Screen width in pixels")
    screen_height: int | None = Field(None, description="Screen height in pixels")
    language: str | None = Field(None, description="Device language")
    ifa: str | None = Field(None, description="Identifier for advertising (RIDA/AFAI/IDFA/GAID/TIFA/LGUDID)")
    ifa_type: str | None = Field(None, description="IFA type (rida/afai/idfa/gaid/tifa/lgudid/vida)")
    lmt: bool | None = Field(None, description="Limit ad tracking / do not track flag")
    ip: str | None = Field(None, description="IP address")
    ua: str | None = Field(None, description="User-Agent string")
    connection_type: str | None = Field(None, description="Connection type (wifi/ethernet/cellular)")


class GeoInfo(BaseModel):
    """Geographic information."""

    ip: str | None = Field(None, description="IP address")
    country: str | None = Field(None, description="Country code (ISO 3166-1 alpha-2)")
    region: str | None = Field(None, description="Region/Province")
    city: str | None = Field(None, description="City name")
    dma: str | None = Field(None, description="DMA (Designated Market Area) code")
    latitude: float | None = Field(None, description="Latitude")
    longitude: float | None = Field(None, description="Longitude")


class AppInfo(BaseModel):
    """Application/content information for CTV and In-App."""

    app_id: str | None = Field(None, description="App identifier")
    app_name: str | None = Field(None, description="App name (e.g., Pluto TV, Tubi)")
    app_bundle: str | None = Field(None, description="App bundle ID")
    app_version: str | None = Field(None, description="App version")
    store_url: str | None = Field(None, description="App store URL")
    content_genre: str | None = Field(None, description="Content genre (news/sports/entertainment)")
    content_rating: str | None = Field(None, description="Content rating (G/PG/PG-13/R)")
    content_id: str | None = Field(None, description="Content/channel identifier")
    network: str | None = Field(None, description="Network type (wifi/ethernet/4g/5g)")


class VideoPlacementInfo(BaseModel):
    """Video ad placement details."""

    placement: str = Field(
        "pre_roll", description="Placement type: pre_roll, mid_roll, post_roll"
    )
    min_duration: int | None = Field(None, ge=1, description="Min accepted video duration (seconds)")
    max_duration: int | None = Field(None, ge=1, description="Max accepted video duration (seconds)")
    skip_enabled: bool = Field(True, description="Whether skip is allowed")
    player_width: int | None = Field(None, description="Video player width")
    player_height: int | None = Field(None, description="Video player height")
    mimes: list[str] | None = Field(
        None, description="Accepted MIME types (e.g., video/mp4, application/x-mpegURL)"
    )
    protocols: list[int] | None = Field(
        None, description="Supported VAST protocols (2=VAST 2.0, 3=VAST 3.0, 6=VAST 4.0, etc.)"
    )
    width: int | None = Field(None, description="Video width (alias for player_width)")
    height: int | None = Field(None, description="Video height (alias for player_height)")
    # Pod support
    pod_duration: int | None = Field(None, description="Total pod duration (seconds)")
    max_ads_in_pod: int | None = Field(None, description="Maximum ads in the pod")


class UserFeatures(BaseModel):
    """User feature information for ML prediction."""

    age: int | None = Field(None, ge=0, le=120, description="User age")
    gender: str | None = Field(None, description="User gender (male/female/unknown)")
    interests: list[str] | None = Field(None, description="User interests")
    app_categories: list[str] | None = Field(None, description="Installed app categories")
    custom: dict[str, Any] | None = Field(None, description="Custom features")


class AdRequest(BaseModel):
    """Video ad request schema for CTV and In-App environments."""

    request_id: str | None = Field(None, description="Unique request/auction ID (auto-generated if absent)")
    slot_id: str = Field("default", description="Ad slot identifier")
    environment: Literal["ctv", "inapp"] = Field(
        ..., description="Ad environment: ctv (Connected TV) or inapp (In-App mobile/tablet)"
    )
    user_id: str | None = Field(None, description="User identifier (IFA/RIDA/custom)")
    device: DeviceInfo | None = Field(None, description="Device information")
    geo: GeoInfo | None = Field(None, description="Geographic information")
    app: AppInfo | None = Field(None, description="Application/content information")
    video: VideoPlacementInfo = Field(
        default_factory=VideoPlacementInfo, description="Video placement details"
    )
    user_features: UserFeatures | None = Field(None, description="User features for ML")
    num_ads: int = Field(1, ge=1, le=10, description="Number of video ads requested")
    bid_floor: float | None = Field(None, ge=0, description="Minimum CPM bid floor from SSP")

    # Flattened geo fields (populated by OpenRTB service or directly)
    geo_country: str | None = Field(None, description="Country code (ISO 3166-1)")
    geo_region: str | None = Field(None, description="Region/state code")
    geo_dma: str | None = Field(None, description="Nielsen DMA code")

    model_config = {
        "json_schema_extra": {
            "example": {
                "slot_id": "ctv_preroll_main",
                "environment": "ctv",
                "user_id": "rida_abc123",
                "device": {
                    "device_type": "ctv",
                    "os": "roku",
                    "os_version": "12.0",
                    "model": "Roku Ultra",
                    "brand": "Roku",
                    "screen_width": 3840,
                    "screen_height": 2160,
                    "ifa": "rida_abc123",
                    "ifa_type": "rida",
                },
                "geo": {
                    "ip": "1.2.3.4",
                    "country": "US",
                    "dma": "501",
                },
                "app": {
                    "app_id": "com.pluto.tv",
                    "app_name": "Pluto TV",
                    "content_genre": "entertainment",
                },
                "video": {
                    "placement": "pre_roll",
                    "max_duration": 30,
                    "skip_enabled": True,
                    "mimes": ["video/mp4"],
                },
                "num_ads": 1,
            }
        }
    }


class EventRequest(BaseModel):
    """Video event tracking request schema.

    Supports VAST-standard video events plus impression/click.
    """

    request_id: str = Field(..., description="Original ad request ID")
    ad_id: str = Field(..., description="Ad identifier")
    event_type: str = Field(
        ...,
        description="Event type: impression, start, firstQuartile, midpoint, "
                    "thirdQuartile, complete, click, skip, mute, unmute, "
                    "pause, resume, fullscreen, error"
    )
    timestamp: int | None = Field(None, description="Event timestamp (Unix epoch)")
    user_id: str | None = Field(None, description="User identifier")
    environment: Literal["ctv", "inapp"] | None = Field(
        None, description="Ad environment"
    )
    video_position: int | None = Field(
        None, description="Video playback position in seconds when event fired"
    )
    extra: dict[str, Any] | None = Field(None, description="Extra event data")

    model_config = {
        "json_schema_extra": {
            "example": {
                "request_id": "req_abc123",
                "ad_id": "ad_100_200",
                "event_type": "complete",
                "timestamp": 1700000000,
                "user_id": "rida_abc123",
                "environment": "ctv",
                "video_position": 30,
            }
        }
    }
