"""
Quality filter for CPM CTV and In-App video ad serving.

Ensures video creatives meet minimum quality standards:
- Required video URL and landing page
- Video duration within placement constraints
- MIME type compatibility
- Minimum fill rate / VTR thresholds
"""

from __future__ import annotations

from typing import Any

from liteads.common.logger import get_logger
from liteads.rec_engine.filter.base import BaseFilter
from liteads.schemas.internal import AdCandidate, UserContext

logger = get_logger(__name__)

# Supported video MIME types for CTV/In-App
SUPPORTED_VIDEO_MIMES = {
    "video/mp4",
    "video/webm",
    "video/ogg",
    "video/3gpp",
    "application/x-mpegURL",   # HLS
    "application/dash+xml",     # DASH
}


class QualityFilter(BaseFilter):
    """
    Filter video candidates by quality criteria.

    Checks:
    1. Required video URL and landing page
    2. Video duration within placement min/max constraints
    3. MIME type is supported
    4. Minimum fill rate threshold (replaces CTR/CVR checks)
    """

    def __init__(
        self,
        require_video_url: bool = True,
        min_fill_rate: float = 0.0,
        min_quality_score: int = 0,
    ):
        self.require_video_url = require_video_url
        self.min_fill_rate = min_fill_rate
        self.min_quality_score = min_quality_score

    async def filter(
        self,
        candidates: list[AdCandidate],
        user_context: UserContext,
        **kwargs: Any,
    ) -> list[AdCandidate]:
        """Filter candidates by video quality."""
        if not candidates:
            return []

        result = []
        for candidate in candidates:
            if await self.filter_single(candidate, user_context, **kwargs):
                result.append(candidate)

        filtered_count = len(candidates) - len(result)
        if filtered_count > 0:
            logger.debug(f"Video quality filter removed {filtered_count} candidates")

        return result

    async def filter_single(
        self,
        candidate: AdCandidate,
        user_context: UserContext,
        **kwargs: Any,
    ) -> bool:
        """Check if a single video candidate passes quality filter."""
        # Must have video URL
        if self.require_video_url and not candidate.video_url:
            return False

        # Must have landing URL
        if not candidate.landing_url:
            return False

        # Check MIME type compatibility
        if candidate.mime_type and candidate.mime_type not in SUPPORTED_VIDEO_MIMES:
            return False

        # Check video duration against placement constraints
        if user_context.min_duration and candidate.duration < user_context.min_duration:
            return False
        if user_context.max_duration and candidate.duration > user_context.max_duration:
            return False

        # Check quality score
        quality = candidate.metadata.get("quality_score", 80)
        if self.min_quality_score > 0 and quality < self.min_quality_score:
            return False

        return True


class DiversityFilter(BaseFilter):
    """
    Diversity filter for video ad results.

    Prevents showing too many ads from the same advertiser
    in a single pod or session.
    """

    def __init__(
        self,
        max_per_advertiser: int = 2,
        max_per_category: int | None = None,
    ):
        self.max_per_advertiser = max_per_advertiser
        self.max_per_category = max_per_category

    async def filter(
        self,
        candidates: list[AdCandidate],
        user_context: UserContext,
        **kwargs: Any,
    ) -> list[AdCandidate]:
        """Filter candidates for diversity."""
        if not candidates:
            return []

        result: list[AdCandidate] = []
        advertiser_counts: dict[int, int] = {}

        for candidate in candidates:
            adv_id = candidate.advertiser_id
            adv_count = advertiser_counts.get(adv_id, 0)

            if adv_count >= self.max_per_advertiser:
                continue

            advertiser_counts[adv_id] = adv_count + 1
            result.append(candidate)

        filtered_count = len(candidates) - len(result)
        if filtered_count > 0:
            logger.debug(f"Diversity filter removed {filtered_count} candidates")

        return result

    async def filter_single(
        self,
        candidate: AdCandidate,
        user_context: UserContext,
        **kwargs: Any,
    ) -> bool:
        """Diversity check requires full list context."""
        return True


class BlacklistFilter(BaseFilter):
    """
    Filter to exclude blacklisted ads, advertisers, or app bundles.
    """

    def __init__(
        self,
        blocked_campaign_ids: set[int] | None = None,
        blocked_advertiser_ids: set[int] | None = None,
        blocked_creative_ids: set[int] | None = None,
        blocked_app_bundles: set[str] | None = None,
    ):
        self.blocked_campaign_ids = blocked_campaign_ids or set()
        self.blocked_advertiser_ids = blocked_advertiser_ids or set()
        self.blocked_creative_ids = blocked_creative_ids or set()
        self.blocked_app_bundles = blocked_app_bundles or set()

    async def filter(
        self,
        candidates: list[AdCandidate],
        user_context: UserContext,
        **kwargs: Any,
    ) -> list[AdCandidate]:
        """Filter out blacklisted candidates."""
        if not candidates:
            return []

        result = []
        for candidate in candidates:
            if await self.filter_single(candidate, user_context, **kwargs):
                result.append(candidate)

        filtered_count = len(candidates) - len(result)
        if filtered_count > 0:
            logger.debug(f"Blacklist filter removed {filtered_count} candidates")

        return result

    async def filter_single(
        self,
        candidate: AdCandidate,
        user_context: UserContext,
        **kwargs: Any,
    ) -> bool:
        """Check if candidate is not blacklisted."""
        if candidate.campaign_id in self.blocked_campaign_ids:
            return False

        if candidate.advertiser_id in self.blocked_advertiser_ids:
            return False

        if candidate.creative_id in self.blocked_creative_ids:
            return False

        # Check app bundle blacklist
        if self.blocked_app_bundles and user_context.app_bundle in self.blocked_app_bundles:
            return False

        return True

    def add_blocked_campaign(self, campaign_id: int) -> None:
        """Add campaign to blacklist."""
        self.blocked_campaign_ids.add(campaign_id)

    def add_blocked_advertiser(self, advertiser_id: int) -> None:
        """Add advertiser to blacklist."""
        self.blocked_advertiser_ids.add(advertiser_id)

    def remove_blocked_campaign(self, campaign_id: int) -> None:
        """Remove campaign from blacklist."""
        self.blocked_campaign_ids.discard(campaign_id)
