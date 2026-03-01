"""
API response schemas for CPM CTV and In-App video ad serving.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class VideoCreativeResponse(BaseModel):
    """Video creative response schema."""

    title: str | None = Field(None, description="Ad title")
    description: str | None = Field(None, description="Ad description")
    video_url: str = Field(..., description="Video file URL (MP4/HLS/DASH)")
    vast_url: str | None = Field(None, description="VAST tag URL")
    companion_image_url: str | None = Field(None, description="Companion banner image URL")
    landing_url: str = Field(..., description="Landing page URL")
    duration: int = Field(..., description="Video duration in seconds")
    width: int = Field(..., description="Video width in pixels")
    height: int = Field(..., description="Video height in pixels")
    bitrate: int | None = Field(None, description="Video bitrate in kbps")
    mime_type: str = Field("video/mp4", description="Video MIME type")
    creative_type: str = Field(..., description="Creative type (ctv_video/inapp_video)")
    skippable: bool = Field(True, description="Whether the ad can be skipped")
    skip_after: int = Field(5, description="Seconds before skip button appears")


class VideoTrackingUrls(BaseModel):
    """VAST-standard tracking URLs for video ad events."""

    impression_url: str = Field(..., description="URL to call on impression")
    start_url: str = Field(..., description="URL to call on video start")
    first_quartile_url: str = Field(..., description="URL to call at 25% viewed")
    midpoint_url: str = Field(..., description="URL to call at 50% viewed")
    third_quartile_url: str = Field(..., description="URL to call at 75% viewed")
    complete_url: str = Field(..., description="URL to call at 100% viewed")
    click_url: str = Field(..., description="URL to call on click-through")
    skip_url: str | None = Field(None, description="URL to call on skip")
    mute_url: str | None = Field(None, description="URL to call on mute")
    unmute_url: str | None = Field(None, description="URL to call on unmute")
    pause_url: str | None = Field(None, description="URL to call on pause")
    resume_url: str | None = Field(None, description="URL to call on resume")
    error_url: str | None = Field(None, description="URL to call on error")


class AdResponse(BaseModel):
    """Single video ad response schema."""

    ad_id: str = Field(..., description="Ad identifier")
    campaign_id: int = Field(..., description="Campaign identifier")
    creative_id: int = Field(..., description="Creative identifier")
    creative: VideoCreativeResponse = Field(..., description="Video creative content")
    tracking: VideoTrackingUrls = Field(..., description="Video tracking URLs")
    environment: str = Field(..., description="Ad environment (ctv/inapp)")
    cpm: float = Field(..., description="CPM bid amount")
    metadata: dict[str, Any] | None = Field(None, description="Additional metadata")


class AdListResponse(BaseModel):
    """Video ad list response schema."""

    request_id: str = Field(..., description="Request identifier")
    ads: list[AdResponse] = Field(default_factory=list, description="List of video ads")
    count: int = Field(..., description="Number of ads returned")
    environment: str = Field(..., description="Ad environment (ctv/inapp)")

    model_config = {
        "json_schema_extra": {
            "example": {
                "request_id": "req_abc123",
                "ads": [
                    {
                        "ad_id": "ad_100_200",
                        "campaign_id": 100,
                        "creative_id": 200,
                        "creative": {
                            "title": "Brand Video Ad",
                            "video_url": "https://cdn.example.com/video/ad_200.mp4",
                            "landing_url": "https://example.com/landing",
                            "duration": 30,
                            "width": 1920,
                            "height": 1080,
                            "mime_type": "video/mp4",
                            "creative_type": "ctv_video",
                            "skippable": True,
                            "skip_after": 5,
                        },
                        "tracking": {
                            "impression_url": "https://api.liteads.com/api/v1/event/track?type=impression&req=req_abc123&ad=ad_100_200",
                            "start_url": "https://api.liteads.com/api/v1/event/track?type=start&req=req_abc123&ad=ad_100_200",
                            "first_quartile_url": "https://api.liteads.com/api/v1/event/track?type=firstQuartile&req=req_abc123&ad=ad_100_200",
                            "midpoint_url": "https://api.liteads.com/api/v1/event/track?type=midpoint&req=req_abc123&ad=ad_100_200",
                            "third_quartile_url": "https://api.liteads.com/api/v1/event/track?type=thirdQuartile&req=req_abc123&ad=ad_100_200",
                            "complete_url": "https://api.liteads.com/api/v1/event/track?type=complete&req=req_abc123&ad=ad_100_200",
                            "click_url": "https://api.liteads.com/api/v1/event/track?type=click&req=req_abc123&ad=ad_100_200",
                        },
                        "environment": "ctv",
                        "cpm": 15.0,
                    }
                ],
                "count": 1,
                "environment": "ctv",
            }
        }
    }


class EventResponse(BaseModel):
    """Event tracking response schema."""

    success: bool = Field(..., description="Whether the event was recorded")
    message: str | None = Field(None, description="Optional message")


class HealthResponse(BaseModel):
    """Health check response schema."""

    status: str = Field(..., description="Service status")
    version: str = Field(..., description="Service version")
    database: bool = Field(..., description="Database connection status")
    redis: bool = Field(..., description="Redis connection status")


class ErrorResponse(BaseModel):
    """Error response schema."""

    error: str = Field(..., description="Error type")
    message: str = Field(..., description="Error message")
    details: dict[str, Any] | None = Field(None, description="Error details")
    request_id: str | None = Field(None, description="Request identifier")

    model_config = {
        "json_schema_extra": {
            "example": {
                "error": "validation_error",
                "message": "Invalid request parameters",
                "details": {"slot_id": "This field is required"},
                "request_id": "req_abc123",
            }
        }
    }
