"""
Analytics & Reporting Router – Campaign performance, demand & supply reports.

Endpoints:
    GET  /campaigns                    – All campaigns summary
    GET  /campaign/{id}/realtime       – Current-hour live stats
    GET  /campaign/{id}/today          – Today aggregate stats
    GET  /campaign/{id}/budget         – Budget & spend status
    GET  /campaign/{id}/historical     – DB-based historical data
    GET  /reports/demand               – Demand report (adomain, gross rev, eCPM …)
    GET  /reports/supply               – Supply / publisher report
    GET  /reports/delivery-health      – Delivery health: VAST funnel, error rates, VTR
    GET  /reports/vast-errors          – VAST error breakdown by code & campaign
    POST /flush                        – Flush Redis stats → DB
    POST /sync-spend                   – Sync spend Redis → Campaign DB
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from liteads.ad_server.services.analytics_service import AnalyticsService
from liteads.common.database import get_session
from liteads.common.logger import get_logger
from liteads.common.utils import safe_divide
from liteads.models import AdEvent, EventType

logger = get_logger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class DemandReportRow(BaseModel):
    """Single row in the demand report."""

    adomain: str = Field(..., description="Advertiser domain")
    demand_id: int | None = Field(None, description="Campaign / demand source ID")
    demand_creative_id: int | None = Field(None, description="Creative ID")
    impressions: int = 0
    gross_revenue: float = Field(0.0, description="Gross revenue in USD")
    bid_request_fill_rate: float = Field(0.0, description="Fill rate vs bid requests (%)")
    gross_ecpm: float = Field(0.0, description="Gross eCPM = (revenue / imps) * 1000")
    avg_win_price: float = Field(0.0, description="Average auction clearing price")
    bid_request_ecpm: float = Field(0.0, description="eCPM relative to bid requests")


class SupplyReportRow(BaseModel):
    """Single row in the supply / publisher report."""

    source_name: str = Field(..., description="Supply source / SSP name")
    campaign_id: int = Field(..., description="Campaign ID")
    campaign_name: str = Field("", description="Campaign display name")
    country_code: str = Field("XX", description="ISO 3166-1 alpha-2")
    country: str = Field("", description="Country name")
    bundle_id: str = Field("unknown", description="App bundle ID")
    ad_requests: int = Field(0, description="Bid requests received")
    ad_opportunities: int = Field(0, description="Eligible impressions (bid opps)")
    impressions: int = 0
    channel_revenue: float = Field(0.0, description="Revenue at auction clearing price")
    channel_ecpm: float = Field(0.0, description="Channel eCPM (clearing-based)")
    total_revenue: float = Field(0.0, description="Gross revenue (CPM cost-based)")
    ecpm: float = Field(0.0, description="eCPM (CPM cost / imps * 1000)")
    fill_rate_ad_req: float = Field(0.0, description="Fill rate vs ad requests (%)")
    fill_rate_ad_ops: float = Field(0.0, description="Fill rate vs ad opportunities (%)")


# ---------------------------------------------------------------------------
# Dependency
# ---------------------------------------------------------------------------

def _get_analytics_service(
    session: AsyncSession = Depends(get_session),
) -> AnalyticsService:
    return AnalyticsService(session)


# ---------------------------------------------------------------------------
# Campaign-level endpoints
# ---------------------------------------------------------------------------

@router.get(
    "/campaigns",
    summary="All campaigns summary",
    description="Returns a summary of all campaigns with today's Redis spend.",
)
async def list_campaigns_summary(
    service: AnalyticsService = Depends(_get_analytics_service),
) -> dict[str, Any]:
    summaries = await service.get_all_campaigns_summary()
    return {"campaigns": summaries, "count": len(summaries)}


@router.get(
    "/campaign/{campaign_id}/realtime",
    summary="Real-time stats (current hour)",
    description=(
        "Live stats from Redis for the current (or specified) hour.  "
        "Includes ad_requests, ad_opportunities, wins, impressions, "
        "spend, gross_ecpm, avg_win_price, bid_request_ecpm, and fill rates."
    ),
)
async def campaign_realtime(
    campaign_id: int,
    hour: str | None = Query(None, description="Hour (YYYYMMDDHH), default=current"),
    service: AnalyticsService = Depends(_get_analytics_service),
) -> dict[str, Any]:
    return await service.get_campaign_realtime_stats(campaign_id, hour)


@router.get(
    "/campaign/{campaign_id}/today",
    summary="Today's aggregate stats",
    description="Aggregates all hourly Redis buckets for today.",
)
async def campaign_today(
    campaign_id: int,
    service: AnalyticsService = Depends(_get_analytics_service),
) -> dict[str, Any]:
    return await service.get_campaign_today_stats(campaign_id)


@router.get(
    "/campaign/{campaign_id}/budget",
    summary="Budget & spend status",
    description="Shows daily + total budget, spend, remaining, and pacing %.",
)
async def campaign_budget(
    campaign_id: int,
    service: AnalyticsService = Depends(_get_analytics_service),
) -> dict[str, Any]:
    data = await service.get_campaign_budget_status(campaign_id)
    if "error" in data:
        raise HTTPException(status_code=404, detail=data["error"])
    return data


@router.get(
    "/campaign/{campaign_id}/historical",
    summary="Historical hourly stats",
    description="Query HourlyStat DB table with optional date range.",
)
async def campaign_historical(
    campaign_id: int,
    start: str | None = Query(None, description="Start datetime ISO-8601"),
    end: str | None = Query(None, description="End datetime ISO-8601"),
    service: AnalyticsService = Depends(_get_analytics_service),
) -> dict[str, Any]:
    start_dt = (
        datetime.fromisoformat(start).replace(tzinfo=timezone.utc) if start else None
    )
    end_dt = (
        datetime.fromisoformat(end).replace(tzinfo=timezone.utc) if end else None
    )
    rows = await service.get_campaign_historical_stats(campaign_id, start_dt, end_dt)
    return {"campaign_id": campaign_id, "hours": rows, "count": len(rows)}


# ---------------------------------------------------------------------------
# Demand & Supply reports
# ---------------------------------------------------------------------------

@router.get(
    "/reports/demand",
    response_model=list[DemandReportRow],
    summary="Demand report",
    description=(
        "Demand-side report grouped by ADOMAIN × DEMAND_ID × DEMAND_CREATIVE_ID. "
        "Returns GROSS_REVENUE, BID_REQUEST_FILL_RATE, GROSS_ECPM, "
        "AVG_WIN_PRICE, and BID_REQUEST_ECPM."
    ),
)
async def demand_report(
    start: str | None = Query(None, description="Start datetime ISO-8601"),
    end: str | None = Query(None, description="End datetime ISO-8601"),
    campaign_id: int | None = Query(None, description="Filter by campaign"),
    service: AnalyticsService = Depends(_get_analytics_service),
) -> list[dict[str, Any]]:
    start_dt = (
        datetime.fromisoformat(start).replace(tzinfo=timezone.utc) if start else None
    )
    end_dt = (
        datetime.fromisoformat(end).replace(tzinfo=timezone.utc) if end else None
    )
    return await service.get_demand_report(start_dt, end_dt, campaign_id)


@router.get(
    "/reports/supply",
    response_model=list[SupplyReportRow],
    summary="Supply / publisher report",
    description=(
        "Supply-side report grouped by Source Name × Campaign × Country × Bundle. "
        "Returns Ad Requests, Ad Opportunities, Impressions, "
        "Channel Revenue, Channel eCPM, Total Revenue, eCPM, "
        "Fill Rate (Ad Req), Fill Rate (Ad Ops)."
    ),
)
async def supply_report(
    start: str | None = Query(None, description="Start datetime ISO-8601"),
    end: str | None = Query(None, description="End datetime ISO-8601"),
    campaign_id: int | None = Query(None, description="Filter by campaign"),
    service: AnalyticsService = Depends(_get_analytics_service),
) -> list[dict[str, Any]]:
    start_dt = (
        datetime.fromisoformat(start).replace(tzinfo=timezone.utc) if start else None
    )
    end_dt = (
        datetime.fromisoformat(end).replace(tzinfo=timezone.utc) if end else None
    )
    return await service.get_supply_report(start_dt, end_dt, campaign_id)


# ---------------------------------------------------------------------------
# Delivery Health & VAST Error reports
# ---------------------------------------------------------------------------

@router.get(
    "/reports/delivery-health",
    summary="Delivery health report",
    description=(
        "Returns the VAST delivery funnel (impressions → start → Q1 → mid → Q3 → complete), "
        "ad start rate, VTR, skip rate, error rate, and no-bid rate per campaign. "
        "Uses HourlyStat data or live Redis stats."
    ),
)
async def delivery_health_report(
    start: str | None = Query(None, description="Start datetime ISO-8601"),
    end: str | None = Query(None, description="End datetime ISO-8601"),
    campaign_id: int | None = Query(None, description="Filter by campaign"),
    service: AnalyticsService = Depends(_get_analytics_service),
) -> dict[str, Any]:
    start_dt = (
        datetime.fromisoformat(start).replace(tzinfo=timezone.utc) if start else None
    )
    end_dt = (
        datetime.fromisoformat(end).replace(tzinfo=timezone.utc) if end else None
    )
    return await service.get_delivery_health_report(start_dt, end_dt, campaign_id)


@router.get(
    "/reports/vast-errors",
    summary="VAST error breakdown",
    description=(
        "Returns VAST error events grouped by error_code and campaign. "
        "Error codes follow IAB VAST spec (100=XML parse, 200=wrapper, "
        "300=linear, 400=companion, 500=non-linear, 600=media, 900=undefined)."
    ),
)
async def vast_errors_report(
    start: str | None = Query(None, description="Start datetime ISO-8601"),
    end: str | None = Query(None, description="End datetime ISO-8601"),
    campaign_id: int | None = Query(None, description="Filter by campaign"),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    from sqlalchemy import func, select

    start_dt = (
        datetime.fromisoformat(start).replace(tzinfo=timezone.utc) if start else None
    )
    end_dt = (
        datetime.fromisoformat(end).replace(tzinfo=timezone.utc) if end else None
    )

    filters: list[Any] = [AdEvent.event_type == EventType.ERROR]
    if start_dt:
        filters.append(AdEvent.event_time >= start_dt)
    if end_dt:
        filters.append(AdEvent.event_time <= end_dt)
    if campaign_id:
        filters.append(AdEvent.campaign_id == campaign_id)

    q = (
        select(
            AdEvent.campaign_id,
            func.count().label("error_count"),
        )
        .where(*filters)
        .group_by(AdEvent.campaign_id)
        .order_by(func.count().desc())
    )
    result = await session.execute(q)
    rows = result.all()

    # Also get total events for error rate calculation
    total_q = select(func.count()).select_from(AdEvent)
    if start_dt:
        total_q = total_q.where(AdEvent.event_time >= start_dt)
    if end_dt:
        total_q = total_q.where(AdEvent.event_time <= end_dt)
    if campaign_id:
        total_q = total_q.where(AdEvent.campaign_id == campaign_id)
    total_result = await session.execute(total_q)
    total_events = total_result.scalar() or 0

    error_rows = []
    total_errors = 0
    for row in rows:
        total_errors += row.error_count
        error_rows.append({
            "campaign_id": row.campaign_id,
            "error_count": row.error_count,
        })

    return {
        "total_errors": total_errors,
        "total_events": total_events,
        "error_rate_pct": round(safe_divide(total_errors, total_events) * 100, 2),
        "by_campaign": error_rows,
    }


# ---------------------------------------------------------------------------
# Operational endpoints
# ---------------------------------------------------------------------------

@router.post(
    "/flush",
    summary="Flush Redis hourly stats → DB",
    description=(
        "Persists Redis hourly stat counters into the HourlyStat table. "
        "Should be called once per hour by a cron job or scheduler."
    ),
)
async def flush_stats(
    hour: str | None = Query(None, description="Hour (YYYYMMDDHH), default=previous hour"),
    service: AnalyticsService = Depends(_get_analytics_service),
) -> dict[str, Any]:
    flushed = await service.flush_hourly_stats(hour)
    return {"flushed_campaigns": flushed, "hour": hour or "previous"}


@router.post(
    "/sync-spend",
    summary="Sync Redis budget spend → Campaign DB",
    description=(
        "Updates Campaign.spent_today and Campaign.spent_total from Redis. "
        "Should run periodically to keep the DB in sync."
    ),
)
async def sync_spend(
    service: AnalyticsService = Depends(_get_analytics_service),
) -> dict[str, Any]:
    updated = await service.sync_campaign_spend_to_db()
    return {"updated_campaigns": updated}
