"""
Targeting-based retrieval for CPM CTV and In-App video.

Retrieves video ads based on targeting rules matching CTV/In-App user attributes:
- Environment (CTV / In-App)
- Device (Roku, Fire TV, Apple TV, Samsung TV, Android TV, LG TV, mobile, etc.)
- Geo / DMA targeting
- App bundle / content genre
- Video duration / placement constraints
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from liteads.common.cache import CacheKeys, redis_client
from liteads.common.logger import get_logger
from liteads.common.utils import json_dumps, json_loads
from liteads.models import Campaign, Creative, Status, TargetingRule
from liteads.rec_engine.retrieval.base import BaseRetrieval
from liteads.schemas.internal import AdCandidate, UserContext

logger = get_logger(__name__)

# IAB CTV device OS taxonomy
CTV_OS_FAMILY = {
    "roku": "roku",
    "firetv": "firetv",
    "fire_tv": "firetv",
    "amazon": "firetv",
    "tvos": "tvos",
    "appletv": "tvos",
    "apple_tv": "tvos",
    "tizen": "tizen",
    "samsung": "tizen",
    "samsungtv": "tizen",
    "androidtv": "androidtv",
    "android_tv": "androidtv",
    "googletv": "androidtv",
    "google_tv": "androidtv",
    "chromecast": "androidtv",
    "webos": "webos",
    "lg": "webos",
    "lgtv": "webos",
    "lg_tv": "webos",
    "vizio": "vizio",
    "smartcast": "vizio",
    "playstation": "playstation",
    "xbox": "xbox",
}


def normalize_ctv_os(os_raw: str) -> str:
    """Normalize CTV OS string to canonical family name."""
    return CTV_OS_FAMILY.get(os_raw.lower().replace(" ", ""), os_raw.lower())


class TargetingRetrieval(BaseRetrieval):
    """
    Retrieval based on CTV/In-App targeting rules.

    Matches user device, environment, geo/DMA, app bundle, and content genre
    against campaign targeting rules for video ad eligibility.
    """

    def __init__(self, session: AsyncSession):
        self.session = session
        self._cache_ttl = 300  # 5 minutes

    async def retrieve(
        self,
        user_context: UserContext,
        slot_id: str,
        limit: int = 100,
        **kwargs: Any,
    ) -> list[AdCandidate]:
        """
        Retrieve video candidates matching targeting rules.

        Flow:
        1. Get all active CPM campaigns with video creatives
        2. Filter by environment (CTV / In-App)
        3. Check targeting rules against user context
        4. Return matching video candidates
        """
        campaigns = await self._get_active_campaigns()

        if not campaigns:
            logger.debug("No active video campaigns found")
            return []

        candidates: list[AdCandidate] = []

        for campaign_data in campaigns:
            # Environment filter: skip campaigns that don't match
            camp_env = campaign_data.get("environment")
            if camp_env is not None:
                env_map = {1: "ctv", 2: "inapp"}
                if env_map.get(camp_env) != user_context.environment:
                    continue

            # Check targeting rules
            if not self._match_targeting(campaign_data, user_context):
                continue

            # Create candidate for each video creative
            for creative_data in campaign_data.get("creatives", []):
                candidate = AdCandidate(
                    campaign_id=campaign_data["id"],
                    creative_id=creative_data["id"],
                    advertiser_id=campaign_data["advertiser_id"],
                    bid=campaign_data["bid_amount"],
                    bid_type=campaign_data["bid_type"],
                    title=creative_data.get("title"),
                    description=creative_data.get("description"),
                    video_url=creative_data.get("video_url", ""),
                    vast_url=creative_data.get("vast_url"),
                    companion_image_url=creative_data.get("companion_image_url"),
                    landing_url=creative_data.get("landing_url", ""),
                    creative_type=creative_data.get("creative_type", 1),
                    duration=creative_data.get("duration", 30),
                    width=creative_data.get("width", 1920),
                    height=creative_data.get("height", 1080),
                    bitrate=creative_data.get("bitrate"),
                    mime_type=creative_data.get("mime_type", "video/mp4"),
                    skippable=creative_data.get("skippable", True),
                    skip_after=creative_data.get("skip_after", 5),
                    placement=creative_data.get("placement", 1),
                    environment=campaign_data.get("environment", 1),
                    metadata={
                        "quality_score": creative_data.get("quality_score", 80),
                    },
                )
                candidates.append(candidate)

                if len(candidates) >= limit:
                    break

            if len(candidates) >= limit:
                break

        logger.debug(
            f"Retrieved {len(candidates)} video candidates from targeting",
            environment=user_context.environment,
        )
        return candidates

    async def _get_active_campaigns(self) -> list[dict[str, Any]]:
        """Get all active CPM campaigns with video creatives."""
        cache_key = CacheKeys.active_ads()
        cached = await redis_client.get(cache_key)

        if cached:
            try:
                return json_loads(cached)
            except Exception:
                pass

        stmt = (
            select(Campaign)
            .where(Campaign.status == Status.ACTIVE)
            .limit(1000)
        )

        result = await self.session.execute(stmt)
        campaigns = result.scalars().all()

        campaign_list: list[dict[str, Any]] = []

        for campaign in campaigns:
            if not campaign.is_active:
                continue

            campaign_data: dict[str, Any] = {
                "id": campaign.id,
                "advertiser_id": campaign.advertiser_id,
                "name": campaign.name,
                "bid_type": campaign.bid_type,
                "bid_amount": float(campaign.bid_amount),
                "environment": campaign.environment,
                "budget_daily": float(campaign.budget_daily) if campaign.budget_daily else None,
                "budget_total": float(campaign.budget_total) if campaign.budget_total else None,
                "spent_today": float(campaign.spent_today),
                "spent_total": float(campaign.spent_total),
                "freq_cap_daily": campaign.freq_cap_daily,
                "freq_cap_hourly": campaign.freq_cap_hourly,
                "creatives": [],
                "targeting_rules": [],
            }

            # Add video creatives
            for creative in campaign.creatives:
                if creative.status == Status.ACTIVE:
                    campaign_data["creatives"].append({
                        "id": creative.id,
                        "title": creative.title,
                        "description": creative.description,
                        "video_url": creative.video_url,
                        "vast_url": creative.vast_url,
                        "companion_image_url": creative.companion_image_url,
                        "landing_url": creative.landing_url,
                        "creative_type": creative.creative_type,
                        "duration": creative.duration,
                        "width": creative.width,
                        "height": creative.height,
                        "bitrate": creative.bitrate,
                        "mime_type": creative.mime_type,
                        "skippable": creative.skippable,
                        "skip_after": creative.skip_after,
                        "placement": creative.placement,
                        "quality_score": creative.quality_score,
                    })

            # Add targeting rules
            for rule in campaign.targeting_rules:
                campaign_data["targeting_rules"].append({
                    "rule_type": rule.rule_type,
                    "rule_value": rule.rule_value,
                    "is_include": rule.is_include,
                })

            if campaign_data["creatives"]:
                campaign_list.append(campaign_data)

        # Cache the result
        if campaign_list:
            await redis_client.set(
                cache_key,
                json_dumps(campaign_list),
                ttl=self._cache_ttl,
            )

        return campaign_list

    def _match_targeting(
        self,
        campaign_data: dict[str, Any],
        user_context: UserContext,
    ) -> bool:
        """Check if user matches campaign targeting rules."""
        targeting_rules = campaign_data.get("targeting_rules", [])

        if not targeting_rules:
            return True

        for rule in targeting_rules:
            rule_type = rule["rule_type"]
            rule_value = rule["rule_value"]
            is_include = rule["is_include"]

            matched = self._match_rule(rule_type, rule_value, user_context)

            if is_include and not matched:
                return False
            if not is_include and matched:
                return False

        return True

    def _match_rule(
        self,
        rule_type: str,
        rule_value: dict[str, Any],
        user_context: UserContext,
    ) -> bool:
        """Match a single targeting rule against user context."""

        if rule_type == "environment":
            # Match CTV or In-App environment
            values = rule_value.get("values", [])
            if values and user_context.environment:
                return user_context.environment.lower() in [v.lower() for v in values]
            return True

        elif rule_type == "device":
            # CTV device OS targeting (Roku, Fire TV, Apple TV, Samsung, Android TV, LG TV, etc.)
            os_values = rule_value.get("os", [])
            if os_values and user_context.os:
                user_os = normalize_ctv_os(user_context.os)
                target_os_normalized = [normalize_ctv_os(v) for v in os_values]
                if user_os not in target_os_normalized:
                    return False

            device_types = rule_value.get("types", [])
            if device_types and user_context.device_type:
                if user_context.device_type.lower() not in [t.lower() for t in device_types]:
                    return False

            return True

        elif rule_type == "os":
            # OS-level targeting
            os_values = rule_value.get("values", [])
            if os_values and user_context.os:
                user_os = normalize_ctv_os(user_context.os)
                target_os = [normalize_ctv_os(v) for v in os_values]
                if user_os not in target_os:
                    return False
            return True

        elif rule_type == "geo":
            countries = rule_value.get("countries", [])
            dma_codes = rule_value.get("dma", [])
            cities = rule_value.get("cities", [])

            if countries and user_context.country:
                if user_context.country.upper() not in [c.upper() for c in countries]:
                    return False

            if dma_codes and user_context.dma:
                if user_context.dma not in [str(d) for d in dma_codes]:
                    return False

            if cities and user_context.city:
                if user_context.city.lower() not in [c.lower() for c in cities]:
                    return False

            return True

        elif rule_type == "app_bundle":
            bundles = rule_value.get("bundles", [])
            if bundles and user_context.app_bundle:
                if user_context.app_bundle not in bundles:
                    return False
            return True

        elif rule_type == "content_genre":
            genres = rule_value.get("genres", [])
            if genres and user_context.content_genre:
                if user_context.content_genre.lower() not in [g.lower() for g in genres]:
                    return False
            return True

        elif rule_type == "age":
            if user_context.age is None:
                return True
            min_age = rule_value.get("min", 0)
            max_age = rule_value.get("max", 999)
            return min_age <= user_context.age <= max_age

        elif rule_type == "gender":
            if user_context.gender is None:
                return True
            values = rule_value.get("values", [])
            return user_context.gender.lower() in [v.lower() for v in values]

        elif rule_type == "interest":
            interests = rule_value.get("values", [])
            if interests and user_context.interests:
                user_interests_lower = [i.lower() for i in user_context.interests]
                target_interests_lower = [i.lower() for i in interests]
                if not any(i in user_interests_lower for i in target_interests_lower):
                    return False
            return True

        elif rule_type == "daypart":
            # Daypart targeting for CTV prime-time, etc.
            hours = rule_value.get("hours", [])
            days = rule_value.get("days", [])
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            if hours and now.hour not in hours:
                return False
            if days:
                day_names = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
                current_day = day_names[now.weekday()]
                if current_day not in [d.lower() for d in days]:
                    return False
            return True

        # Unknown rule type - default match
        return True

    async def refresh(self) -> None:
        """Clear cache to force refresh."""
        cache_key = CacheKeys.active_ads()
        await redis_client.delete(cache_key)
        logger.info("Targeting retrieval cache refreshed")
