"""
Analytics & reporting service for CPM CTV and In-App Video.

Provides:
- Real-time stats from Redis (hourly/daily/budget).
- Historical stats from the HourlyStat DB table.
- **Demand report**: ADOMAIN, DEMAND_ID, DEMAND_CREATIVE_ID, GROSS_REVENUE,
    BID_REQUEST_FILL_RATE, GROSS_ECPM, AVG_WIN_PRICE, BID_REQUEST_ECPM.
- **Supply / publisher report**: Source Name, Campaign ID, Campaign Name,
    Country Code, Country, Bundle ID, Ad Requests, Ad Opportunities,
    Impressions, Channel Revenue, Channel eCPM, Total Revenue, eCPM,
    Fill Rate (Ad Req), Fill Rate (Ad Ops).
- Redis → DB flush for persistent storage.
- Campaign spend sync from Redis back to the Campaign DB rows.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from liteads.common.cache import CacheKeys, redis_client
from liteads.common.logger import get_logger
from liteads.common.utils import current_date, current_hour, safe_divide
from liteads.models import AdEvent, Advertiser, Campaign, EventType, HourlyStat

logger = get_logger(__name__)

# ── Stat fields tracked in Redis hashes ──────────────────────────────────
_STAT_FIELDS = (
    "ad_requests",
    "ad_opportunities",
    "wins",
    "impressions",
    "starts",
    "first_quartiles",
    "midpoints",
    "third_quartiles",
    "completions",
    "clicks",
    "skips",
    "spend",
    "win_price_sum",
)

# ISO-3166-1 alpha-2 → country name (common ad-tech markets)
_COUNTRY_NAMES: dict[str, str] = {
    "US": "United States", "GB": "United Kingdom", "CA": "Canada",
    "AU": "Australia", "DE": "Germany", "FR": "France", "JP": "Japan",
    "BR": "Brazil", "IN": "India", "MX": "Mexico", "IT": "Italy",
    "ES": "Spain", "KR": "South Korea", "NL": "Netherlands",
    "SE": "Sweden", "NO": "Norway", "DK": "Denmark", "FI": "Finland",
    "PL": "Poland", "AT": "Austria", "CH": "Switzerland",
    "BE": "Belgium", "IE": "Ireland", "NZ": "New Zealand",
    "SG": "Singapore", "HK": "Hong Kong", "TW": "Taiwan",
    "AR": "Argentina", "CO": "Colombia", "CL": "Chile",
    "IL": "Israel", "AE": "United Arab Emirates", "SA": "Saudi Arabia",
    "ZA": "South Africa", "NG": "Nigeria", "EG": "Egypt",
    "TR": "Turkey", "RU": "Russia", "CN": "China", "ID": "Indonesia",
    "TH": "Thailand", "MY": "Malaysia", "PH": "Philippines",
    "VN": "Vietnam",
}


class AnalyticsService:
    """Analytics service with real-time Redis + historical DB queries."""

    def __init__(self, session: AsyncSession):
        self.session = session

    # ══════════════════════════════════════════════════════════════════════
    # 1. Real-time stats (Redis)
    # ══════════════════════════════════════════════════════════════════════

    async def get_campaign_realtime_stats(
        self, campaign_id: int, hour: str | None = None,
    ) -> dict[str, Any]:
        """Get real-time stats for a campaign from Redis for one hour."""
        hour = hour or current_hour()
        key = CacheKeys.stat_hourly(campaign_id, hour)
        raw = await redis_client.hgetall(key)

        int_fields = [f for f in _STAT_FIELDS if f not in ("spend", "win_price_sum")]
        stats: dict[str, Any] = {f: int(raw.get(f, "0")) for f in int_fields}
        stats["spend"] = float(raw.get("spend", "0"))
        stats["win_price_sum"] = float(raw.get("win_price_sum", "0"))

        imps = stats["impressions"]
        ad_reqs = stats["ad_requests"]
        ad_opps = stats["ad_opportunities"]
        wins = stats["wins"]
        spend = stats["spend"]

        stats["vtr"] = round(safe_divide(stats["completions"], imps), 6)
        stats["ctr"] = round(safe_divide(stats["clicks"], imps), 6)
        stats["skip_rate"] = round(safe_divide(stats["skips"], imps), 6)
        stats["gross_ecpm"] = round(safe_divide(spend * 1000, imps), 4)
        stats["avg_win_price"] = round(safe_divide(stats["win_price_sum"], wins), 4)
        stats["bid_request_ecpm"] = round(safe_divide(spend * 1000, ad_reqs), 4)
        stats["fill_rate_ad_req"] = round(safe_divide(imps, ad_reqs) * 100, 2)
        stats["fill_rate_ad_ops"] = round(safe_divide(imps, ad_opps) * 100, 2)

        return {"campaign_id": campaign_id, "hour": hour, **stats}

    async def get_campaign_today_stats(self, campaign_id: int) -> dict[str, Any]:
        """Aggregate today's hourly Redis stats for a campaign."""
        today = current_date()
        totals: dict[str, float] = {f: 0.0 for f in _STAT_FIELDS}

        for h in range(24):
            hour_key = f"{today}{h:02d}"
            key = CacheKeys.stat_hourly(campaign_id, hour_key)
            raw = await redis_client.hgetall(key)
            for f in _STAT_FIELDS:
                totals[f] += float(raw.get(f, "0"))

        imps = totals["impressions"]
        ad_reqs = totals["ad_requests"]
        ad_opps = totals["ad_opportunities"]
        wins = totals["wins"]
        spend = totals["spend"]

        return {
            "campaign_id": campaign_id,
            "date": today,
            "ad_requests": int(ad_reqs),
            "ad_opportunities": int(ad_opps),
            "wins": int(wins),
            "impressions": int(imps),
            "starts": int(totals["starts"]),
            "first_quartiles": int(totals["first_quartiles"]),
            "midpoints": int(totals["midpoints"]),
            "third_quartiles": int(totals["third_quartiles"]),
            "completions": int(totals["completions"]),
            "clicks": int(totals["clicks"]),
            "skips": int(totals["skips"]),
            "spend": round(spend, 4),
            "win_price_sum": round(totals["win_price_sum"], 4),
            "vtr": round(safe_divide(totals["completions"], imps), 6),
            "ctr": round(safe_divide(totals["clicks"], imps), 6),
            "skip_rate": round(safe_divide(totals["skips"], imps), 6),
            "gross_ecpm": round(safe_divide(spend * 1000, imps), 4),
            "avg_win_price": round(safe_divide(totals["win_price_sum"], wins), 4),
            "bid_request_ecpm": round(safe_divide(spend * 1000, ad_reqs), 4),
            "fill_rate_ad_req": round(safe_divide(imps, ad_reqs) * 100, 2),
            "fill_rate_ad_ops": round(safe_divide(imps, ad_opps) * 100, 2),
        }

    async def get_campaign_budget_status(self, campaign_id: int) -> dict[str, Any]:
        """Get budget status from Redis + DB."""
        today = current_date()
        budget_key = f"budget:{campaign_id}:{today}"
        raw = await redis_client.hgetall(budget_key)
        spent_today = float(raw.get("spent_today", "0"))
        spent_total = float(raw.get("spent_total", "0"))

        result = await self.session.execute(
            select(
                Campaign.budget_daily,
                Campaign.budget_total,
                Campaign.bid_amount,
                Campaign.status,
                Campaign.name,
            ).where(Campaign.id == campaign_id)
        )
        row = result.one_or_none()
        if not row:
            return {"error": "Campaign not found"}

        budget_daily = float(row.budget_daily)
        budget_total = float(row.budget_total)

        return {
            "campaign_id": campaign_id,
            "campaign_name": row.name,
            "status": row.status,
            "bid_amount_cpm": float(row.bid_amount),
            "budget_daily": budget_daily,
            "budget_total": budget_total,
            "spent_today": round(spent_today, 4),
            "spent_total": round(spent_total, 4),
            "remaining_daily": round(max(0, budget_daily - spent_today), 4),
            "remaining_total": round(max(0, budget_total - spent_total), 4),
            "daily_pacing_pct": round(safe_divide(spent_today, budget_daily) * 100, 2),
        }

    # ══════════════════════════════════════════════════════════════════════
    # 2. Historical stats (DB)
    # ══════════════════════════════════════════════════════════════════════

    async def get_campaign_historical_stats(
        self,
        campaign_id: int,
        start_hour: datetime | None = None,
        end_hour: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Query HourlyStat table for a campaign over a date range."""
        q = select(HourlyStat).where(HourlyStat.campaign_id == campaign_id)
        if start_hour:
            q = q.where(HourlyStat.stat_hour >= start_hour)
        if end_hour:
            q = q.where(HourlyStat.stat_hour <= end_hour)
        q = q.order_by(HourlyStat.stat_hour)

        result = await self.session.execute(q)
        rows = result.scalars().all()

        return [
            {
                "campaign_id": r.campaign_id,
                "hour": r.stat_hour.isoformat(),
                "ad_requests": r.ad_requests,
                "ad_opportunities": r.ad_opportunities,
                "wins": r.wins,
                "impressions": r.impressions,
                "starts": r.starts,
                "first_quartiles": r.first_quartiles,
                "midpoints": r.midpoints,
                "third_quartiles": r.third_quartiles,
                "completions": r.completions,
                "clicks": r.clicks,
                "skips": r.skips,
                "spend": float(r.spend),
                "win_price_sum": float(r.win_price_sum),
                "vtr": float(r.vtr),
                "gross_ecpm": round(
                    safe_divide(float(r.spend) * 1000, r.impressions), 4,
                ),
                "avg_win_price": round(
                    safe_divide(float(r.win_price_sum), r.wins), 4,
                ),
                "fill_rate_ad_req": round(
                    safe_divide(r.impressions, r.ad_requests) * 100, 2,
                ),
                "fill_rate_ad_ops": round(
                    safe_divide(r.impressions, r.ad_opportunities) * 100, 2,
                ),
            }
            for r in rows
        ]

    # ══════════════════════════════════════════════════════════════════════
    # 3. All-campaigns summary
    # ══════════════════════════════════════════════════════════════════════

    async def get_all_campaigns_summary(self) -> list[dict[str, Any]]:
        """Summary view of all campaigns with real-time Redis data."""
        result = await self.session.execute(
            select(
                Campaign.id,
                Campaign.name,
                Campaign.advertiser_id,
                Campaign.bid_amount,
                Campaign.budget_daily,
                Campaign.budget_total,
                Campaign.status,
                Campaign.environment,
                Campaign.impressions,
                Campaign.completions,
                Campaign.clicks,
            ).order_by(Campaign.id)
        )
        rows = result.all()

        summaries: list[dict[str, Any]] = []
        today = current_date()
        for row in rows:
            budget_key = f"budget:{row.id}:{today}"
            raw = await redis_client.hgetall(budget_key)
            spent_today = float(raw.get("spent_today", "0"))

            summaries.append({
                "campaign_id": row.id,
                "name": row.name,
                "advertiser_id": row.advertiser_id,
                "status": row.status,
                "environment": row.environment,
                "bid_amount_cpm": float(row.bid_amount),
                "budget_daily": float(row.budget_daily),
                "spent_today": round(spent_today, 4),
                "impressions": row.impressions,
                "completions": row.completions,
                "clicks": row.clicks,
            })

        return summaries

    # ══════════════════════════════════════════════════════════════════════
    # 4. DEMAND REPORT
    #    Grouped by: adomain, campaign_id (demand_id), creative_id
    # ══════════════════════════════════════════════════════════════════════

    async def get_demand_report(
        self,
        start: datetime | None = None,
        end: datetime | None = None,
        campaign_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Demand-side analytics report.

        Returns rows grouped by (adomain, campaign_id, creative_id) with:
            ADOMAIN, DEMAND_ID, DEMAND_CREATIVE_ID, GROSS_REVENUE,
            BID_REQUEST_FILL_RATE, GROSS_ECPM, AVG_WIN_PRICE,
            BID_REQUEST_ECPM
        """
        # Base filter: IMPRESSION events for revenue metrics
        filters: list[Any] = [AdEvent.event_type == EventType.IMPRESSION]
        if start:
            filters.append(AdEvent.event_time >= start)
        if end:
            filters.append(AdEvent.event_time <= end)
        if campaign_id:
            filters.append(AdEvent.campaign_id == campaign_id)

        # Impressions / revenue grouped by demand dimensions
        imp_q = (
            select(
                func.coalesce(AdEvent.adomain, "unknown").label("adomain"),
                AdEvent.campaign_id.label("demand_id"),
                AdEvent.creative_id.label("demand_creative_id"),
                func.count().label("impressions"),
                func.sum(AdEvent.cost).label("gross_revenue"),
                func.sum(AdEvent.win_price).label("win_price_sum"),
            )
            .where(*filters)
            .group_by(
                func.coalesce(AdEvent.adomain, "unknown"),
                AdEvent.campaign_id,
                AdEvent.creative_id,
            )
        )

        imp_result = await self.session.execute(imp_q)
        imp_rows = imp_result.all()

        # Distinct request count per campaign (proxy for bid requests)
        req_q = (
            select(
                AdEvent.campaign_id,
                func.count(func.distinct(AdEvent.request_id)).label("bid_requests"),
            )
            .where(*filters)
            .group_by(AdEvent.campaign_id)
        )
        req_result = await self.session.execute(req_q)
        bid_requests_map: dict[int | None, int] = {
            r.campaign_id: r.bid_requests for r in req_result.all()
        }

        report: list[dict[str, Any]] = []
        for row in imp_rows:
            imps = int(row.impressions)
            revenue = float(row.gross_revenue or 0)
            wp_sum = float(row.win_price_sum or 0)
            bid_reqs = bid_requests_map.get(row.demand_id, 0)

            report.append({
                "adomain": row.adomain,
                "demand_id": row.demand_id,
                "demand_creative_id": row.demand_creative_id,
                "impressions": imps,
                "gross_revenue": round(revenue, 4),
                "bid_request_fill_rate": round(
                    safe_divide(imps, bid_reqs) * 100, 2,
                ),
                "gross_ecpm": round(safe_divide(revenue * 1000, imps), 4),
                "avg_win_price": round(safe_divide(wp_sum, imps), 4),
                "bid_request_ecpm": round(
                    safe_divide(revenue * 1000, bid_reqs), 4,
                ),
            })

        return report

    # ══════════════════════════════════════════════════════════════════════
    # 5. SUPPLY / PUBLISHER REPORT
    #    Grouped by: source_name, campaign, country, bundle
    # ══════════════════════════════════════════════════════════════════════

    async def get_supply_report(
        self,
        start: datetime | None = None,
        end: datetime | None = None,
        campaign_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Supply-side / publisher analytics report.

        Returns rows grouped by (source_name, campaign_id, country_code,
        bundle_id):
            Source Name, Campaign ID, Campaign Name, Country Code, Country,
            Bundle ID, Ad Requests, Ad Opportunities, Impressions,
            Channel Revenue, Channel eCPM, Total Revenue, eCPM,
            Fill Rate (Ad Req), Fill Rate (Ad Ops)
        """
        filters: list[Any] = [AdEvent.event_type == EventType.IMPRESSION]
        if start:
            filters.append(AdEvent.event_time >= start)
        if end:
            filters.append(AdEvent.event_time <= end)
        if campaign_id:
            filters.append(AdEvent.campaign_id == campaign_id)

        q = (
            select(
                func.coalesce(AdEvent.source_name, "direct").label("source_name"),
                AdEvent.campaign_id,
                func.coalesce(AdEvent.country_code, "XX").label("country_code"),
                func.coalesce(AdEvent.bundle_id, "unknown").label("bundle_id"),
                func.count().label("impressions"),
                func.sum(AdEvent.cost).label("total_revenue"),
                func.sum(AdEvent.win_price).label("channel_revenue"),
            )
            .where(*filters)
            .group_by(
                func.coalesce(AdEvent.source_name, "direct"),
                AdEvent.campaign_id,
                func.coalesce(AdEvent.country_code, "XX"),
                func.coalesce(AdEvent.bundle_id, "unknown"),
            )
        )

        result = await self.session.execute(q)
        rows = result.all()

        # Campaign names + advertiser names for lookup
        camp_q = (
            select(
                Campaign.id,
                Campaign.name,
                Advertiser.name.label("advertiser_name"),
            )
            .join(Advertiser, Campaign.advertiser_id == Advertiser.id)
        )
        camp_result = await self.session.execute(camp_q)
        camp_map: dict[int, tuple[str, str]] = {
            r.id: (r.name, r.advertiser_name) for r in camp_result.all()
        }

        # Ad requests / opportunities from Redis (today only)
        today = current_date()
        req_opp_cache: dict[int, tuple[int, int]] = {}

        report: list[dict[str, Any]] = []
        for row in rows:
            cid: int = row.campaign_id or 0
            imps = int(row.impressions)
            total_rev = float(row.total_revenue or 0)
            channel_rev = float(row.channel_revenue or 0)
            cc: str = row.country_code

            # Lazy-load ad_requests / ad_opportunities from Redis
            if cid and cid not in req_opp_cache:
                ad_reqs = 0
                ad_opps = 0
                for h in range(24):
                    hour_key = f"{today}{h:02d}"
                    rkey = CacheKeys.stat_hourly(cid, hour_key)
                    raw = await redis_client.hgetall(rkey)
                    ad_reqs += int(raw.get("ad_requests", "0"))
                    ad_opps += int(raw.get("ad_opportunities", "0"))
                req_opp_cache[cid] = (ad_reqs, ad_opps)

            ad_reqs, ad_opps = req_opp_cache.get(cid, (0, 0))
            camp_name, _ = camp_map.get(cid, ("Unknown", "Unknown"))

            report.append({
                "source_name": row.source_name,
                "campaign_id": cid,
                "campaign_name": camp_name,
                "country_code": cc,
                "country": _COUNTRY_NAMES.get(cc, cc),
                "bundle_id": row.bundle_id,
                "ad_requests": ad_reqs,
                "ad_opportunities": ad_opps,
                "impressions": imps,
                "channel_revenue": round(channel_rev, 4),
                "channel_ecpm": round(safe_divide(channel_rev * 1000, imps), 4),
                "total_revenue": round(total_rev, 4),
                "ecpm": round(safe_divide(total_rev * 1000, imps), 4),
                "fill_rate_ad_req": round(
                    safe_divide(imps, ad_reqs) * 100, 2,
                ),
                "fill_rate_ad_ops": round(
                    safe_divide(imps, ad_opps) * 100, 2,
                ),
            })

        return report

    # ══════════════════════════════════════════════════════════════════════
    # 6. DELIVERY HEALTH REPORT
    #    VAST funnel: impressions → start → Q1 → mid → Q3 → complete
    # ══════════════════════════════════════════════════════════════════════

    async def get_delivery_health_report(
        self,
        start: datetime | None = None,
        end: datetime | None = None,
        campaign_id: int | None = None,
    ) -> dict[str, Any]:
        """Delivery health report with VAST funnel metrics.

        Returns per-campaign and aggregate:
        - VAST event funnel (impression → start → Q1 → mid → Q3 → complete)
        - Ad start rate, VTR/completion rate, skip rate, error rate
        - No-bid rate (from Redis)
        """
        # Query ad events grouped by campaign and event_type
        filters: list[Any] = []
        if start:
            filters.append(AdEvent.event_time >= start)
        if end:
            filters.append(AdEvent.event_time <= end)
        if campaign_id:
            filters.append(AdEvent.campaign_id == campaign_id)

        q = select(
            AdEvent.campaign_id,
            AdEvent.event_type,
            func.count().label("cnt"),
        )
        if filters:
            q = q.where(*filters)
        q = q.group_by(AdEvent.campaign_id, AdEvent.event_type)

        result = await self.session.execute(q)
        rows = result.all()

        # Build per-campaign funnel
        campaign_data: dict[int, dict[str, int]] = {}
        for row in rows:
            cid = row.campaign_id or 0
            if cid not in campaign_data:
                campaign_data[cid] = {
                    "impressions": 0, "starts": 0,
                    "first_quartiles": 0, "midpoints": 0,
                    "third_quartiles": 0, "completions": 0,
                    "clicks": 0, "skips": 0, "errors": 0,
                }
            et = row.event_type
            cnt = row.cnt
            if et == EventType.IMPRESSION:
                campaign_data[cid]["impressions"] = cnt
            elif et == EventType.START:
                campaign_data[cid]["starts"] = cnt
            elif et == EventType.FIRST_QUARTILE:
                campaign_data[cid]["first_quartiles"] = cnt
            elif et == EventType.MIDPOINT:
                campaign_data[cid]["midpoints"] = cnt
            elif et == EventType.THIRD_QUARTILE:
                campaign_data[cid]["third_quartiles"] = cnt
            elif et == EventType.COMPLETE:
                campaign_data[cid]["completions"] = cnt
            elif et == EventType.CLICK:
                campaign_data[cid]["clicks"] = cnt
            elif et == EventType.SKIP:
                campaign_data[cid]["skips"] = cnt
            elif et == EventType.ERROR:
                campaign_data[cid]["errors"] = cnt

        # Aggregate totals
        totals = {
            "impressions": 0, "starts": 0,
            "first_quartiles": 0, "midpoints": 0,
            "third_quartiles": 0, "completions": 0,
            "clicks": 0, "skips": 0, "errors": 0,
        }
        campaigns_detail: list[dict[str, Any]] = []
        for cid, data in campaign_data.items():
            for k in totals:
                totals[k] += data[k]
            imps = data["impressions"]
            campaigns_detail.append({
                "campaign_id": cid,
                **data,
                "ad_start_rate": round(safe_divide(data["starts"], imps) * 100, 2),
                "vtr": round(safe_divide(data["completions"], imps) * 100, 2),
                "skip_rate": round(safe_divide(data["skips"], imps) * 100, 2),
                "error_rate": round(safe_divide(data["errors"], imps) * 100, 2),
                "ctr": round(safe_divide(data["clicks"], imps) * 100, 2),
            })

        total_imps = totals["impressions"]

        # Get no-bid rate from Redis
        today = current_date()
        total_ad_reqs = 0
        total_filled = 0
        result2 = await self.session.execute(select(Campaign.id))
        camp_ids = [r[0] for r in result2.all()]
        for cid in camp_ids:
            for h in range(24):
                hour_key = f"{today}{h:02d}"
                rkey = CacheKeys.stat_hourly(cid, hour_key)
                raw = await redis_client.hgetall(rkey)
                total_ad_reqs += int(raw.get("ad_requests", "0"))
                total_filled += int(raw.get("impressions", "0"))

        return {
            "funnel": {
                "impressions": totals["impressions"],
                "starts": totals["starts"],
                "first_quartiles": totals["first_quartiles"],
                "midpoints": totals["midpoints"],
                "third_quartiles": totals["third_quartiles"],
                "completions": totals["completions"],
            },
            "aggregate": {
                **totals,
                "ad_start_rate": round(safe_divide(totals["starts"], total_imps) * 100, 2),
                "vtr": round(safe_divide(totals["completions"], total_imps) * 100, 2),
                "skip_rate": round(safe_divide(totals["skips"], total_imps) * 100, 2),
                "error_rate": round(safe_divide(totals["errors"], total_imps) * 100, 2),
                "ctr": round(safe_divide(totals["clicks"], total_imps) * 100, 2),
                "fill_rate": round(safe_divide(total_filled, total_ad_reqs) * 100, 2),
                "no_bid_rate": round((1 - safe_divide(total_filled, total_ad_reqs)) * 100, 2) if total_ad_reqs > 0 else 0.0,
                "total_ad_requests": total_ad_reqs,
            },
            "by_campaign": campaigns_detail,
        }

    # ══════════════════════════════════════════════════════════════════════
    # 7. Redis → DB flush (HourlyStat persistence)
    # ══════════════════════════════════════════════════════════════════════

    async def flush_hourly_stats(self, hour: str | None = None) -> int:
        """Flush Redis hourly stats into the HourlyStat DB table.

        Should be called periodically (e.g. every hour via cron/scheduler).
        If *hour* is None, flushes the **previous** hour.

        Returns:
            Number of campaigns flushed.
        """
        if hour is None:
            from datetime import timedelta

            prev = datetime.now(timezone.utc) - timedelta(hours=1)
            hour = prev.strftime("%Y%m%d%H")

        stat_hour_dt = datetime.strptime(hour, "%Y%m%d%H").replace(
            tzinfo=timezone.utc,
        )

        result = await self.session.execute(select(Campaign.id))
        campaign_ids = [r[0] for r in result.all()]

        flushed = 0
        for cid in campaign_ids:
            key = CacheKeys.stat_hourly(cid, hour)
            raw = await redis_client.hgetall(key)
            if not raw:
                continue

            imps = int(raw.get("impressions", "0"))
            completions = int(raw.get("completions", "0"))
            spend = float(raw.get("spend", "0"))
            wp_sum = float(raw.get("win_price_sum", "0"))
            vtr = safe_divide(completions, imps) if imps else 0.0

            values = dict(
                campaign_id=cid,
                stat_hour=stat_hour_dt,
                ad_requests=int(raw.get("ad_requests", "0")),
                ad_opportunities=int(raw.get("ad_opportunities", "0")),
                wins=int(raw.get("wins", "0")),
                impressions=imps,
                starts=int(raw.get("starts", "0")),
                first_quartiles=int(raw.get("first_quartiles", "0")),
                midpoints=int(raw.get("midpoints", "0")),
                third_quartiles=int(raw.get("third_quartiles", "0")),
                completions=completions,
                clicks=int(raw.get("clicks", "0")),
                skips=int(raw.get("skips", "0")),
                spend=Decimal(str(round(spend, 4))),
                win_price_sum=Decimal(str(round(wp_sum, 4))),
                vtr=Decimal(str(round(vtr, 6))),
            )

            # Upsert – on re-flush the row is replaced rather than duplicated
            stmt = pg_insert(HourlyStat).values(**values)
            stmt = stmt.on_conflict_do_update(
                constraint="uq_hourly_stat_campaign_hour",
                set_={k: v for k, v in values.items() if k not in ("campaign_id", "stat_hour")},
            )
            await self.session.execute(stmt)
            flushed += 1

        await self.session.flush()
        logger.info("Flushed hourly stats to DB", hour=hour, campaigns=flushed)
        return flushed

    # ══════════════════════════════════════════════════════════════════════
    # 7. Spend sync (Redis → Campaign DB)
    # ══════════════════════════════════════════════════════════════════════

    async def sync_campaign_spend_to_db(self) -> int:
        """Sync Redis budget spend back to Campaign DB rows.

        Updates ``spent_today`` and ``spent_total`` columns so the DB
        reflects actual recorded spend.  Should run periodically.

        Returns:
            Number of campaigns updated.
        """
        today = current_date()
        result = await self.session.execute(select(Campaign.id))
        campaign_ids = [r[0] for r in result.all()]

        updated = 0
        for cid in campaign_ids:
            budget_key = f"budget:{cid}:{today}"
            raw = await redis_client.hgetall(budget_key)
            if not raw:
                continue

            spent_today = float(raw.get("spent_today", "0"))
            spent_total = float(raw.get("spent_total", "0"))

            await self.session.execute(
                update(Campaign)
                .where(Campaign.id == cid)
                .values(
                    spent_today=Decimal(str(round(spent_today, 4))),
                    spent_total=Decimal(str(round(spent_total, 4))),
                )
            )
            updated += 1

        await self.session.flush()
        logger.info("Synced campaign spend to DB", campaigns=updated)
        return updated
