"""
Admin CRUD router – Manage advertisers, campaigns, creatives, and targeting.

This is the "add-demand" interface: create advertisers, set up CPM campaigns
with budgets and targeting rules, and upload video creatives.

Endpoints:
    ADVERTISERS
        POST   /api/v1/admin/advertisers                – Create advertiser
        GET    /api/v1/admin/advertisers                – List advertisers
        GET    /api/v1/admin/advertisers/{id}           – Get advertiser
        PUT    /api/v1/admin/advertisers/{id}           – Update advertiser
        DELETE /api/v1/admin/advertisers/{id}           – Delete (soft) advertiser

    CAMPAIGNS
        POST   /api/v1/admin/campaigns                  – Create campaign
        GET    /api/v1/admin/campaigns                  – List campaigns
        GET    /api/v1/admin/campaigns/{id}             – Get campaign + creatives
        PUT    /api/v1/admin/campaigns/{id}             – Update campaign
        PATCH  /api/v1/admin/campaigns/{id}/status      – Pause / resume / archive
        DELETE /api/v1/admin/campaigns/{id}             – Soft-delete campaign

    CREATIVES
        POST   /api/v1/admin/creatives                  – Add creative to campaign
        GET    /api/v1/admin/creatives/{id}             – Get creative
        PUT    /api/v1/admin/creatives/{id}             – Update creative
        DELETE /api/v1/admin/creatives/{id}             – Soft-delete creative

    TARGETING
        POST   /api/v1/admin/campaigns/{id}/targeting   – Add targeting rule
        GET    /api/v1/admin/campaigns/{id}/targeting   – List targeting rules
        DELETE /api/v1/admin/targeting/{rule_id}        – Delete targeting rule
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from liteads.common.database import get_session
from liteads.common.logger import get_logger
from liteads.models import (
    Advertiser,
    Campaign,
    Creative,
    Status as ModelStatus,
    TargetingRule,
)

logger = get_logger(__name__)
router = APIRouter()


# ============================================================================
# Pydantic schemas (request / response)
# ============================================================================

# ---- Advertiser ----

class AdvertiserCreate(BaseModel):
    name: str = Field(..., max_length=255, description="Advertiser name")
    company: str | None = Field(None, max_length=255)
    contact_email: str | None = Field(None, max_length=255)
    balance: float = Field(0.0, ge=0, description="Account balance (USD)")
    daily_budget: float = Field(0.0, ge=0, description="Daily spend limit (USD)")


class AdvertiserUpdate(BaseModel):
    name: str | None = None
    company: str | None = None
    contact_email: str | None = None
    balance: float | None = None
    daily_budget: float | None = None
    status: int | None = Field(None, description="0=inactive, 1=active, 2=paused, 3=deleted")


class AdvertiserOut(BaseModel):
    id: int
    name: str
    company: str | None = None
    contact_email: str | None = None
    balance: float
    daily_budget: float
    status: int
    created_at: datetime | None = None
    model_config = {"from_attributes": True}


# ---- Campaign ----

class CampaignCreate(BaseModel):
    advertiser_id: int = Field(..., description="Parent advertiser ID")
    name: str = Field(..., max_length=255)
    description: str | None = None
    budget_daily: float = Field(0.0, ge=0)
    budget_total: float = Field(0.0, ge=0)
    bid_amount: float = Field(..., gt=0, description="CPM bid (USD)")
    bid_floor: float = Field(0.0, ge=0, description="Minimum CPM floor (USD)")
    floor_config: dict[str, Any] | None = Field(
        None,
        description="Dynamic floor rules: {geo: {US: 5.0}, daypart: {prime: 8.0}, app: {com.roku: 6.0}}",
    )
    adomain: str | None = Field(None, max_length=255, description="Advertiser domain for competitive separation")
    iab_categories: list[str] | None = Field(None, description="IAB content categories for competitive separation")
    environment: int | None = Field(None, description="1=CTV, 2=INAPP, null=both")
    freq_cap_daily: int = Field(10, ge=0)
    freq_cap_hourly: int = Field(3, ge=0)
    start_time: datetime | None = None
    end_time: datetime | None = None


class CampaignUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    budget_daily: float | None = None
    budget_total: float | None = None
    bid_amount: float | None = None
    bid_floor: float | None = None
    floor_config: dict[str, Any] | None = None
    adomain: str | None = None
    iab_categories: list[str] | None = None
    environment: int | None = None
    freq_cap_daily: int | None = None
    freq_cap_hourly: int | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None


class CampaignStatusUpdate(BaseModel):
    status: int = Field(..., description="0=inactive, 1=active, 2=paused, 3=deleted")


class CampaignOut(BaseModel):
    id: int
    advertiser_id: int
    name: str
    description: str | None = None
    budget_daily: float
    budget_total: float
    spent_today: float
    spent_total: float
    bid_amount: float
    bid_floor: float = 0.0
    floor_config: dict[str, Any] | None = None
    adomain: str | None = None
    iab_categories: list[str] | None = None
    environment: int | None = None
    freq_cap_daily: int
    freq_cap_hourly: int
    start_time: datetime | None = None
    end_time: datetime | None = None
    status: int
    impressions: int
    completions: int
    clicks: int
    created_at: datetime | None = None
    model_config = {"from_attributes": True}


# ---- Creative ----

class CreativeCreate(BaseModel):
    campaign_id: int = Field(..., description="Parent campaign ID")
    title: str = Field(..., max_length=255)
    description: str | None = None
    video_url: str = Field(..., max_length=1024, description="Video file URL (MP4/HLS/DASH)")
    vast_url: str | None = Field(None, max_length=1024, description="Third-party VAST wrapper URL")
    companion_image_url: str | None = Field(None, max_length=1024)
    landing_url: str = Field(..., max_length=1024, description="Click-through landing page")
    creative_type: int = Field(1, description="1=CTV_VIDEO, 2=INAPP_VIDEO")
    duration: int = Field(30, ge=1, description="Video duration (seconds)")
    width: int = Field(1920)
    height: int = Field(1080)
    bitrate: int | None = None
    mime_type: str = Field("video/mp4")
    skippable: bool = Field(True)
    skip_after: int = Field(5, ge=0)
    placement: int = Field(1, description="1=pre-roll, 2=mid-roll, 3=post-roll")
    quality_score: int = Field(80, ge=0, le=100)


class CreativeUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    video_url: str | None = None
    vast_url: str | None = None
    companion_image_url: str | None = None
    landing_url: str | None = None
    creative_type: int | None = None
    duration: int | None = None
    width: int | None = None
    height: int | None = None
    bitrate: int | None = None
    mime_type: str | None = None
    skippable: bool | None = None
    skip_after: int | None = None
    placement: int | None = None
    quality_score: int | None = None
    status: int | None = None


class CreativeOut(BaseModel):
    id: int
    campaign_id: int
    title: str
    description: str | None = None
    video_url: str
    vast_url: str | None = None
    companion_image_url: str | None = None
    landing_url: str
    creative_type: int
    duration: int
    width: int
    height: int
    bitrate: int | None = None
    mime_type: str
    skippable: bool
    skip_after: int
    placement: int
    quality_score: int
    status: int
    created_at: datetime | None = None
    model_config = {"from_attributes": True}


# ---- Targeting Rule ----

class TargetingRuleCreate(BaseModel):
    rule_type: str = Field(
        ...,
        description="Rule type: geo, device, app_bundle, content_genre, environment, daypart",
    )
    rule_value: dict[str, Any] = Field(
        ...,
        description="Rule value JSON (e.g. {\"countries\": [\"US\", \"GB\"]})",
    )
    is_include: bool = Field(True, description="True=include, False=exclude")


class TargetingRuleOut(BaseModel):
    id: int
    campaign_id: int
    rule_type: str
    rule_value: dict[str, Any]
    is_include: bool
    model_config = {"from_attributes": True}


# ============================================================================
# Helpers
# ============================================================================

_T = TypeVar("_T")


async def _get_or_404(session: AsyncSession, model: type[_T], entity_id: int, label: str = "Entity") -> _T:
    """Fetch a model instance by ID or raise 404."""
    result = await session.execute(select(model).where(model.id == entity_id))  # type: ignore[arg-type]
    obj = result.scalar_one_or_none()
    if not obj:
        raise HTTPException(status_code=404, detail=f"{label} {entity_id} not found")
    return obj


def _apply_updates(obj: Any, updates: BaseModel) -> None:
    """Apply non-None fields from a Pydantic update model onto an ORM object."""
    for field_name, value in updates.model_dump(exclude_unset=True).items():
        if value is not None:
            col_attr = getattr(type(obj), field_name, None)  # type: ignore[arg-type]
            if isinstance(value, float) and hasattr(col_attr, "type"):
                value = Decimal(str(value))
            setattr(obj, field_name, value)


# ============================================================================
# ADVERTISER endpoints
# ============================================================================

@router.post("/advertisers", response_model=AdvertiserOut, status_code=201, summary="Create advertiser")
async def create_advertiser(
    body: AdvertiserCreate,
    session: AsyncSession = Depends(get_session),
) -> Any:
    adv = Advertiser(
        name=body.name,
        company=body.company,
        contact_email=body.contact_email,
        balance=Decimal(str(body.balance)),
        daily_budget=Decimal(str(body.daily_budget)),
        status=ModelStatus.ACTIVE,
    )
    session.add(adv)
    await session.flush()
    await session.refresh(adv)
    logger.info("Advertiser created", advertiser_id=adv.id, name=adv.name)
    return adv


@router.get("/advertisers", response_model=list[AdvertiserOut], summary="List advertisers")
async def list_advertisers(
    status_filter: int | None = Query(None, alias="status", description="Filter by status"),
    session: AsyncSession = Depends(get_session),
) -> Any:
    q = select(Advertiser)
    if status_filter is not None:
        q = q.where(Advertiser.status == status_filter)
    q = q.order_by(Advertiser.id)
    result = await session.execute(q)
    return result.scalars().all()


@router.get("/advertisers/{adv_id}", response_model=AdvertiserOut, summary="Get advertiser")
async def get_advertiser(adv_id: int, session: AsyncSession = Depends(get_session)) -> Any:
    return await _get_or_404(session, Advertiser, adv_id, "Advertiser")


@router.put("/advertisers/{adv_id}", response_model=AdvertiserOut, summary="Update advertiser")
async def update_advertiser(
    adv_id: int,
    body: AdvertiserUpdate,
    session: AsyncSession = Depends(get_session),
) -> Any:
    adv = await _get_or_404(session, Advertiser, adv_id, "Advertiser")
    _apply_updates(adv, body)
    await session.flush()
    await session.refresh(adv)
    logger.info("Advertiser updated", advertiser_id=adv_id)
    return adv


@router.delete("/advertisers/{adv_id}", status_code=204, summary="Soft-delete advertiser")
async def delete_advertiser(adv_id: int, session: AsyncSession = Depends(get_session)) -> None:
    adv = await _get_or_404(session, Advertiser, adv_id, "Advertiser")
    adv.status = ModelStatus.DELETED
    await session.flush()
    logger.info("Advertiser soft-deleted", advertiser_id=adv_id)


# ============================================================================
# CAMPAIGN endpoints
# ============================================================================

@router.post("/campaigns", response_model=CampaignOut, status_code=201, summary="Create campaign")
async def create_campaign(
    body: CampaignCreate,
    session: AsyncSession = Depends(get_session),
) -> Any:
    # Verify advertiser exists
    await _get_or_404(session, Advertiser, body.advertiser_id, "Advertiser")

    campaign = Campaign(
        advertiser_id=body.advertiser_id,
        name=body.name,
        description=body.description,
        budget_daily=Decimal(str(body.budget_daily)),
        budget_total=Decimal(str(body.budget_total)),
        bid_amount=Decimal(str(body.bid_amount)),
        bid_floor=Decimal(str(body.bid_floor)) if body.bid_floor else Decimal("0"),
        floor_config=body.floor_config,
        adomain=body.adomain,
        iab_categories=body.iab_categories,
        environment=body.environment,
        freq_cap_daily=body.freq_cap_daily,
        freq_cap_hourly=body.freq_cap_hourly,
        start_time=body.start_time,
        end_time=body.end_time,
        status=ModelStatus.ACTIVE,
    )
    session.add(campaign)
    await session.flush()
    await session.refresh(campaign)
    logger.info("Campaign created", campaign_id=campaign.id, name=campaign.name)
    return campaign


@router.get("/campaigns", response_model=list[CampaignOut], summary="List campaigns")
async def list_campaigns(
    advertiser_id: int | None = Query(None, description="Filter by advertiser"),
    status_filter: int | None = Query(None, alias="status", description="Filter by status"),
    session: AsyncSession = Depends(get_session),
) -> Any:
    q = select(Campaign)
    if advertiser_id is not None:
        q = q.where(Campaign.advertiser_id == advertiser_id)
    if status_filter is not None:
        q = q.where(Campaign.status == status_filter)
    q = q.order_by(Campaign.id)
    result = await session.execute(q)
    return result.scalars().all()


@router.get("/campaigns/{camp_id}", response_model=CampaignOut, summary="Get campaign")
async def get_campaign(camp_id: int, session: AsyncSession = Depends(get_session)) -> Any:
    return await _get_or_404(session, Campaign, camp_id, "Campaign")


@router.put("/campaigns/{camp_id}", response_model=CampaignOut, summary="Update campaign")
async def update_campaign(
    camp_id: int,
    body: CampaignUpdate,
    session: AsyncSession = Depends(get_session),
) -> Any:
    campaign = await _get_or_404(session, Campaign, camp_id, "Campaign")
    _apply_updates(campaign, body)
    await session.flush()
    await session.refresh(campaign)
    logger.info("Campaign updated", campaign_id=camp_id)
    return campaign


@router.patch(
    "/campaigns/{camp_id}/status",
    response_model=CampaignOut,
    summary="Change campaign status (pause/resume/archive)",
)
async def update_campaign_status(
    camp_id: int,
    body: CampaignStatusUpdate,
    session: AsyncSession = Depends(get_session),
) -> Any:
    campaign = await _get_or_404(session, Campaign, camp_id, "Campaign")
    campaign.status = body.status
    await session.flush()
    await session.refresh(campaign)
    logger.info("Campaign status changed", campaign_id=camp_id, new_status=body.status)
    return campaign


@router.delete("/campaigns/{camp_id}", status_code=204, summary="Soft-delete campaign")
async def delete_campaign(camp_id: int, session: AsyncSession = Depends(get_session)) -> None:
    campaign = await _get_or_404(session, Campaign, camp_id, "Campaign")
    campaign.status = ModelStatus.DELETED
    await session.flush()
    logger.info("Campaign soft-deleted", campaign_id=camp_id)


# ============================================================================
# CREATIVE endpoints
# ============================================================================

@router.get("/creatives", response_model=list[CreativeOut], summary="List creatives")
async def list_creatives(
    campaign_id: int | None = Query(None, description="Filter by campaign"),
    status_filter: int | None = Query(None, alias="status", description="Filter by status"),
    session: AsyncSession = Depends(get_session),
) -> Any:
    q = select(Creative)
    if campaign_id is not None:
        q = q.where(Creative.campaign_id == campaign_id)
    if status_filter is not None:
        q = q.where(Creative.status == status_filter)
    q = q.order_by(Creative.id)
    result = await session.execute(q)
    return result.scalars().all()


@router.post("/creatives", response_model=CreativeOut, status_code=201, summary="Add creative")
async def create_creative(
    body: CreativeCreate,
    session: AsyncSession = Depends(get_session),
) -> Any:
    # Verify campaign exists
    await _get_or_404(session, Campaign, body.campaign_id, "Campaign")

    creative = Creative(
        campaign_id=body.campaign_id,
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
    logger.info(
        "Creative added",
        creative_id=creative.id,
        campaign_id=body.campaign_id,
        title=creative.title,
    )
    return creative


@router.get("/creatives/{creative_id}", response_model=CreativeOut, summary="Get creative")
async def get_creative(
    creative_id: int, session: AsyncSession = Depends(get_session)
) -> Any:
    return await _get_or_404(session, Creative, creative_id, "Creative")


@router.put("/creatives/{creative_id}", response_model=CreativeOut, summary="Update creative")
async def update_creative(
    creative_id: int,
    body: CreativeUpdate,
    session: AsyncSession = Depends(get_session),
) -> Any:
    creative = await _get_or_404(session, Creative, creative_id, "Creative")
    _apply_updates(creative, body)
    await session.flush()
    await session.refresh(creative)
    logger.info("Creative updated", creative_id=creative_id)
    return creative


@router.delete("/creatives/{creative_id}", status_code=204, summary="Soft-delete creative")
async def delete_creative(
    creative_id: int, session: AsyncSession = Depends(get_session)
) -> None:
    creative = await _get_or_404(session, Creative, creative_id, "Creative")
    creative.status = ModelStatus.DELETED
    await session.flush()
    logger.info("Creative soft-deleted", creative_id=creative_id)


# ============================================================================
# TARGETING RULE endpoints
# ============================================================================

@router.post(
    "/campaigns/{camp_id}/targeting",
    response_model=TargetingRuleOut,
    status_code=201,
    summary="Add targeting rule",
)
async def create_targeting_rule(
    camp_id: int,
    body: TargetingRuleCreate,
    session: AsyncSession = Depends(get_session),
) -> Any:
    await _get_or_404(session, Campaign, camp_id, "Campaign")

    rule = TargetingRule(
        campaign_id=camp_id,
        rule_type=body.rule_type,
        rule_value=body.rule_value,
        is_include=body.is_include,
    )
    session.add(rule)
    await session.flush()
    await session.refresh(rule)
    logger.info("Targeting rule added", rule_id=rule.id, campaign_id=camp_id, type=body.rule_type)
    return rule


@router.get(
    "/campaigns/{camp_id}/targeting",
    response_model=list[TargetingRuleOut],
    summary="List targeting rules for campaign",
)
async def list_targeting_rules(
    camp_id: int,
    session: AsyncSession = Depends(get_session),
) -> Any:
    result = await session.execute(
        select(TargetingRule).where(TargetingRule.campaign_id == camp_id).order_by(TargetingRule.id)
    )
    return result.scalars().all()


@router.delete("/targeting/{rule_id}", status_code=204, summary="Delete targeting rule")
async def delete_targeting_rule(
    rule_id: int,
    session: AsyncSession = Depends(get_session),
) -> None:
    rule = await _get_or_404(session, TargetingRule, rule_id, "Targeting rule")
    await session.delete(rule)
    await session.flush()
    logger.info("Targeting rule deleted", rule_id=rule_id)
