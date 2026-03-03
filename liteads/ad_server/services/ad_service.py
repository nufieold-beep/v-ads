"""
Video ad serving service for CPM CTV and In-App.

Handles the core video ad serving logic using the recommendation engine.
All campaigns are CPM-only; creative types are CTV_VIDEO and INAPP_VIDEO.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from liteads.common.config import get_settings
from liteads.common.logger import get_logger
from liteads.common.utils import hash_user_id
from liteads.rec_engine import RecommendationConfig, RecommendationEngine
from liteads.schemas.internal import AdCandidate, UserContext
from liteads.schemas.request import AdRequest

logger = get_logger(__name__)

# Module-level config singleton (immutable, created once)
_REC_CONFIG = RecommendationConfig(
    max_retrieval=200,
    enable_budget_filter=True,
    enable_frequency_filter=True,
    enable_quality_filter=True,
)


class AdService:
    """CPM video ad serving service for CTV and In-App environments."""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.settings = get_settings()
        self._engine: RecommendationEngine | None = None

    @property
    def engine(self) -> RecommendationEngine:
        """Lazy initialization of recommendation engine."""
        if self._engine is None:
            self._engine = RecommendationEngine(
                session=self.session,
                config=_REC_CONFIG,
            )
        return self._engine

    async def serve_ads(
        self,
        request: AdRequest,
        request_id: str,
    ) -> list[AdCandidate]:
        """
        Main video ad serving method.

        Pipeline:
        1. Retrieve candidate video ads matching environment (CTV/INAPP)
        2. Filter by targeting, budget, frequency, video requirements
        3. Rank by expected revenue (CPM × pVTR × quality)
        4. Apply bid-floor filtering (SSP-specified floor)
        5. Return top candidates with video tracking URLs

        The bid floor is propagated into the ranking module so that
        the ranker can factor it into score calculations and the
        downstream auction can use it for clearing-price computation.
        """
        user_context = self._build_user_context(request)

        candidates, metrics = await self.engine.recommend(
            user_context=user_context,
            slot_id=request.slot_id,
            num_ads=request.num_ads,
        )

        # ── Post-selection bid floor enforcement ────────────────────
        bid_floor = request.bid_floor if request.bid_floor and request.bid_floor > 0 else 0.0
        if bid_floor > 0:
            before = len(candidates)
            candidates = [c for c in candidates if c.bid >= bid_floor]
            if len(candidates) < before:
                logger.info(
                    "Bid floor filter applied",
                    request_id=request_id,
                    bid_floor=bid_floor,
                    removed=before - len(candidates),
                )

        logger.debug(
            "Video ad serving completed",
            request_id=request_id,
            environment=request.environment,
            retrieval_count=metrics.retrieval_count,
            final_count=metrics.final_count,
            total_ms=round(metrics.total_ms, 2),
        )

        return candidates

    def _build_user_context(self, request: AdRequest) -> UserContext:
        """Build user context from video ad request."""
        ctx = UserContext(
            user_id=request.user_id,
            user_hash=hash_user_id(request.user_id) if request.user_id else 0,
            environment=request.environment,
        )

        # Device info
        if request.device:
            ctx.device_type = request.device.device_type or ""
            ctx.os = request.device.os
            ctx.os_version = request.device.os_version or ""
            ctx.device_model = request.device.model or ""
            ctx.device_brand = request.device.brand or ""
            ctx.ifa = request.device.ifa
            ctx.ifa_type = request.device.ifa_type

        # Geo info
        if request.geo:
            ctx.ip = request.geo.ip or ""
            ctx.country = request.geo.country or ""
            ctx.region = request.geo.region or ""
            ctx.city = request.geo.city or ""
            ctx.dma = request.geo.dma or ""
            ctx.latitude = request.geo.latitude
            ctx.longitude = request.geo.longitude

        # App/content info
        if request.app:
            ctx.app_id = request.app.app_id or ""
            ctx.app_name = request.app.app_name or ""
            ctx.app_bundle = request.app.app_bundle or ""
            ctx.content_genre = request.app.content_genre or ""
            ctx.content_rating = request.app.content_rating or ""
            ctx.content_id = request.app.content_id or ""
            ctx.network = request.app.network_name or ""

        # Video placement info
        if request.video:
            ctx.placement = request.video.placement
            ctx.min_duration = request.video.min_duration
            ctx.max_duration = request.video.max_duration
            ctx.skip_enabled = request.video.skip_enabled

        # User features
        if request.user_features:
            ctx.age = request.user_features.age
            ctx.gender = request.user_features.gender
            ctx.interests = request.user_features.interests or []
            ctx.app_categories = request.user_features.app_categories or []
            ctx.custom_features = request.user_features.custom or {}

        return ctx
