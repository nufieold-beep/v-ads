"""
Video event tracking service for CPM CTV and In-App.

Handles recording VAST-standard video events and CPM-based billing.
Events are persisted to PostgreSQL and aggregated in Redis pipelines for
real-time dashboard analytics, without blocking ad delivery paths.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import redis.asyncio as redis
from redis.exceptions import RedisError

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

# ── Module-level constants ───────────────────────────────────────────────
_DECIMAL_1000 = Decimal("1000")
_DECIMAL_ZERO = Decimal("0.000000")
_CACHE_TTL_STATS = 48 * 3600       # 48 hours for hourly statistics retention
_CACHE_TTL_BUDGET = 2 * 86400      # 2 days for budget tracking
_CACHE_TTL_DEDUP = 3600            # 1 hour for impression deduplication

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
    EventType.WIN: "wins",
    EventType.LOSS: "losses",
    EventType.ERROR: "errors",
}


class EventService:
    """
    Video event tracking service with robust failure handling and CPM billing logic.
    Maintains clean pathways between database persistence and real-time cache counters
    to feed accurately into dashboard analytics.
    """

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
        video_position: Any | None = None,
        extra: dict[str, Any] | None = None,
        ip_address: str | None = None,
        win_price: float = 0.0,
        adomain: str | None = None,
        source_name: str | None = None,
        bundle_id: str | None = None,
        country_code: str | None = None,
    ) -> bool:
        """
        Main entry point for tracking an ad event.
        Guarantees that database persistence will not fail if Redis services drop.
        """
        try:
            # 1. Parsing & Sanitization
            campaign_id, creative_id = self._parse_ad_id(ad_id)
            event_type_enum = self._get_event_type(event_type)
            
            if event_type_enum is None:
                logger.warning(f"Unknown video event type ignored: {event_type}")
                return False

            env_int = ENV_TO_INT.get(environment) if environment else None
            safe_video_position = self._sanitize_video_position(video_position)
            
            # Format win price accurately for DB representation
            safe_win_price = Decimal(str(round(win_price, 6))) if win_price else _DECIMAL_ZERO
            event_time = datetime.fromtimestamp(timestamp, tz=timezone.utc) if timestamp else datetime.now(timezone.utc)

            # 2. Impression Deduplication (Safely falls back if Redis fails)
            is_dedup = await self._is_duplicate_impression(
                request_id, campaign_id, event_type_enum
            )

            # 3. Analytics Accounting (Cost) - Uses DB or Redis Safely
            cost = await self._calculate_event_cost(
                event_type_enum, campaign_id, win_price, is_dedup
            )
            
            # 4. PostgreSQL Database Persistence
            try:
                await self._persist_event_to_db(
                    request_id=request_id,
                    campaign_id=campaign_id,
                    creative_id=creative_id,
                    event_type=event_type_enum,
                    event_time=event_time,
                    user_id=user_id,
                    ip_address=ip_address,
                    cost=cost,
                    win_price=safe_win_price,
                    adomain=adomain,
                    source_name=source_name,
                    bundle_id=bundle_id,
                    country_code=country_code,
                    video_position=safe_video_position,
                    environment=env_int,
                )
            except Exception as db_err:
                # Strictly isolate DB rollback so it does not destroy healthy Redis states if reversed
                logger.error(f"Failed to persist video event to database: {db_err}", exc_info=True)
                await self.session.rollback()
                return False

            # We persist duplicates for auditing, but skip dashboard counters/billing modifications
            if is_dedup:
                return True

            # 5. Redis Dashboard Analytics & Spend Pipelining
            try:
                await self._update_redis_analytics_pipeline(
                    campaign_id=campaign_id,
                    event_type_enum=event_type_enum,
                    user_id=user_id,
                    cost=cost,
                    win_price=win_price,
                )
            except Exception as redis_err:
                # If Redis analytics fail, we catch so it does not trigger the HTTP dependency 
                # to rollback the database transaction (which just succeeded).
                logger.error(f"Redis pipeline update failed for event: {redis_err}")
                # We do not rollback DB here! 
                
            # 6. Prometheus Export (In-memory UDP, non-blocking)
            self._update_prometheus_metrics(campaign_id, event_type_enum, extra)

            logger.debug(
                "Video event tracked",
                event_type=event_type,
                campaign_id=campaign_id,
                cost=str(cost),
            )
            return True

        except Exception as e:
            logger.error(f"Critical failure globally processing event {event_type}: {e}", exc_info=True)
            return False

    # ──────────────────────────────────────────────────────────────────
    # Helper Sub-Methods
    # ──────────────────────────────────────────────────────────────────

    def _sanitize_video_position(self, video_position: Any) -> int | None:
        """
        Safety converter: ensures video_position is castable to integer.
        Mitigates crash when VAST macros inject string values like 'ctv'.
        """
        if video_position is None:
            return None
        try:
            return int(video_position)
        except (ValueError, TypeError):
            return None

    def _parse_ad_id(self, ad_id: str) -> tuple[int | None, int | None]:
        """
        Safely extracts campaign_id and creative_id without tripping PostgreSQL
        ForeignKey exceptions when arbitrary DSP strings map over tracking links.
        """
        if not ad_id:
            return None, None

        parts = ad_id.split("_")
        try:
            if len(parts) >= 3:
                # Format: ad_{campaign_id}_{creative_id}
                cid = self._safe_int(parts[1])
                crid = self._safe_int(parts[2])
                
                # DSP fills natively bypass internal DB creatives, cid is usually 0
                if cid == 0:
                    crid = None
                    
                return (cid if cid is not None and cid >= 0 else None, crid)
                
            elif len(parts) >= 2:
                # Format: ad_{campaign_id}
                cid = self._safe_int(parts[1])
                return (cid if cid is not None and cid >= 0 else None, None)
                
            else:
                # Format: {raw_id}
                cid = self._safe_int(ad_id)
                return (cid, None) if cid is not None else (0, None)
                
        except Exception as e:
            logger.warning(f"Failed to cleanly parse ad_id '{ad_id}': {e}")
            return None, None

    def _safe_int(self, value: Any) -> int | None:
        """Convert arbitrary input to integer safely."""
        try:
            return int(value)
        except (ValueError, TypeError):
            return None

    def _get_event_type(self, event_type: str) -> int | None:
        """Convert standard VAST strings to internal system EventType mappings."""
        if not event_type:
            return None
        return _EVENT_TYPE_MAP.get(event_type.lower())

    async def _is_duplicate_impression(
        self, request_id: str, campaign_id: int | None, event_type_enum: int
    ) -> bool:
        """
        Prevents double-billing for when BURL and IMP pixels naturally hit identically.
        Falls back to 'False' safely if Redis connection drops.
        """
        if event_type_enum != EventType.IMPRESSION or campaign_id is None:
            return False

        try:
            dedup_key = f"imp_dedup:{request_id}:{campaign_id}"
            is_new = await redis_client.set(dedup_key, "1", ttl=_CACHE_TTL_DEDUP, nx=True)
            
            # is_new = True indicates fresh key creation.
            # is_new = False indicates it just collided
            if not is_new:
                logger.info("Duplicate impression suppressed", request_id=request_id, campaign_id=campaign_id)
                return True
        except Exception as e:
            logger.warning(f"Dedup check bypassed due to Redis unreachability: {e}")

        return False

    async def _calculate_event_cost(
        self, 
        event_type_enum: int, 
        campaign_id: int | None, 
        win_price: float, 
        is_dedup: bool
    ) -> Decimal:
        """Calculate the precise strict Decimal CPM cost if actionable."""
        if is_dedup or event_type_enum != EventType.IMPRESSION or campaign_id is None:
            return _DECIMAL_ZERO
            
        if campaign_id > 0:
            # Local campaign -> database reference
            return await self._calculate_cpm_cost(campaign_id)
        elif win_price > 0:
            # Third-party programmatic fill -> dynamic price
            return Decimal(str(win_price)) / _DECIMAL_1000
            
        return _DECIMAL_ZERO

    async def _calculate_cpm_cost(self, campaign_id: int) -> Decimal:
        """Fetch local campaign DB bid amounts specifically cached into Redis safely."""
        cache_key = f"campaign:cpm:{campaign_id}"
        
        try:
            cached_cpm = await redis_client.get(cache_key)
            if cached_cpm:
                val_str = cached_cpm.decode('utf-8') if isinstance(cached_cpm, bytes) else str(cached_cpm)
                return Decimal(val_str) / _DECIMAL_1000
        except Exception as e:
            logger.warning(f"Redis campaign bid read failed: {e}")

        # Fall back to PostgreSQL DB resolving single record scalar
        try:
            result = await self.session.execute(
                select(Campaign.bid_amount).where(Campaign.id == campaign_id)
            )
            bid_amount = result.scalar()
            
            if bid_amount is not None:
                try:
                    await redis_client.set(cache_key, str(bid_amount), ttl=300)
                except Exception:
                    pass # Ignore redis cache set failure 
                return Decimal(str(bid_amount)) / _DECIMAL_1000
        except Exception as e:
            logger.error(f"Postgres execution failed extracting campaign bid: {e}")

        return _DECIMAL_ZERO

    async def _persist_event_to_db(self, **kwargs: Any) -> None:
        """Injects `AdEvent` dynamically mapped to Postgres layer."""
        event = AdEvent(**kwargs)
        self.session.add(event)
        await self.session.flush()

    async def _update_redis_analytics_pipeline(
        self, 
        campaign_id: int | None, 
        event_type_enum: int, 
        user_id: str | None, 
        cost: Decimal, 
        win_price: float
    ) -> None:
        """
        Executes robust clustered hash updates dynamically powering the Analytics Dashboard.
        Called within exception handling wrappers implicitly.
        """
        hour = current_hour()
        stat_key = CacheKeys.stat_hourly(campaign_id, hour) if campaign_id is not None else None
        
        pipe = redis_client.pipeline()

        # 1. Update Core Statistical Dashboard Counters Map
        if stat_key:
            _enum_val = EventType(event_type_enum) if event_type_enum in EventType else event_type_enum  # type: ignore[arg-type]
            field_name = _STAT_FIELD_MAP.get(_enum_val)
            
            if field_name:
                pipe.hincrby(stat_key, field_name, 1)
                
            # Extra WIN context mapping
            if event_type_enum == EventType.WIN and win_price > 0:
                # Add check if it doesn't double run "wins" if it was already caught in field_name
                # (EventType.WIN is already 'wins' in map, avoid duplicate injection)
                if field_name != "wins":
                    pipe.hincrby(stat_key, "wins", 1)
                pipe.hincrbyfloat(stat_key, "win_price_sum", float(win_price))
                
            pipe.expire(stat_key, _CACHE_TTL_STATS)

        # 2. Append Cost Accruals (Spend ledger logic)
        if event_type_enum == EventType.IMPRESSION and campaign_id is not None and cost > 0:
            today = current_date()
            budget_key = f"budget:{campaign_id}:{today}"
            
            pipe.hincrbyfloat(budget_key, "spent_today", float(cost))
            pipe.hincrbyfloat(budget_key, "spent_total", float(cost))
            pipe.expire(budget_key, _CACHE_TTL_BUDGET)
            
            if stat_key:
                pipe.hincrbyfloat(stat_key, "spend", float(cost))

        # 3. Manage Pacing Logs (Frequency Caps)
        if event_type_enum == EventType.IMPRESSION and user_id and campaign_id is not None:
            today = current_date()
            daily_key = CacheKeys.freq_daily(user_id, campaign_id, today)
            hourly_key = CacheKeys.freq_hourly(user_id, campaign_id, hour)
            
            pipe.incr(daily_key)
            pipe.expire(daily_key, 86400)    
            pipe.incr(hourly_key)
            pipe.expire(hourly_key, 3600)    

        await pipe.execute()

    def _update_prometheus_metrics(self, campaign_id: int | None, event_type_enum: int, extra: dict[str, Any] | None) -> None:
        """Gracefully emit telemetry to the independent metrics infrastructure layer."""
        cid_str = str(campaign_id) if campaign_id is not None else "unknown"
        
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
            pass  

    # ──────────────────────────────────────────────────────────────────
    # Request & Opportunity Trackers
    # ──────────────────────────────────────────────────────────────────

    @staticmethod
    async def track_ad_request(campaign_ids: list[int] | None = None) -> None:
        """Registers global incoming ad requests dynamically across campaigns."""
        try:
            hour = current_hour()
            pipe = redis_client.pipeline()
            
            if campaign_ids:
                for cid in campaign_ids:
                    key = CacheKeys.stat_hourly(cid, hour)
                    pipe.hincrby(key, "ad_requests", 1)
                    pipe.expire(key, _CACHE_TTL_STATS)
            else:
                key = CacheKeys.stat_hourly(0, hour)
                pipe.hincrby(key, "ad_requests", 1)
                pipe.expire(key, _CACHE_TTL_STATS)
                
            await pipe.execute()
        except Exception as e:
            logger.warning(f"Failed to track ad requests dynamically in Cache: {e}")

    @staticmethod
    async def track_ad_opportunity(campaign_ids: list[int]) -> None:
        """Bump opportunity metrics defining downstream rendering health."""
        if not campaign_ids:
            return
            
        try:
            hour = current_hour()
            pipe = redis_client.pipeline()
            
            for cid in campaign_ids:
                key = CacheKeys.stat_hourly(cid, hour)
                pipe.hincrby(key, "ad_opportunities", 1)
                pipe.expire(key, _CACHE_TTL_STATS)
                
            await pipe.execute()
        except Exception as e:
            logger.warning(f"Failed to cleanly track ad opportunities in cache: {e}")
