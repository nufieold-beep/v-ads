"""
Demand-Side Router – Self-service API for advertisers / DSPs.

Provides endpoints for demand partners to:
  - Onboard and manage their advertiser account
  - Create / manage CPM video campaigns with budgets and bids
  - Upload video creatives (CTV + In-App)
  - Set targeting rules (geo, device, environment, content, daypart)
  - View real-time campaign performance & spend
  - Retrieve integration endpoints (VAST tag URL, OpenRTB bid endpoint)

All endpoints are under /api/v1/demand.

Endpoints:
    ONBOARDING
        POST   /register                           – Register new demand account
        GET    /account                             – Get own account details
        PUT    /account                             – Update account
        POST   /account/fund                        – Add funds to balance

    CAMPAIGNS
        POST   /campaigns                           – Create campaign
        GET    /campaigns                           – List own campaigns
        GET    /campaigns/{id}                      – Get campaign detail (w/ creatives + targeting)
        PUT    /campaigns/{id}                      – Update campaign
        PATCH  /campaigns/{id}/status               – Pause / resume / archive
        PATCH  /campaigns/{id}/bid                  – Update CPM bid
        PATCH  /campaigns/{id}/budget               – Update daily/total budget

    CREATIVES
        POST   /campaigns/{id}/creatives            – Add creative to campaign
        GET    /campaigns/{id}/creatives            – List creatives for campaign
        PUT    /creatives/{id}                      – Update creative
        DELETE /creatives/{id}                      – Remove creative

    TARGETING
        POST   /campaigns/{id}/targeting            – Add targeting rule
        GET    /campaigns/{id}/targeting            – List targeting rules
        DELETE /targeting/{rule_id}                  – Remove targeting rule

    REPORTING
        GET    /campaigns/{id}/stats                – Campaign performance stats
        GET    /campaigns/{id}/spend                – Budget / spend status
        GET    /dashboard                           – Overview across all campaigns

    INTEGRATION
        GET    /integration/endpoints               – Get VAST tag URL + OpenRTB endpoint
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from liteads.common.cache import CacheKeys, redis_client
from liteads.common.database import get_session
from liteads.common.logger import get_logger
from liteads.models import (
    AdEvent,
    Advertiser,
    Campaign,
    Creative,
    HourlyStat,
    EventType,
    Status as ModelStatus,
    TargetingRule,
)

logger = get_logger(__name__)
router = APIRouter()


# ============================================================================
# Pydantic schemas
# ============================================================================

# ---- Account ----

class DemandRegisterRequest(BaseModel):
    name: str = Field(..., max_length=255, description="Advertiser / DSP name")
    company: str | None = Field(None, max_length=255, description="Company name")
    contact_email: str = Field(..., max_length=255, description="Contact email")
    initial_balance: float = Field(0.0, ge=0, description="Initial account balance (USD)")
    daily_budget: float = Field(0.0, ge=0, description="Daily spend limit (USD)")


class DemandAccountUpdate(BaseModel):
    name: str | None = None
    company: str | None = None
    contact_email: str | None = None
    daily_budget: float | None = Field(None, ge=0)


class DemandFundRequest(BaseModel):
    amount: float = Field(..., gt=0, description="Amount to add (USD)")


class DemandAccountOut(BaseModel):
    id: int
    name: str
    company: str | None = None
    contact_email: str | None = None
    balance: float
    daily_budget: float
    status: int
    total_campaigns: int = 0
    active_campaigns: int = 0
    created_at: datetime | None = None
    model_config = {"from_attributes": True}


# ---- Campaign ----

class DemandCampaignCreate(BaseModel):
    name: str = Field(..., max_length=255, description="Campaign name")
    description: str | None = Field(None, description="Campaign description")
    environment: int | None = Field(
        None, description="1 = CTV, 2 = In-App, null = both"
    )
    bid_amount: float = Field(..., gt=0, description="CPM bid in USD")
    budget_daily: float = Field(0.0, ge=0, description="Daily budget (USD)")
    budget_total: float = Field(0.0, ge=0, description="Total budget (USD)")
    freq_cap_daily: int = Field(10, ge=0, description="Daily frequency cap per user")
    freq_cap_hourly: int = Field(3, ge=0, description="Hourly frequency cap per user")
    start_time: datetime | None = Field(None, description="Campaign start (UTC)")
    end_time: datetime | None = Field(None, description="Campaign end (UTC)")


class DemandCampaignUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    environment: int | None = None
    freq_cap_daily: int | None = None
    freq_cap_hourly: int | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None


class DemandBidUpdate(BaseModel):
    bid_amount: float = Field(..., gt=0, description="New CPM bid (USD)")


class DemandBudgetUpdate(BaseModel):
    budget_daily: float | None = Field(None, ge=0, description="New daily budget")
    budget_total: float | None = Field(None, ge=0, description="New total budget")


class DemandStatusUpdate(BaseModel):
    status: int = Field(
        ...,
        description="1 = active, 2 = paused, 0 = inactive",
    )


class DemandCampaignOut(BaseModel):
    id: int
    name: str
    description: str | None = None
    environment: int | None = None
    environment_label: str = ""
    bid_amount: float
    budget_daily: float
    budget_total: float
    spent_today: float
    spent_total: float
    freq_cap_daily: int
    freq_cap_hourly: int
    start_time: datetime | None = None
    end_time: datetime | None = None
    status: int
    status_label: str = ""
    impressions: int = 0
    completions: int = 0
    clicks: int = 0
    creative_count: int = 0
    targeting_rule_count: int = 0
    created_at: datetime | None = None
    model_config = {"from_attributes": True}


# ---- Creative ----

class DemandCreativeCreate(BaseModel):
    title: str = Field(..., max_length=255, description="Creative title")
    description: str | None = None
    video_url: str = Field(..., max_length=1024, description="Video file URL (MP4/HLS)")
    vast_url: str | None = Field(
        None, max_length=1024,
        description="Third-party VAST wrapper URL (leave empty for direct serve)",
    )
    companion_image_url: str | None = Field(None, max_length=1024)
    landing_url: str = Field(..., max_length=1024, description="Click-through URL")
    creative_type: int = Field(1, description="1 = CTV Video, 2 = In-App Video")
    duration: int = Field(30, ge=1, le=120, description="Video duration (seconds)")
    width: int = Field(1920, description="Video width (px)")
    height: int = Field(1080, description="Video height (px)")
    bitrate: int | None = Field(None, description="Video bitrate (kbps)")
    mime_type: str = Field("video/mp4", description="MIME type")
    skippable: bool = Field(True, description="Allow skip")
    skip_after: int = Field(5, ge=0, description="Allow skip after N seconds")
    placement: int = Field(1, description="1 = pre-roll, 2 = mid-roll, 3 = post-roll")
    quality_score: int = Field(80, ge=0, le=100, description="Creative quality 0-100")


class DemandCreativeUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    video_url: str | None = None
    vast_url: str | None = None
    companion_image_url: str | None = None
    landing_url: str | None = None
    duration: int | None = None
    width: int | None = None
    height: int | None = None
    bitrate: int | None = None
    skippable: bool | None = None
    skip_after: int | None = None
    placement: int | None = None
    quality_score: int | None = None


class DemandCreativeOut(BaseModel):
    id: int
    campaign_id: int
    title: str
    description: str | None = None
    video_url: str
    vast_url: str | None = None
    companion_image_url: str | None = None
    landing_url: str
    creative_type: int
    creative_type_label: str = ""
    duration: int
    width: int
    height: int
    bitrate: int | None = None
    mime_type: str
    skippable: bool
    skip_after: int
    placement: int
    placement_label: str = ""
    quality_score: int
    status: int
    created_at: datetime | None = None
    model_config = {"from_attributes": True}


# ---- Targeting ----

class DemandTargetingCreate(BaseModel):
    rule_type: str = Field(
        ...,
        description=(
            "Rule type: geo, device, environment, app_bundle, "
            "content_genre, daypart"
        ),
    )
    rule_value: dict[str, Any] = Field(
        ...,
        description="Rule value as JSON",
        json_schema_extra={
            "examples": [
                {"countries": ["US", "CA", "GB"]},
                {"os": ["roku", "firetv", "tvos"]},
                {"values": ["ctv"]},
                {"bundles": ["com.pluto.tv"]},
                {"values": ["sports", "entertainment"]},
                {"hours": [18, 19, 20, 21], "days": ["mon", "tue", "wed"]},
            ]
        },
    )
    is_include: bool = Field(True, description="True = include, False = exclude")


class DemandTargetingOut(BaseModel):
    id: int
    campaign_id: int
    rule_type: str
    rule_value: dict[str, Any]
    is_include: bool
    created_at: datetime | None = None
    model_config = {"from_attributes": True}


# ---- Stats / Dashboard ----

class CampaignStatsOut(BaseModel):
    campaign_id: int
    campaign_name: str
    environment: int | None = None
    impressions: int = 0
    starts: int = 0
    completions: int = 0
    clicks: int = 0
    skips: int = 0
    spend: float = 0.0
    cpm: float = 0.0
    ctr: float = 0.0
    vtr: float = 0.0
    fill_rate: float = 0.0


class SpendStatusOut(BaseModel):
    campaign_id: int
    campaign_name: str
    bid_amount: float
    budget_daily: float
    budget_total: float
    spent_today: float
    spent_total: float
    daily_remaining: float
    total_remaining: float
    daily_pacing_pct: float = Field(0.0, description="% of daily budget consumed")
    status: str


class DashboardOut(BaseModel):
    advertiser_id: int
    name: str
    balance: float
    total_campaigns: int
    active_campaigns: int
    total_spend: float
    today_spend: float
    total_impressions: int
    total_clicks: int
    total_completions: int
    overall_ctr: float
    overall_vtr: float
    campaigns: list[CampaignStatsOut]


class IntegrationEndpointsOut(BaseModel):
    vast_tag_url: str = Field(..., description="VAST tag endpoint (GET)")
    vast_tag_builder: str = Field(..., description="VAST tag builder (POST)")
    openrtb_bid: str = Field(..., description="OpenRTB 2.6 bid endpoint (POST)")
    event_tracking: str = Field(..., description="Event tracking base URL")
    admin_api: str = Field(..., description="Admin CRUD base URL")
    analytics_api: str = Field(..., description="Analytics/reporting base URL")
    health: str = Field(..., description="Health check endpoint")


# ============================================================================
# Helpers
# ============================================================================

_ENV_LABELS = {1: "CTV", 2: "In-App", None: "All"}
_STATUS_LABELS = {0: "Inactive", 1: "Active", 2: "Paused", 3: "Deleted"}
_CREATIVE_TYPE_LABELS = {1: "CTV Video", 2: "In-App Video"}
_PLACEMENT_LABELS = {1: "Pre-Roll", 2: "Mid-Roll", 3: "Post-Roll"}


def _enrich_campaign(campaign: Campaign) -> DemandCampaignOut:
    """Convert Campaign ORM to response with labels."""
    return DemandCampaignOut(
        id=campaign.id,
        name=campaign.name,
        description=campaign.description,
        environment=campaign.environment,
        environment_label=_ENV_LABELS.get(campaign.environment, "Unknown"),
        bid_amount=float(campaign.bid_amount),
        budget_daily=float(campaign.budget_daily),
        budget_total=float(campaign.budget_total),
        spent_today=float(campaign.spent_today),
        spent_total=float(campaign.spent_total),
        freq_cap_daily=campaign.freq_cap_daily,
        freq_cap_hourly=campaign.freq_cap_hourly,
        start_time=campaign.start_time,
        end_time=campaign.end_time,
        status=campaign.status,
        status_label=_STATUS_LABELS.get(campaign.status, "Unknown"),
        impressions=campaign.impressions,
        completions=campaign.completions,
        clicks=campaign.clicks,
        creative_count=len(campaign.creatives) if campaign.creatives else 0,
        targeting_rule_count=len(campaign.targeting_rules) if campaign.targeting_rules else 0,
        created_at=campaign.created_at,
    )


def _enrich_creative(creative: Creative) -> DemandCreativeOut:
    """Convert Creative ORM to response with labels."""
    return DemandCreativeOut(
        id=creative.id,
        campaign_id=creative.campaign_id,
        title=creative.title,
        description=creative.description,
        video_url=creative.video_url,
        vast_url=creative.vast_url,
        companion_image_url=creative.companion_image_url,
        landing_url=creative.landing_url,
        creative_type=creative.creative_type,
        creative_type_label=_CREATIVE_TYPE_LABELS.get(creative.creative_type, "Unknown"),
        duration=creative.duration,
        width=creative.width,
        height=creative.height,
        bitrate=creative.bitrate,
        mime_type=creative.mime_type,
        skippable=creative.skippable,
        skip_after=creative.skip_after,
        placement=creative.placement,
        placement_label=_PLACEMENT_LABELS.get(creative.placement, "Unknown"),
        quality_score=creative.quality_score,
        status=creative.status,
        created_at=creative.created_at,
    )


async def _get_or_404(session: AsyncSession, model: type, entity_id: int, label: str = "Entity"):
    result = await session.execute(select(model).where(model.id == entity_id))
    obj = result.scalar_one_or_none()
    if not obj:
        raise HTTPException(status_code=404, detail=f"{label} {entity_id} not found")
    return obj


async def _verify_campaign_owner(session: AsyncSession, camp_id: int, adv_id: int) -> Campaign:
    """Get campaign and verify it belongs to the advertiser."""
    campaign = await _get_or_404(session, Campaign, camp_id, "Campaign")
    if campaign.advertiser_id != adv_id:
        raise HTTPException(status_code=403, detail="Campaign does not belong to this advertiser")
    return campaign


def _apply_updates(obj: Any, updates: BaseModel) -> None:
    for field_name, value in updates.model_dump(exclude_unset=True).items():
        if value is not None:
            if isinstance(value, float):
                value = Decimal(str(value))
            setattr(obj, field_name, value)


# ============================================================================
# ONBOARDING – Register & manage demand account
# ============================================================================

@router.post(
    "/register",
    response_model=DemandAccountOut,
    status_code=201,
    summary="Register new demand partner",
    description="Create a new advertiser/DSP account and receive an advertiser ID.",
)
async def register_demand(
    body: DemandRegisterRequest,
    session: AsyncSession = Depends(get_session),
) -> Any:
    adv = Advertiser(
        name=body.name,
        company=body.company,
        contact_email=body.contact_email,
        balance=Decimal(str(body.initial_balance)),
        daily_budget=Decimal(str(body.daily_budget)),
        status=ModelStatus.ACTIVE,
    )
    session.add(adv)
    await session.flush()
    await session.refresh(adv)

    logger.info("Demand partner registered", advertiser_id=adv.id, name=adv.name)
    return DemandAccountOut(
        id=adv.id,
        name=adv.name,
        company=adv.company,
        contact_email=adv.contact_email,
        balance=float(adv.balance),
        daily_budget=float(adv.daily_budget),
        status=adv.status,
        created_at=adv.created_at,
    )


@router.get(
    "/account/{adv_id}",
    response_model=DemandAccountOut,
    summary="Get demand account details",
)
async def get_account(
    adv_id: int,
    session: AsyncSession = Depends(get_session),
) -> Any:
    adv = await _get_or_404(session, Advertiser, adv_id, "Advertiser")

    # Count campaigns
    total_q = await session.execute(
        select(func.count()).select_from(Campaign).where(Campaign.advertiser_id == adv_id)
    )
    active_q = await session.execute(
        select(func.count()).select_from(Campaign).where(
            Campaign.advertiser_id == adv_id,
            Campaign.status == ModelStatus.ACTIVE,
        )
    )

    return DemandAccountOut(
        id=adv.id,
        name=adv.name,
        company=adv.company,
        contact_email=adv.contact_email,
        balance=float(adv.balance),
        daily_budget=float(adv.daily_budget),
        status=adv.status,
        total_campaigns=total_q.scalar() or 0,
        active_campaigns=active_q.scalar() or 0,
        created_at=adv.created_at,
    )


@router.put(
    "/account/{adv_id}",
    response_model=DemandAccountOut,
    summary="Update demand account",
)
async def update_account(
    adv_id: int,
    body: DemandAccountUpdate,
    session: AsyncSession = Depends(get_session),
) -> Any:
    adv = await _get_or_404(session, Advertiser, adv_id, "Advertiser")
    _apply_updates(adv, body)
    await session.flush()
    await session.refresh(adv)
    logger.info("Demand account updated", advertiser_id=adv_id)
    return await get_account(adv_id, session)


@router.post(
    "/account/{adv_id}/fund",
    response_model=DemandAccountOut,
    summary="Add funds to demand account",
    description="Increase the advertiser balance. Simulates a payment deposit.",
)
async def fund_account(
    adv_id: int,
    body: DemandFundRequest,
    session: AsyncSession = Depends(get_session),
) -> Any:
    adv = await _get_or_404(session, Advertiser, adv_id, "Advertiser")
    adv.balance = adv.balance + Decimal(str(body.amount))
    await session.flush()
    await session.refresh(adv)
    logger.info("Demand account funded", advertiser_id=adv_id, amount=body.amount, new_balance=float(adv.balance))
    return await get_account(adv_id, session)


# ============================================================================
# CAMPAIGNS
# ============================================================================

@router.post(
    "/campaigns",
    response_model=DemandCampaignOut,
    status_code=201,
    summary="Create campaign",
    description=(
        "Create a new CPM video campaign. Set environment to 1 (CTV), "
        "2 (In-App), or null (both). After creation, add creatives and targeting rules."
    ),
)
async def create_campaign(
    body: DemandCampaignCreate,
    advertiser_id: int = Query(..., description="Your advertiser ID"),
    session: AsyncSession = Depends(get_session),
) -> Any:
    await _get_or_404(session, Advertiser, advertiser_id, "Advertiser")

    campaign = Campaign(
        advertiser_id=advertiser_id,
        name=body.name,
        description=body.description,
        environment=body.environment,
        bid_amount=Decimal(str(body.bid_amount)),
        budget_daily=Decimal(str(body.budget_daily)),
        budget_total=Decimal(str(body.budget_total)),
        freq_cap_daily=body.freq_cap_daily,
        freq_cap_hourly=body.freq_cap_hourly,
        start_time=body.start_time,
        end_time=body.end_time,
        status=ModelStatus.ACTIVE,
    )
    session.add(campaign)
    await session.flush()
    await session.refresh(campaign)
    logger.info("Demand campaign created", campaign_id=campaign.id, advertiser_id=advertiser_id)
    return _enrich_campaign(campaign)


@router.get(
    "/campaigns",
    response_model=list[DemandCampaignOut],
    summary="List campaigns",
)
async def list_campaigns(
    advertiser_id: int = Query(..., description="Your advertiser ID"),
    status: int | None = Query(None, description="Filter by status"),
    environment: int | None = Query(None, description="Filter by environment (1=CTV, 2=In-App)"),
    session: AsyncSession = Depends(get_session),
) -> Any:
    q = select(Campaign).where(Campaign.advertiser_id == advertiser_id)
    if status is not None:
        q = q.where(Campaign.status == status)
    if environment is not None:
        q = q.where(Campaign.environment == environment)
    q = q.order_by(Campaign.id.desc())
    result = await session.execute(q)
    return [_enrich_campaign(c) for c in result.scalars().all()]


@router.get(
    "/campaigns/{camp_id}",
    response_model=DemandCampaignOut,
    summary="Get campaign detail",
)
async def get_campaign(
    camp_id: int,
    advertiser_id: int = Query(..., description="Your advertiser ID"),
    session: AsyncSession = Depends(get_session),
) -> Any:
    campaign = await _verify_campaign_owner(session, camp_id, advertiser_id)
    return _enrich_campaign(campaign)


@router.put(
    "/campaigns/{camp_id}",
    response_model=DemandCampaignOut,
    summary="Update campaign",
)
async def update_campaign(
    camp_id: int,
    body: DemandCampaignUpdate,
    advertiser_id: int = Query(..., description="Your advertiser ID"),
    session: AsyncSession = Depends(get_session),
) -> Any:
    campaign = await _verify_campaign_owner(session, camp_id, advertiser_id)
    _apply_updates(campaign, body)
    await session.flush()
    await session.refresh(campaign)
    logger.info("Demand campaign updated", campaign_id=camp_id)
    return _enrich_campaign(campaign)


@router.patch(
    "/campaigns/{camp_id}/status",
    response_model=DemandCampaignOut,
    summary="Pause / resume / deactivate campaign",
)
async def update_campaign_status(
    camp_id: int,
    body: DemandStatusUpdate,
    advertiser_id: int = Query(..., description="Your advertiser ID"),
    session: AsyncSession = Depends(get_session),
) -> Any:
    campaign = await _verify_campaign_owner(session, camp_id, advertiser_id)
    campaign.status = body.status
    await session.flush()
    await session.refresh(campaign)
    logger.info("Demand campaign status changed", campaign_id=camp_id, status=body.status)
    return _enrich_campaign(campaign)


@router.patch(
    "/campaigns/{camp_id}/bid",
    response_model=DemandCampaignOut,
    summary="Update CPM bid",
)
async def update_campaign_bid(
    camp_id: int,
    body: DemandBidUpdate,
    advertiser_id: int = Query(..., description="Your advertiser ID"),
    session: AsyncSession = Depends(get_session),
) -> Any:
    campaign = await _verify_campaign_owner(session, camp_id, advertiser_id)
    campaign.bid_amount = Decimal(str(body.bid_amount))
    await session.flush()
    await session.refresh(campaign)
    logger.info("Demand CPM bid updated", campaign_id=camp_id, new_bid=body.bid_amount)
    return _enrich_campaign(campaign)


@router.patch(
    "/campaigns/{camp_id}/budget",
    response_model=DemandCampaignOut,
    summary="Update budget",
)
async def update_campaign_budget(
    camp_id: int,
    body: DemandBudgetUpdate,
    advertiser_id: int = Query(..., description="Your advertiser ID"),
    session: AsyncSession = Depends(get_session),
) -> Any:
    campaign = await _verify_campaign_owner(session, camp_id, advertiser_id)
    if body.budget_daily is not None:
        campaign.budget_daily = Decimal(str(body.budget_daily))
    if body.budget_total is not None:
        campaign.budget_total = Decimal(str(body.budget_total))
    await session.flush()
    await session.refresh(campaign)
    logger.info("Demand budget updated", campaign_id=camp_id)
    return _enrich_campaign(campaign)


# ============================================================================
# CREATIVES
# ============================================================================

@router.post(
    "/campaigns/{camp_id}/creatives",
    response_model=DemandCreativeOut,
    status_code=201,
    summary="Add creative to campaign",
)
async def create_creative(
    camp_id: int,
    body: DemandCreativeCreate,
    advertiser_id: int = Query(..., description="Your advertiser ID"),
    session: AsyncSession = Depends(get_session),
) -> Any:
    await _verify_campaign_owner(session, camp_id, advertiser_id)

    creative = Creative(
        campaign_id=camp_id,
        title=body.title,
        description=body.description,
        video_url=body.video_url,
        vast_url=body.vast_url,
        companion_image_url=body.companion_image_url,
        landing_url=body.landing_url,
        creative_type=body.creative_type,
        duration=body.duration,
        width=body.width,
        height=body.height,
        bitrate=body.bitrate,
        mime_type=body.mime_type,
        skippable=body.skippable,
        skip_after=body.skip_after,
        placement=body.placement,
        quality_score=body.quality_score,
        status=ModelStatus.ACTIVE,
    )
    session.add(creative)
    await session.flush()
    await session.refresh(creative)
    logger.info("Demand creative added", creative_id=creative.id, campaign_id=camp_id)
    return _enrich_creative(creative)


@router.get(
    "/campaigns/{camp_id}/creatives",
    response_model=list[DemandCreativeOut],
    summary="List creatives for campaign",
)
async def list_creatives(
    camp_id: int,
    advertiser_id: int = Query(..., description="Your advertiser ID"),
    session: AsyncSession = Depends(get_session),
) -> Any:
    await _verify_campaign_owner(session, camp_id, advertiser_id)
    result = await session.execute(
        select(Creative)
        .where(Creative.campaign_id == camp_id, Creative.status != ModelStatus.DELETED)
        .order_by(Creative.id)
    )
    return [_enrich_creative(c) for c in result.scalars().all()]


@router.put(
    "/creatives/{creative_id}",
    response_model=DemandCreativeOut,
    summary="Update creative",
)
async def update_creative(
    creative_id: int,
    body: DemandCreativeUpdate,
    advertiser_id: int = Query(..., description="Your advertiser ID"),
    session: AsyncSession = Depends(get_session),
) -> Any:
    creative = await _get_or_404(session, Creative, creative_id, "Creative")
    # Verify ownership
    campaign = await _get_or_404(session, Campaign, creative.campaign_id, "Campaign")
    if campaign.advertiser_id != advertiser_id:
        raise HTTPException(status_code=403, detail="Creative does not belong to this advertiser")

    _apply_updates(creative, body)
    await session.flush()
    await session.refresh(creative)
    logger.info("Demand creative updated", creative_id=creative_id)
    return _enrich_creative(creative)


@router.delete(
    "/creatives/{creative_id}",
    status_code=204,
    summary="Remove creative",
)
async def delete_creative(
    creative_id: int,
    advertiser_id: int = Query(..., description="Your advertiser ID"),
    session: AsyncSession = Depends(get_session),
) -> None:
    creative = await _get_or_404(session, Creative, creative_id, "Creative")
    campaign = await _get_or_404(session, Campaign, creative.campaign_id, "Campaign")
    if campaign.advertiser_id != advertiser_id:
        raise HTTPException(status_code=403, detail="Creative does not belong to this advertiser")
    creative.status = ModelStatus.DELETED
    await session.flush()
    logger.info("Demand creative deleted", creative_id=creative_id)


# ============================================================================
# TARGETING
# ============================================================================

@router.post(
    "/campaigns/{camp_id}/targeting",
    response_model=DemandTargetingOut,
    status_code=201,
    summary="Add targeting rule",
    description=(
        "Add a targeting rule to a campaign. Supported rule types:\n"
        "- **geo**: `{\"countries\": [\"US\", \"CA\"], \"dma\": [\"501\"]}`\n"
        "- **device**: `{\"os\": [\"roku\", \"firetv\", \"tvos\"]}`\n"
        "- **environment**: `{\"values\": [\"ctv\"]}` or `{\"values\": [\"inapp\"]}`\n"
        "- **app_bundle**: `{\"bundles\": [\"com.pluto.tv\"]}`\n"
        "- **content_genre**: `{\"values\": [\"sports\", \"news\"]}`\n"
        "- **daypart**: `{\"hours\": [18,19,20], \"days\": [\"mon\",\"tue\"]}`"
    ),
)
async def create_targeting(
    camp_id: int,
    body: DemandTargetingCreate,
    advertiser_id: int = Query(..., description="Your advertiser ID"),
    session: AsyncSession = Depends(get_session),
) -> Any:
    await _verify_campaign_owner(session, camp_id, advertiser_id)

    rule = TargetingRule(
        campaign_id=camp_id,
        rule_type=body.rule_type,
        rule_value=body.rule_value,
        is_include=body.is_include,
    )
    session.add(rule)
    await session.flush()
    await session.refresh(rule)
    logger.info("Demand targeting added", rule_id=rule.id, campaign_id=camp_id, type=body.rule_type)
    return DemandTargetingOut(
        id=rule.id,
        campaign_id=rule.campaign_id,
        rule_type=rule.rule_type,
        rule_value=rule.rule_value,
        is_include=rule.is_include,
        created_at=rule.created_at,
    )


@router.get(
    "/campaigns/{camp_id}/targeting",
    response_model=list[DemandTargetingOut],
    summary="List targeting rules for campaign",
)
async def list_targeting(
    camp_id: int,
    advertiser_id: int = Query(..., description="Your advertiser ID"),
    session: AsyncSession = Depends(get_session),
) -> Any:
    await _verify_campaign_owner(session, camp_id, advertiser_id)
    result = await session.execute(
        select(TargetingRule)
        .where(TargetingRule.campaign_id == camp_id)
        .order_by(TargetingRule.id)
    )
    rules = result.scalars().all()
    return [
        DemandTargetingOut(
            id=r.id,
            campaign_id=r.campaign_id,
            rule_type=r.rule_type,
            rule_value=r.rule_value,
            is_include=r.is_include,
            created_at=r.created_at,
        )
        for r in rules
    ]


@router.delete(
    "/targeting/{rule_id}",
    status_code=204,
    summary="Delete targeting rule",
)
async def delete_targeting(
    rule_id: int,
    advertiser_id: int = Query(..., description="Your advertiser ID"),
    session: AsyncSession = Depends(get_session),
) -> None:
    rule = await _get_or_404(session, TargetingRule, rule_id, "Targeting rule")
    campaign = await _get_or_404(session, Campaign, rule.campaign_id, "Campaign")
    if campaign.advertiser_id != advertiser_id:
        raise HTTPException(status_code=403, detail="Targeting rule does not belong to this advertiser")
    await session.delete(rule)
    await session.flush()
    logger.info("Demand targeting deleted", rule_id=rule_id)


# ============================================================================
# REPORTING
# ============================================================================

@router.get(
    "/campaigns/{camp_id}/stats",
    response_model=CampaignStatsOut,
    summary="Campaign performance stats",
    description="Returns aggregated performance stats from hourly_stats table.",
)
async def campaign_stats(
    camp_id: int,
    advertiser_id: int = Query(..., description="Your advertiser ID"),
    session: AsyncSession = Depends(get_session),
) -> Any:
    campaign = await _verify_campaign_owner(session, camp_id, advertiser_id)

    # Aggregate from hourly_stats
    result = await session.execute(
        select(
            func.coalesce(func.sum(HourlyStat.impressions), 0).label("impressions"),
            func.coalesce(func.sum(HourlyStat.starts), 0).label("starts"),
            func.coalesce(func.sum(HourlyStat.completions), 0).label("completions"),
            func.coalesce(func.sum(HourlyStat.clicks), 0).label("clicks"),
            func.coalesce(func.sum(HourlyStat.skips), 0).label("skips"),
            func.coalesce(func.sum(HourlyStat.spend), 0).label("spend"),
            func.coalesce(func.sum(HourlyStat.ad_requests), 0).label("ad_requests"),
        ).where(HourlyStat.campaign_id == camp_id)
    )
    row = result.one()

    impressions = int(row.impressions)
    clicks = int(row.clicks)
    completions = int(row.completions)
    starts = int(row.starts)
    spend = float(row.spend)
    ad_requests = int(row.ad_requests)

    return CampaignStatsOut(
        campaign_id=camp_id,
        campaign_name=campaign.name,
        environment=campaign.environment,
        impressions=impressions,
        starts=starts,
        completions=completions,
        clicks=clicks,
        skips=int(row.skips),
        spend=round(spend, 4),
        cpm=round((spend / impressions * 1000) if impressions > 0 else 0, 4),
        ctr=round((clicks / impressions * 100) if impressions > 0 else 0, 4),
        vtr=round((completions / starts * 100) if starts > 0 else 0, 4),
        fill_rate=round((impressions / ad_requests * 100) if ad_requests > 0 else 0, 4),
    )


@router.get(
    "/campaigns/{camp_id}/spend",
    response_model=SpendStatusOut,
    summary="Budget & spend status",
)
async def campaign_spend(
    camp_id: int,
    advertiser_id: int = Query(..., description="Your advertiser ID"),
    session: AsyncSession = Depends(get_session),
) -> Any:
    campaign = await _verify_campaign_owner(session, camp_id, advertiser_id)

    budget_daily = float(campaign.budget_daily)
    budget_total = float(campaign.budget_total)
    spent_today = float(campaign.spent_today)
    spent_total = float(campaign.spent_total)

    return SpendStatusOut(
        campaign_id=camp_id,
        campaign_name=campaign.name,
        bid_amount=float(campaign.bid_amount),
        budget_daily=budget_daily,
        budget_total=budget_total,
        spent_today=spent_today,
        spent_total=spent_total,
        daily_remaining=max(budget_daily - spent_today, 0),
        total_remaining=max(budget_total - spent_total, 0),
        daily_pacing_pct=round(
            (spent_today / budget_daily * 100) if budget_daily > 0 else 0, 2
        ),
        status=_STATUS_LABELS.get(campaign.status, "Unknown"),
    )


@router.get(
    "/dashboard",
    response_model=DashboardOut,
    summary="Demand dashboard overview",
    description="Returns an aggregate dashboard across all campaigns for an advertiser.",
)
async def demand_dashboard(
    advertiser_id: int = Query(..., description="Your advertiser ID"),
    session: AsyncSession = Depends(get_session),
) -> Any:
    adv = await _get_or_404(session, Advertiser, advertiser_id, "Advertiser")

    # Get all campaigns
    result = await session.execute(
        select(Campaign)
        .where(Campaign.advertiser_id == advertiser_id)
        .order_by(Campaign.id)
    )
    campaigns = result.scalars().all()

    total_spend = 0.0
    today_spend = 0.0
    total_imps = 0
    total_clicks = 0
    total_completions = 0
    active_count = 0
    campaign_stats_list: list[CampaignStatsOut] = []

    for c in campaigns:
        if c.status == ModelStatus.ACTIVE:
            active_count += 1

        imps = c.impressions or 0
        clicks = c.clicks or 0
        completions = c.completions or 0
        spent_total = float(c.spent_total)
        spent_today = float(c.spent_today)

        total_spend += spent_total
        today_spend += spent_today
        total_imps += imps
        total_clicks += clicks
        total_completions += completions

        campaign_stats_list.append(
            CampaignStatsOut(
                campaign_id=c.id,
                campaign_name=c.name,
                environment=c.environment,
                impressions=imps,
                completions=completions,
                clicks=clicks,
                spend=round(spent_total, 4),
                cpm=float(c.bid_amount),
                ctr=round((clicks / imps * 100) if imps > 0 else 0, 4),
                vtr=round((completions / imps * 100) if imps > 0 else 0, 4),
            )
        )

    return DashboardOut(
        advertiser_id=adv.id,
        name=adv.name,
        balance=float(adv.balance),
        total_campaigns=len(campaigns),
        active_campaigns=active_count,
        total_spend=round(total_spend, 4),
        today_spend=round(today_spend, 4),
        total_impressions=total_imps,
        total_clicks=total_clicks,
        total_completions=total_completions,
        overall_ctr=round((total_clicks / total_imps * 100) if total_imps > 0 else 0, 4),
        overall_vtr=round((total_completions / total_imps * 100) if total_imps > 0 else 0, 4),
        campaigns=campaign_stats_list,
    )


# ============================================================================
# INTEGRATION ENDPOINTS
# ============================================================================

@router.get(
    "/integration/endpoints",
    response_model=IntegrationEndpointsOut,
    summary="Get integration endpoints",
    description=(
        "Returns all API endpoint URLs that a demand partner needs for integration: "
        "VAST tag, OpenRTB bid, event tracking, admin, and analytics."
    ),
)
async def integration_endpoints(
    request: Any = None,
    base_url: str = Query(
        "http://localhost:8000",
        description="Override base URL (use your public server URL)",
    ),
) -> IntegrationEndpointsOut:
    base = base_url.rstrip("/")

    return IntegrationEndpointsOut(
        vast_tag_url=f"{base}/api/vast?sid={{SLOT_ID}}&w=1920&h=1080&max_dur=30&ip={{IP}}&ua={{UA}}&ifa={{IFA}}&os={{OS}}&cb={{CACHEBUSTER}}",
        vast_tag_builder=f"{base}/api/vast/tag-builder",
        openrtb_bid=f"{base}/api/v1/openrtb/bid",
        event_tracking=f"{base}/api/v1/event/track?type={{EVENT}}&req={{REQUEST_ID}}&ad={{AD_ID}}",
        admin_api=f"{base}/api/v1/admin",
        analytics_api=f"{base}/api/v1/analytics",
        health=f"{base}/health",
    )
