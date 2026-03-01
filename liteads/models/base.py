"""
Base model and common utilities for SQLAlchemy ORM.

Refactored for CPM-only CTV and In-App video ad serving.
"""

from datetime import datetime
from enum import IntEnum

from sqlalchemy import DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """SQLAlchemy declarative base class."""
    pass


class TimestampMixin:
    """Mixin for created_at and updated_at timestamps."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Status(IntEnum):
    """Common status enum."""
    INACTIVE = 0
    ACTIVE = 1
    PAUSED = 2
    DELETED = 3
    PENDING = 4


class BidType(IntEnum):
    """Bid type enum — CPM only for video ad serving."""
    CPM = 1  # Cost per mille (1000 impressions) — the only supported bid type


class CreativeType(IntEnum):
    """Creative type enum — video only for CTV and In-App."""
    CTV_VIDEO = 1      # Connected TV video ad
    INAPP_VIDEO = 2    # In-App (mobile/tablet) video ad


class Environment(IntEnum):
    """Ad serving environment."""
    CTV = 1       # Connected TV (Roku, Fire TV, Apple TV, Smart TVs)
    INAPP = 2     # In-App (mobile/tablet applications)


class VideoPlacement(IntEnum):
    """Video ad placement type."""
    PRE_ROLL = 1    # Before content
    MID_ROLL = 2    # During content
    POST_ROLL = 3   # After content


class EventType(IntEnum):
    """Ad event types — video-centric tracking."""
    IMPRESSION = 1
    START = 2
    FIRST_QUARTILE = 3   # 25% viewed
    MIDPOINT = 4          # 50% viewed
    THIRD_QUARTILE = 5    # 75% viewed
    COMPLETE = 6          # 100% viewed
    CLICK = 7
    SKIP = 8
    MUTE = 9
    UNMUTE = 10
    PAUSE = 11
    RESUME = 12
    FULLSCREEN = 13
    ERROR = 14
    CLOSE = 15
    ACCEPT_INVITATION = 16
    EXIT_FULLSCREEN = 17
    EXPAND = 18
    COLLAPSE = 19
    REWIND = 20
    PROGRESS = 21
    LOADED = 22
    CREATIVE_VIEW = 23
    LOSS = 24            # Auction loss (lurl callback)
    WIN = 25             # Auction win  (nurl callback)
