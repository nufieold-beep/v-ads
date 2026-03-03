"""
Video event tracking service for CPM CTV and In-App.

Handles recording VAST-standard video events and CPM-based billing.
Cost is calculated and recorded on IMPRESSION events using CPM bid / 1000.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from liteads.common.cache import CacheKeys, redis_client
from liteads.common.logger import get_logger
from liteads.common.utils import ENV_TO_INT, current_date, current_hour
from liteads.models import AdEvent, Campaign, EventType
from liteads.ad_server.middleware.metrics import (
    record_ad_completion,
    record_ad_skip,
    record_ad_start,
    record_quartile,
    record_vast_error,
)

logger = get_logger(__name__)

# ── Module-level constants (avoid re-creation per call) ──────────────────
_DECIMAL_1000 = Decimal("1000")
_DECIMAL_ZERO = Decimal("0.000000")
_EVENT_TYPE_MAP: dict[str, int] = {
    # Core VAST events
    "impression": EventType.IMPRESSION,
    "imp": EventType.IMPRESSION,
    "start": EventType.START,
    "firstquartile": EventType.FIRST_QUARTILE,
    "first_quartile": EventType.FIRST_QUARTILE,
    "midpoint": EventType.MIDPOINT,
    "thirdquartile": EventType.THIRD_QUARTILE,
    "third_quartile": EventType.THIRD_QUARTILE,
    "complete": EventType.COMPLETE,
    "click": EventType.CLICK,
    "skip": EventType.SKIP,
    "mute": EventType.MUTE,
    "unmute": EventType.UNMUTE,
    "pause": EventType.PAUSE,
    "resume": EventType.RESUME,
    "fullscreen": EventType.FULLSCREEN,
    "error": EventType.ERROR,
    # Extended VAST events
    "close": EventType.CLOSE,
    "acceptinvitation": EventType.ACCEPT_INVITATION,
    "accept_invitation": EventType.ACCEPT_INVITATION,
    "exitfullscreen": EventType.EXIT_FULLSCREEN,
    "exit_fullscreen": EventType.EXIT_FULLSCREEN,
    "expand": EventType.EXPAND,
    "collapse": EventType.COLLAPSE,
    "rewind": EventType.REWIND,
    "progress": EventType.PROGRESS,
    "loaded": EventType.LOADED,
    "creativeview": EventType.CREATIVE_VIEW,
    "creative_view": EventType.CREATIVE_VIEW,
    # OpenRTB auction events
    "loss": EventType.LOSS,
    "win": EventType.WIN,
}

_STAT_FIELD_MAP: dict[int, str] = {
    EventType.IMPRESSION: "impressions",
    EventType.START: "starts",
    EventType.FIRST_QUARTILE: "first_quartiles",
    EventType.MIDPOINT: "midpoints",
    EventType.THIRD_QUARTILE: "third_quartiles",
    EventType.COMPLETE: "completions",
    EventType.CLICK: "clicks",
    EventType.SKIP: "skips",
}


class EventService:
    """Video event tracking service with CPM billing."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def track_event(
        self,
        request_id: str,
        ad_id: str,
        event_type: str,
        user_id: str | None = None,
        timestamp: int | None = None,
        environment: str | None = None,
        video_position: int | None = None,
        extra: dict[str, Any] | None = None,
        ip_address: str | None = None,
        win_price: float = 0.0,
        adomain: str | None = None,
        source_name: str | None = None,
        bundle_id: str | None = None,
        country_code: str | None = None,
    ) -> bool:
        """
        Track a video ad event.

        Events are:
        1. Persisted to database for billing/reporting
        2. Cached in Redis for real-time stats
        3. Used for frequency control updates (on impression)
        4. CPM cost is charged on IMPRESSION events
        """
        try:
            campaign_id, creative_id = self._parse_ad_id(ad_id)

            event_type_enum = self._get_event_type(event_type)
            if event_type_enum is None:
                logger.warning(f"Unknown video event type: {event_type}")
                return False

            # Resolve environment to int (shared mapping)
            env_int = ENV_TO_INT.get(environment) if environment else None

            # ---- Impression deduplication ----
            # Prevent double-billing when both burl and VAST pixel fire
            # for the same (request_id, campaign_id) pair.
            is_dedup = False
            if event_type_enum == EventType.IMPRESSION and campaign_id is not None:
                dedup_key = f"imp_dedup:{request_id}:{campaign_id}"
                is_new = await redis_client.set(
                    dedup_key, "1", ttl=3600, nx=True,
                )
                # is_new == True  → key was just created (first impression)
                # is_new == False → key already existed  (duplicate)
                if not is_new:
                    # Impression already recorded — still persist event for
                    # audit trail but skip billing and counter increments.
                    logger.info(
                        "Duplicate impression suppressed",
                        request_id=request_id,
                        campaign_id=campaign_id,
                    )
                    is_dedup = True

            # Calculate CPM cost on impression (skip for deduplicated events)
            cost = _DECIMAL_ZERO
            if not is_dedup and event_type_enum == EventType.IMPRESSION and campaign_id is not None:
                if campaign_id > 0:
                    # Local campaign — look up CPM from DB
                    cost = await self._calculate_cpm_cost(campaign_id)
                elif win_price > 0:
                    # Demand fill — cost = bid_price / 1000 (CPM → per-impression)
                    cost = Decimal(win_price) / Decimal(1000)

            # Build & persist AdEvent (single construction for both paths)
            event = AdEvent(
                request_id=request_id,
                campaign_id=campaign_id,
                creative_id=creative_id,
                event_type=event_type_enum,
                event_time=datetime.fromtimestamp(timestamp, tz=timezone.utc)
                if timestamp
                else datetime.now(timezone.utc),
                user_id=user_id,
                ip_address=ip_address,
                cost=cost,
                win_price=Decimal(str(round(win_price, 6))) if win_price else Decimal("0"),
                adomain=adomain,
                source_name=source_name,
                bundle_id=bundle_id,
                country_code=country_code,
                video_position=video_position,
                environment=env_int,
            )
            self.session.add(event)
            await self.session.flush()

            if is_dedup:
                return True

            # ── Batch all Redis updates into a single pipeline ─────
            # Previously 3-5 separate round-trips; now 1.
            hour = current_hour()
            stat_key = CacheKeys.stat_hourly(campaign_id, hour) if campaign_id is not None else None

            pipe = redis_client.pipeline()

            # 1. Real-time stat counter
            if stat_key:
                field_name = _STAT_FIELD_MAP.get(
                    EventType(event_type_enum) if event_type_enum in EventType else event_type_enum  # type: ignore[arg-type]
                )
                if field_name:
                    pipe.hincrby(stat_key, field_name, 1)
                if event_type_enum == EventType.WIN and win_price > 0:
                    pipe.hincrby(stat_key, "wins", 1)
                    pipe.hincrbyfloat(stat_key, "win_price_sum", win_price)
                pipe.expire(stat_key, 48 * 3600)

            # 2. Budget spend + hourly spend (impression only)
            if event_type_enum == EventType.IMPRESSION and campaign_id is not None:
                if cost > 0:
                    today = current_date()
                    budget_key = f"budget:{campaign_id}:{today}"
                    pipe.hincrbyfloat(budget_key, "spent_today", float(cost))
                    pipe.hincrbyfloat(budget_key, "spent_total", float(cost))
                    pipe.expire(budget_key, 86400 * 2)
                    if stat_key:
                        pipe.hincrbyfloat(stat_key, "spend", float(cost))

            # 3. Frequency counter (impression + has user_id)
            if event_type_enum == EventType.IMPRESSION and user_id and campaign_id is not None:
                today = current_date()
                daily_key = CacheKeys.freq_daily(user_id, campaign_id, today)
                hourly_key = CacheKeys.freq_hourly(user_id, campaign_id, hour)
                pipe.incr(daily_key)
                pipe.expire(daily_key, 24 * 3600)
                pipe.incr(hourly_key)
                pipe.expire(hourly_key, 3600)

            await pipe.execute()

            # ── Prometheus delivery-health metrics ─────────────────
            cid_str = str(campaign_id) if campaign_id else "unknown"
            try:
                if event_type_enum == EventType.ERROR:
                    err_code = (extra or {}).get("error_code", "unknown")
                    record_vast_error(str(err_code), cid_str)
                elif event_type_enum == EventType.IMPRESSION:
                    record_quartile("impression", cid_str)
                elif event_type_enum == EventType.START:
                    record_ad_start(cid_str)
                    record_quartile("start", cid_str)
                elif event_type_enum == EventType.FIRST_QUARTILE:
                    record_quartile("firstQuartile", cid_str)
                elif event_type_enum == EventType.MIDPOINT:
                    record_quartile("midpoint", cid_str)
                elif event_type_enum == EventType.THIRD_QUARTILE:
                    record_quartile("thirdQuartile", cid_str)
                elif event_type_enum == EventType.COMPLETE:
                    record_ad_completion(cid_str)
                    record_quartile("complete", cid_str)
                elif event_type_enum == EventType.SKIP:
                    record_ad_skip(cid_str)
            except Exception:
                pass  # Metrics must never break event tracking

            logger.debug(
                "Video event tracked",
                event_type=event_type,
                campaign_id=campaign_id,
                cost=str(cost),
            )

            return True

        except Exception as e:
            logger.error(f"Failed to track video event: {e}")
            # Rollback session so the context-manager commit doesn't hit
            # PendingRollbackError and blow up the request lifecycle.
            try:
                await self.session.rollback()
            except Exception:
                pass
            return False

    def _parse_ad_id(self, ad_id: str) -> tuple[int | None, int | None]:
        """Parse ad ID to extract campaign and creative IDs.

        For demand fills, campaign_id is 0 and creative_id is a random hash.
        We store campaign_id=0 in the DB (FK constraints removed) and keep
        the creative hash for correlation with demand partner reporting.
        """
        try:
            parts = ad_id.split("_")
            if len(parts) >= 3:
                cid = int(parts[1])
                crid = int(parts[2])
                return cid if cid >= 0 else None, crid
            elif len(parts) >= 2:
                cid = int(parts[1])
                return cid if cid >= 0 else None, None
            else:
                return int(ad_id), None
        except (ValueError, IndexError):
            logger.warning(f"Invalid ad_id format: {ad_id}")
            return None, None

    def _get_event_type(self, event_type: str) -> int | None:
        """Convert VAST event type string to enum.

        Supports all VAST 2.x–4.x event names, plus OpenRTB loss.
        """
        return _EVENT_TYPE_MAP.get(event_type.lower())

    async def _calculate_cpm_cost(self, campaign_id: int) -> Decimal:
        """Calculate CPM cost per impression.

        CPM cost per impression = bid_amount / 1000
        """
        try:
            # Try cache first
            cache_key = f"campaign:cpm:{campaign_id}"
            cached_cpm = await redis_client.get(cache_key)
            if cached_cpm:
                return Decimal(cached_cpm) / _DECIMAL_1000

            # Fall back to DB
            result = await self.session.execute(
                select(Campaign.bid_amount).where(Campaign.id == campaign_id)
            )
            bid_amount = result.scalar()
            if bid_amount:
                # Cache the CPM bid for 5 minutes
                await redis_client.set(cache_key, str(bid_amount), ttl=300)
                return Decimal(str(bid_amount)) / _DECIMAL_1000

            return _DECIMAL_ZERO
        except Exception as e:
            logger.warning(f"Failed to calculate CPM cost for campaign {campaign_id}: {e}")
            return Decimal("0.000000")

    # ------------------------------------------------------------------
    # Ad request / opportunity tracking (called from router layer)
    # ------------------------------------------------------------------

    @staticmethod
    async def track_ad_request(campaign_ids: list[int] | None = None) -> None:
        """Increment ad_requests counter in Redis.

        Called once per incoming bid request. If *campaign_ids* is None the
        counter is incremented on a global key; otherwise on each campaign
        that was a candidate.
        """
        hour = current_hour()
        if campaign_ids:
            pipe = redis_client.pipeline()
            for cid in campaign_ids:
                key = CacheKeys.stat_hourly(cid, hour)
                pipe.hincrby(key, "ad_requests", 1)
                pipe.expire(key, 48 * 3600)
            await pipe.execute()
        else:
            key = CacheKeys.stat_hourly(0, hour)  # global
            pipe = redis_client.pipeline()
            pipe.hincrby(key, "ad_requests", 1)
            pipe.expire(key, 48 * 3600)
            await pipe.execute()

    @staticmethod
    async def track_ad_opportunity(campaign_ids: list[int]) -> None:
        """Increment ad_opportunities for each campaign that filled.

        Called after the pipeline returns candidates — each candidate
        represents one ad opportunity.
        """
        hour = current_hour()
        if not campaign_ids:
            return
        pipe = redis_client.pipeline()
        for cid in campaign_ids:
            key = CacheKeys.stat_hourly(cid, hour)
            pipe.hincrby(key, "ad_opportunities", 1)
            pipe.expire(key, 48 * 3600)
        await pipe.execute()
