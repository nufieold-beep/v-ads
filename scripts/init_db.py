#!/usr/bin/env python3
"""
Database initialisation script – CPM CTV & In-App Video Only.

Creates tables and seeds CTV / in-app video campaign data for LiteAds.

Usage:
    python scripts/init_db.py [--drop-existing] [--seed]
"""

import argparse
import asyncio
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from liteads.common.config import get_settings
from liteads.common.database import engine, get_session
from liteads.common.logger import get_logger
from liteads.models.base import Base

logger = get_logger(__name__)


async def create_tables(drop_existing: bool = False) -> None:
    """Create all database tables."""
    from sqlalchemy import text

    async with engine.begin() as conn:
        if drop_existing:
            logger.warning("Dropping existing tables...")
            await conn.run_sync(Base.metadata.drop_all)

        logger.info("Creating database tables...")
        await conn.run_sync(Base.metadata.create_all)

        # Additional indexes for video / CTV workloads
        logger.info("Creating additional indexes...")

        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_campaigns_status_time
            ON campaigns (status, start_time, end_time)
        """))

        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_campaigns_environment
            ON campaigns (environment)
        """))

        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_targeting_rules_campaign
            ON targeting_rules (campaign_id, is_include)
        """))

        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_hourly_stats_time
            ON hourly_stats (stat_hour DESC, campaign_id)
        """))

        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_creative_type
            ON creatives (creative_type)
        """))

        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_event_environment
            ON ad_events (environment)
        """))

    logger.info("Database tables created successfully")


async def seed_data() -> None:
    """Seed CTV and in-app video campaign data for development/testing."""
    from datetime import datetime, timedelta, timezone

    from liteads.models.ad import Advertiser, Campaign, Creative, TargetingRule

    logger.info("Seeding CTV & in-app video data...")

    async with get_session() as session:
        from sqlalchemy import select

        result = await session.execute(select(Advertiser).limit(1))
        if result.scalar():
            logger.info("Data already exists, skipping seed")
            return

        # ------------------------------------------------------------------
        # Advertisers
        # ------------------------------------------------------------------
        advertisers = [
            Advertiser(
                name="StreamCo CTV",
                balance=50000.0,
                daily_budget=5000.0,
                status=1,
            ),
            Advertiser(
                name="GameMedia InApp",
                balance=30000.0,
                daily_budget=3000.0,
                status=1,
            ),
            Advertiser(
                name="Premium Video Network",
                balance=100000.0,
                daily_budget=10000.0,
                status=1,
            ),
        ]

        for adv in advertisers:
            session.add(adv)
        await session.flush()
        logger.info(f"Created {len(advertisers)} advertisers")

        now = datetime.now(timezone.utc)
        campaigns = []

        # ------------------------------------------------------------------
        # CTV Campaigns
        # ------------------------------------------------------------------
        ctv_configs = [
            {
                "name": "CTV Pre-Roll – Premium Entertainment",
                "advertiser_id": advertisers[0].id,
                "environment": "ctv",
                "budget_daily": 500.0,
                "budget_total": 10000.0,
                "bid_amount": 12.0,     # $12 CPM
            },
            {
                "name": "CTV Mid-Roll – Live Sports",
                "advertiser_id": advertisers[0].id,
                "environment": "ctv",
                "budget_daily": 800.0,
                "budget_total": 15000.0,
                "bid_amount": 18.0,     # $18 CPM
            },
            {
                "name": "CTV Post-Roll – News",
                "advertiser_id": advertisers[2].id,
                "environment": "ctv",
                "budget_daily": 300.0,
                "budget_total": 6000.0,
                "bid_amount": 8.0,      # $8 CPM
            },
        ]

        # ------------------------------------------------------------------
        # In-App Video Campaigns
        # ------------------------------------------------------------------
        inapp_configs = [
            {
                "name": "InApp Rewarded Video – Casual Games",
                "advertiser_id": advertisers[1].id,
                "environment": "inapp",
                "budget_daily": 200.0,
                "budget_total": 4000.0,
                "bid_amount": 6.0,      # $6 CPM
            },
            {
                "name": "InApp Interstitial – Streaming",
                "advertiser_id": advertisers[1].id,
                "environment": "inapp",
                "budget_daily": 250.0,
                "budget_total": 5000.0,
                "bid_amount": 7.5,      # $7.50 CPM
            },
        ]

        all_configs = ctv_configs + inapp_configs

        for config in all_configs:
            campaign = Campaign(
                name=config["name"],
                advertiser_id=config["advertiser_id"],
                environment=config["environment"],
                budget_daily=config["budget_daily"],
                budget_total=config["budget_total"],
                bid_type=1,             # CPM only
                bid_amount=config["bid_amount"],
                start_time=now - timedelta(days=1),
                end_time=now + timedelta(days=30),
                status=1,
            )
            session.add(campaign)
            campaigns.append((campaign, config))

        await session.flush()
        logger.info(f"Created {len(campaigns)} campaigns")

        # ------------------------------------------------------------------
        # Video Creatives
        # ------------------------------------------------------------------
        creative_count = 0
        for campaign, config in campaigns:
            is_ctv = config["environment"] == "ctv"
            creative_type = 1 if is_ctv else 2  # 1=CTV_VIDEO, 2=INAPP_VIDEO

            creatives_data = [
                {
                    "title": f"{config['name']} – 30s",
                    "description": f"30-second video for {config['name']}",
                    "video_url": f"https://cdn.example.com/video/{campaign.id}_30s.mp4",
                    "companion_image_url": f"https://cdn.example.com/img/{campaign.id}_comp_300x250.png",
                    "landing_url": f"https://example.com/landing/{campaign.id}",
                    "creative_type": creative_type,
                    "duration": 30,
                    "bitrate": 5000 if is_ctv else 2500,
                    "mime_type": "video/mp4",
                    "width": 1920 if is_ctv else 1280,
                    "height": 1080 if is_ctv else 720,
                    "skippable": True,
                    "skip_after": 5,
                    "placement": "pre_roll",
                },
                {
                    "title": f"{config['name']} – 15s",
                    "description": f"15-second video for {config['name']}",
                    "video_url": f"https://cdn.example.com/video/{campaign.id}_15s.mp4",
                    "companion_image_url": None,
                    "landing_url": f"https://example.com/landing/{campaign.id}",
                    "creative_type": creative_type,
                    "duration": 15,
                    "bitrate": 4000 if is_ctv else 2000,
                    "mime_type": "video/mp4",
                    "width": 1920 if is_ctv else 1280,
                    "height": 1080 if is_ctv else 720,
                    "skippable": False,
                    "skip_after": 0,
                    "placement": "pre_roll" if "Pre-Roll" in config["name"] else "mid_roll",
                },
            ]

            for data in creatives_data:
                creative = Creative(
                    campaign_id=campaign.id,
                    title=data["title"],
                    description=data["description"],
                    video_url=data["video_url"],
                    companion_image_url=data["companion_image_url"],
                    landing_url=data["landing_url"],
                    creative_type=data["creative_type"],
                    duration=data["duration"],
                    bitrate=data["bitrate"],
                    mime_type=data["mime_type"],
                    width=data["width"],
                    height=data["height"],
                    skippable=data["skippable"],
                    skip_after=data["skip_after"],
                    placement=data["placement"],
                    status=1,
                )
                session.add(creative)
                creative_count += 1

        await session.flush()
        logger.info(f"Created {creative_count} video creatives")

        # ------------------------------------------------------------------
        # Targeting Rules
        # ------------------------------------------------------------------
        targeting_count = 0
        for campaign, config in campaigns:
            # Environment rule (always)
            session.add(TargetingRule(
                campaign_id=campaign.id,
                rule_type="environment",
                rule_value={"values": [config["environment"]]},
                is_include=True,
            ))
            targeting_count += 1

            if config["environment"] == "ctv":
                # CTV device targeting
                session.add(TargetingRule(
                    campaign_id=campaign.id,
                    rule_type="device",
                    rule_value={"os": ["roku", "firetv", "tvos", "tizen", "androidtv", "webos"]},
                    is_include=True,
                ))
                targeting_count += 1

                # Geo targeting
                session.add(TargetingRule(
                    campaign_id=campaign.id,
                    rule_type="geo",
                    rule_value={"countries": ["US"]},
                    is_include=True,
                ))
                targeting_count += 1
            else:
                # InApp device targeting
                session.add(TargetingRule(
                    campaign_id=campaign.id,
                    rule_type="device",
                    rule_value={"os": ["android", "ios"]},
                    is_include=True,
                ))
                targeting_count += 1

        await session.commit()
        logger.info(f"Created {targeting_count} targeting rules")

    logger.info("Database seeding completed – CTV & in-app video data ready")


async def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Initialize LiteAds database (CPM CTV & InApp Video)")
    parser.add_argument(
        "--drop-existing",
        action="store_true",
        help="Drop existing tables before creating",
    )
    parser.add_argument(
        "--seed",
        action="store_true",
        help="Seed CTV / in-app video demo data",
    )

    args = parser.parse_args()

    settings = get_settings()
    logger.info(f"Initializing database: {settings.database.host}:{settings.database.port}")

    await create_tables(drop_existing=args.drop_existing)

    if args.seed:
        await seed_data()

    logger.info("Database initialization complete!")


if __name__ == "__main__":
    asyncio.run(main())
