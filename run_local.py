"""
Local development runner – no PostgreSQL / Redis required.

Uses SQLite (aiosqlite) + fakeredis so you can test the server
immediately on a bare Windows machine.

Usage:
    python run_local.py
"""

import asyncio
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# 1. Monkey-patch DatabaseManager.init() → SQLite in-memory
# ---------------------------------------------------------------------------
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from liteads.common import database as _db_mod

_SQLITE_URL = "sqlite+aiosqlite:///liteads_local.db"

_orig_db_init = _db_mod.DatabaseManager.init


async def _patched_db_init(self):
    """Use local SQLite instead of PostgreSQL."""
    self._engine = create_async_engine(
        _SQLITE_URL,
        echo=False,
        connect_args={"check_same_thread": False},
    )
    self._session_factory = async_sessionmaker(
        bind=self._engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autocommit=False,
        autoflush=False,
    )
    print(f"[run_local] Database: {_SQLITE_URL}")


_db_mod.DatabaseManager.init = _patched_db_init

# ---------------------------------------------------------------------------
# 2. Monkey-patch RedisClient.connect() → fakeredis
# ---------------------------------------------------------------------------
import fakeredis.aioredis as _fakeredis

from liteads.common import cache as _cache_mod

_orig_redis_connect = _cache_mod.RedisClient.connect


async def _patched_redis_connect(self):
    """Use in-memory fakeredis instead of a real Redis server."""
    self._client = _fakeredis.FakeRedis(decode_responses=True)
    print("[run_local] Redis: fakeredis (in-memory)")


_cache_mod.RedisClient.connect = _patched_redis_connect

# ---------------------------------------------------------------------------
# 3. Boot the FastAPI server
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    print("=" * 60)
    print("  LiteAds – Local Dev Server (SQLite + fakeredis)")
    print("=" * 60)
    print()

    uvicorn.run(
        "liteads.ad_server.main:app",
        host="127.0.0.1",
        port=8000,
        reload=False,
        log_level="info",
    )
