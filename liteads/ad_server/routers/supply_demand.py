"""
Supply & Demand Management Router.

CRUD endpoints for:
  - Supply VAST Tags (publisher-facing)
  - Demand ORTB Endpoints (DSP / bridge ad servers)
  - Demand VAST Tags (third-party VAST demand sources)
  - Supply↔Demand Mappings (targeting supply tags to specific demand sources)
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from liteads.common.database import get_session
from liteads.common.logger import get_logger
from liteads.models import (
    DemandEndpoint,
    DemandVastTag,
    Status as ModelStatus,
    SupplyDemandMapping,
    SupplyTag,
)

logger = get_logger(__name__)
router = APIRouter()


# ============================================================================
# Pydantic schemas
# ============================================================================

# ── Supply Tag ──

class SupplyTagCreate(BaseModel):
    name: str = Field(..., max_length=255, description="Supply tag name")
    description: str | None = None
    slot_id: str = Field(..., max_length=100, description="Unique slot/zone ID for VAST tag URL")
    bid_floor: float = Field(0.0, ge=0, description="Minimum CPM floor ($)")
    margin_pct: float = Field(0.0, ge=0, le=100, description="Margin percentage (0-100)")
    environment: int | None = Field(None, description="1=CTV, 2=INAPP, null=both")
    min_duration: int = Field(5, ge=1, description="Min video duration (s)")
    max_duration: int = Field(30, ge=1, description="Max video duration (s)")
    width: int = Field(1920, description="Video width")
    height: int = Field(1080, description="Video height")


class SupplyTagUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    slot_id: str | None = None
    bid_floor: float | None = None
    margin_pct: float | None = None
    environment: int | None = None
    min_duration: int | None = None
    max_duration: int | None = None
    width: int | None = None
    height: int | None = None
    status: int | None = None


class SupplyTagOut(BaseModel):
    id: int
    name: str
    description: str | None = None
    slot_id: str
    bid_floor: float
    margin_pct: float
    environment: int | None = None
    min_duration: int
    max_duration: int
    width: int
    height: int
    status: int
    demand_count: int = 0
    created_at: datetime | None = None
    model_config = {"from_attributes": True}


# ── Demand Endpoint ──

class DemandEndpointCreate(BaseModel):
    name: str = Field(..., max_length=255, description="Demand endpoint name")
    description: str | None = None
    endpoint_url: str = Field(..., max_length=1024, description="OpenRTB 2.6 bid URL")
    bid_floor: float = Field(0.0, ge=0, description="Minimum CPM floor ($)")
    margin_pct: float = Field(0.0, ge=0, le=100, description="Margin percentage (0-100)")
    timeout_ms: int = Field(500, ge=50, le=10000, description="Timeout (ms)")
    qps_limit: int = Field(0, ge=0, description="Max QPS (0=unlimited)")


class DemandEndpointUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    endpoint_url: str | None = None
    bid_floor: float | None = None
    margin_pct: float | None = None
    timeout_ms: int | None = None
    qps_limit: int | None = None
    status: int | None = None


class DemandEndpointOut(BaseModel):
    id: int
    name: str
    description: str | None = None
    endpoint_url: str
    bid_floor: float
    margin_pct: float
    timeout_ms: int
    qps_limit: int
    status: int
    created_at: datetime | None = None
    model_config = {"from_attributes": True}


# ── Demand VAST Tag ──

class DemandVastTagCreate(BaseModel):
    name: str = Field(..., max_length=255, description="Demand VAST tag name")
    description: str | None = None
    vast_url: str = Field(..., max_length=2048, description="Third-party VAST tag URL")
    bid_floor: float = Field(0.0, ge=0, description="Minimum CPM floor ($)")
    margin_pct: float = Field(0.0, ge=0, le=100, description="Margin percentage (0-100)")
    cpm_value: float = Field(0.0, ge=0, description="Estimated CPM value ($)")


class DemandVastTagUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    vast_url: str | None = None
    bid_floor: float | None = None
    margin_pct: float | None = None
    cpm_value: float | None = None
    status: int | None = None


class DemandVastTagOut(BaseModel):
    id: int
    name: str
    description: str | None = None
    vast_url: str
    bid_floor: float
    margin_pct: float
    cpm_value: float
    status: int
    created_at: datetime | None = None
    model_config = {"from_attributes": True}


# ── Mapping ──

class MappingCreate(BaseModel):
    supply_tag_id: int = Field(..., description="Supply tag ID")
    demand_endpoint_id: int | None = Field(None, description="Demand ORTB endpoint ID")
    demand_vast_tag_id: int | None = Field(None, description="Demand VAST tag ID")
    priority: int = Field(1, ge=1, description="Priority (1=highest)")
    weight: int = Field(100, ge=1, description="Weight for load balancing")


class MappingOut(BaseModel):
    id: int
    supply_tag_id: int
    demand_endpoint_id: int | None = None
    demand_vast_tag_id: int | None = None
    demand_name: str = ""
    demand_type: str = ""
    priority: int
    weight: int
    status: int
    created_at: datetime | None = None
    model_config = {"from_attributes": True}


# ============================================================================
# Helpers
# ============================================================================

async def _get_or_404(session: AsyncSession, model: type, entity_id: int, label: str = "Entity"):
    result = await session.execute(select(model).where(model.id == entity_id))
    obj = result.scalar_one_or_none()
    if not obj:
        raise HTTPException(status_code=404, detail=f"{label} {entity_id} not found")
    return obj


def _apply_updates(obj: Any, updates: BaseModel) -> None:
    for field_name, value in updates.model_dump(exclude_unset=True).items():
        if value is not None and isinstance(value, float):
            value = Decimal(str(value))
        setattr(obj, field_name, value)


def _supply_tag_out(tag: SupplyTag) -> SupplyTagOut:
    return SupplyTagOut(
        id=tag.id,
        name=tag.name,
        description=tag.description,
        slot_id=tag.slot_id,
        bid_floor=float(tag.bid_floor),
        margin_pct=float(tag.margin_pct),
        environment=tag.environment,
        min_duration=tag.min_duration,
        max_duration=tag.max_duration,
        width=tag.width,
        height=tag.height,
        status=tag.status,
        demand_count=len(tag.demand_mappings) if tag.demand_mappings else 0,
        created_at=tag.created_at,
    )


def _mapping_out(m: SupplyDemandMapping) -> MappingOut:
    demand_name = ""
    demand_type = ""
    if m.demand_endpoint:
        demand_name = m.demand_endpoint.name
        demand_type = "ORTB"
    elif m.demand_vast_tag:
        demand_name = m.demand_vast_tag.name
        demand_type = "VAST"
    return MappingOut(
        id=m.id,
        supply_tag_id=m.supply_tag_id,
        demand_endpoint_id=m.demand_endpoint_id,
        demand_vast_tag_id=m.demand_vast_tag_id,
        demand_name=demand_name,
        demand_type=demand_type,
        priority=m.priority,
        weight=m.weight,
        status=m.status,
        created_at=m.created_at,
    )


# ============================================================================
# SUPPLY TAG endpoints
# ============================================================================

@router.post("/supply-tags", response_model=SupplyTagOut, status_code=201, summary="Create supply tag")
async def create_supply_tag(
    body: SupplyTagCreate,
    session: AsyncSession = Depends(get_session),
) -> Any:
    tag = SupplyTag(
        name=body.name,
        description=body.description,
        slot_id=body.slot_id,
        bid_floor=Decimal(str(body.bid_floor)),
        margin_pct=Decimal(str(body.margin_pct)),
        environment=body.environment,
        min_duration=body.min_duration,
        max_duration=body.max_duration,
        width=body.width,
        height=body.height,
        status=ModelStatus.ACTIVE,
    )
    session.add(tag)
    await session.flush()
    await session.refresh(tag)
    logger.info("Supply tag created", tag_id=tag.id, slot_id=tag.slot_id)
    return _supply_tag_out(tag)


@router.get("/supply-tags", response_model=list[SupplyTagOut], summary="List supply tags")
async def list_supply_tags(
    status_filter: int | None = Query(None, alias="status"),
    session: AsyncSession = Depends(get_session),
) -> Any:
    q = select(SupplyTag)
    if status_filter is not None:
        q = q.where(SupplyTag.status == status_filter)
    q = q.order_by(SupplyTag.id)
    result = await session.execute(q)
    return [_supply_tag_out(t) for t in result.scalars().all()]


@router.get("/supply-tags/{tag_id}", response_model=SupplyTagOut, summary="Get supply tag")
async def get_supply_tag(tag_id: int, session: AsyncSession = Depends(get_session)) -> Any:
    tag = await _get_or_404(session, SupplyTag, tag_id, "Supply tag")
    return _supply_tag_out(tag)


@router.put("/supply-tags/{tag_id}", response_model=SupplyTagOut, summary="Update supply tag")
async def update_supply_tag(
    tag_id: int,
    body: SupplyTagUpdate,
    session: AsyncSession = Depends(get_session),
) -> Any:
    tag = await _get_or_404(session, SupplyTag, tag_id, "Supply tag")
    _apply_updates(tag, body)
    await session.flush()
    await session.refresh(tag)
    logger.info("Supply tag updated", tag_id=tag_id)
    return _supply_tag_out(tag)


@router.delete("/supply-tags/{tag_id}", status_code=204, summary="Soft-delete supply tag")
async def delete_supply_tag(tag_id: int, session: AsyncSession = Depends(get_session)) -> None:
    tag = await _get_or_404(session, SupplyTag, tag_id, "Supply tag")
    tag.status = ModelStatus.DELETED
    await session.flush()
    logger.info("Supply tag soft-deleted", tag_id=tag_id)


# ============================================================================
# DEMAND ENDPOINT endpoints
# ============================================================================

@router.post("/demand-endpoints", response_model=DemandEndpointOut, status_code=201, summary="Create demand ORTB endpoint")
async def create_demand_endpoint(
    body: DemandEndpointCreate,
    session: AsyncSession = Depends(get_session),
) -> Any:
    ep = DemandEndpoint(
        name=body.name,
        description=body.description,
        endpoint_url=body.endpoint_url,
        bid_floor=Decimal(str(body.bid_floor)),
        margin_pct=Decimal(str(body.margin_pct)),
        timeout_ms=body.timeout_ms,
        qps_limit=body.qps_limit,
        status=ModelStatus.ACTIVE,
    )
    session.add(ep)
    await session.flush()
    await session.refresh(ep)
    logger.info("Demand endpoint created", ep_id=ep.id, url=ep.endpoint_url)
    return ep


@router.get("/demand-endpoints", response_model=list[DemandEndpointOut], summary="List demand endpoints")
async def list_demand_endpoints(
    status_filter: int | None = Query(None, alias="status"),
    session: AsyncSession = Depends(get_session),
) -> Any:
    q = select(DemandEndpoint)
    if status_filter is not None:
        q = q.where(DemandEndpoint.status == status_filter)
    q = q.order_by(DemandEndpoint.id)
    result = await session.execute(q)
    return result.scalars().all()


@router.get("/demand-endpoints/{ep_id}", response_model=DemandEndpointOut, summary="Get demand endpoint")
async def get_demand_endpoint(ep_id: int, session: AsyncSession = Depends(get_session)) -> Any:
    return await _get_or_404(session, DemandEndpoint, ep_id, "Demand endpoint")


@router.put("/demand-endpoints/{ep_id}", response_model=DemandEndpointOut, summary="Update demand endpoint")
async def update_demand_endpoint(
    ep_id: int,
    body: DemandEndpointUpdate,
    session: AsyncSession = Depends(get_session),
) -> Any:
    ep = await _get_or_404(session, DemandEndpoint, ep_id, "Demand endpoint")
    _apply_updates(ep, body)
    await session.flush()
    await session.refresh(ep)
    logger.info("Demand endpoint updated", ep_id=ep_id)
    return ep


@router.delete("/demand-endpoints/{ep_id}", status_code=204, summary="Soft-delete demand endpoint")
async def delete_demand_endpoint(ep_id: int, session: AsyncSession = Depends(get_session)) -> None:
    ep = await _get_or_404(session, DemandEndpoint, ep_id, "Demand endpoint")
    ep.status = ModelStatus.DELETED
    await session.flush()
    logger.info("Demand endpoint soft-deleted", ep_id=ep_id)


# ============================================================================
# DEMAND VAST TAG endpoints
# ============================================================================

@router.post("/demand-vast-tags", response_model=DemandVastTagOut, status_code=201, summary="Create demand VAST tag")
async def create_demand_vast_tag(
    body: DemandVastTagCreate,
    session: AsyncSession = Depends(get_session),
) -> Any:
    dvt = DemandVastTag(
        name=body.name,
        description=body.description,
        vast_url=body.vast_url,
        bid_floor=Decimal(str(body.bid_floor)),
        margin_pct=Decimal(str(body.margin_pct)),
        cpm_value=Decimal(str(body.cpm_value)),
        status=ModelStatus.ACTIVE,
    )
    session.add(dvt)
    await session.flush()
    await session.refresh(dvt)
    logger.info("Demand VAST tag created", dvt_id=dvt.id)
    return dvt


@router.get("/demand-vast-tags", response_model=list[DemandVastTagOut], summary="List demand VAST tags")
async def list_demand_vast_tags(
    status_filter: int | None = Query(None, alias="status"),
    session: AsyncSession = Depends(get_session),
) -> Any:
    q = select(DemandVastTag)
    if status_filter is not None:
        q = q.where(DemandVastTag.status == status_filter)
    q = q.order_by(DemandVastTag.id)
    result = await session.execute(q)
    return result.scalars().all()


@router.get("/demand-vast-tags/{dvt_id}", response_model=DemandVastTagOut, summary="Get demand VAST tag")
async def get_demand_vast_tag(dvt_id: int, session: AsyncSession = Depends(get_session)) -> Any:
    return await _get_or_404(session, DemandVastTag, dvt_id, "Demand VAST tag")


@router.put("/demand-vast-tags/{dvt_id}", response_model=DemandVastTagOut, summary="Update demand VAST tag")
async def update_demand_vast_tag(
    dvt_id: int,
    body: DemandVastTagUpdate,
    session: AsyncSession = Depends(get_session),
) -> Any:
    dvt = await _get_or_404(session, DemandVastTag, dvt_id, "Demand VAST tag")
    _apply_updates(dvt, body)
    await session.flush()
    await session.refresh(dvt)
    logger.info("Demand VAST tag updated", dvt_id=dvt_id)
    return dvt


@router.delete("/demand-vast-tags/{dvt_id}", status_code=204, summary="Soft-delete demand VAST tag")
async def delete_demand_vast_tag(dvt_id: int, session: AsyncSession = Depends(get_session)) -> None:
    dvt = await _get_or_404(session, DemandVastTag, dvt_id, "Demand VAST tag")
    dvt.status = ModelStatus.DELETED
    await session.flush()
    logger.info("Demand VAST tag soft-deleted", dvt_id=dvt_id)


# ============================================================================
# SUPPLY ↔ DEMAND MAPPING endpoints
# ============================================================================

@router.post("/mappings", response_model=MappingOut, status_code=201, summary="Link supply tag to demand source")
async def create_mapping(
    body: MappingCreate,
    session: AsyncSession = Depends(get_session),
) -> Any:
    if not body.demand_endpoint_id and not body.demand_vast_tag_id:
        raise HTTPException(400, "Must specify either demand_endpoint_id or demand_vast_tag_id")
    if body.demand_endpoint_id and body.demand_vast_tag_id:
        raise HTTPException(400, "Specify only one of demand_endpoint_id or demand_vast_tag_id")

    # Verify referenced entities exist
    await _get_or_404(session, SupplyTag, body.supply_tag_id, "Supply tag")
    if body.demand_endpoint_id:
        await _get_or_404(session, DemandEndpoint, body.demand_endpoint_id, "Demand endpoint")
    if body.demand_vast_tag_id:
        await _get_or_404(session, DemandVastTag, body.demand_vast_tag_id, "Demand VAST tag")

    m = SupplyDemandMapping(
        supply_tag_id=body.supply_tag_id,
        demand_endpoint_id=body.demand_endpoint_id,
        demand_vast_tag_id=body.demand_vast_tag_id,
        priority=body.priority,
        weight=body.weight,
        status=ModelStatus.ACTIVE,
    )
    session.add(m)
    await session.flush()
    await session.refresh(m)
    logger.info("Supply-demand mapping created", mapping_id=m.id)
    return _mapping_out(m)


@router.get("/mappings", response_model=list[MappingOut], summary="List supply-demand mappings")
async def list_mappings(
    supply_tag_id: int | None = Query(None, description="Filter by supply tag"),
    session: AsyncSession = Depends(get_session),
) -> Any:
    q = select(SupplyDemandMapping)
    if supply_tag_id is not None:
        q = q.where(SupplyDemandMapping.supply_tag_id == supply_tag_id)
    q = q.order_by(SupplyDemandMapping.supply_tag_id, SupplyDemandMapping.priority)
    result = await session.execute(q)
    return [_mapping_out(m) for m in result.scalars().all()]


@router.delete("/mappings/{mapping_id}", status_code=204, summary="Delete supply-demand mapping")
async def delete_mapping(mapping_id: int, session: AsyncSession = Depends(get_session)) -> None:
    m = await _get_or_404(session, SupplyDemandMapping, mapping_id, "Mapping")
    await session.delete(m)
    await session.flush()
    logger.info("Supply-demand mapping deleted", mapping_id=mapping_id)
