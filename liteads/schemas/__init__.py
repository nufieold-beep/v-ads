"""
Pydantic schemas for CPM CTV & In-App Video API requests and responses.
Includes OpenRTB 2.6 bid request / response models.
"""

from liteads.schemas.internal import (
    AdCandidate,
    BudgetInfo,
    FeatureVector,
    FrequencyInfo,
    PredictionResult,
    UserContext,
)
from liteads.schemas.openrtb import (
    App as ORTBApp,
    Bid,
    BidRequest,
    BidResponse,
    Device as ORTBDevice,
    Geo as ORTBGeo,
    Imp,
    NoBidReason,
    SeatBid,
    Video as ORTBVideo,
)
from liteads.schemas.request import (
    AdRequest,
    AppInfo,
    DeviceInfo,
    EventRequest,
    GeoInfo,
    UserFeatures,
    VideoPlacementInfo,
)
from liteads.schemas.response import (
    AdListResponse,
    AdResponse,
    ErrorResponse,
    EventResponse,
    HealthResponse,
    VideoCreativeResponse,
    VideoTrackingUrls,
)

__all__ = [
    # Request schemas
    "AdRequest",
    "EventRequest",
    "DeviceInfo",
    "GeoInfo",
    "AppInfo",
    "VideoPlacementInfo",
    "UserFeatures",
    # Response schemas
    "AdResponse",
    "AdListResponse",
    "EventResponse",
    "HealthResponse",
    "ErrorResponse",
    "VideoCreativeResponse",
    "VideoTrackingUrls",
    # Internal schemas
    "AdCandidate",
    "UserContext",
    "FeatureVector",
    "PredictionResult",
    "FrequencyInfo",
    "BudgetInfo",
    # OpenRTB 2.6
    "BidRequest",
    "BidResponse",
    "Bid",
    "SeatBid",
    "Imp",
    "ORTBVideo",
    "ORTBDevice",
    "ORTBGeo",
    "ORTBApp",
    "NoBidReason",
]
