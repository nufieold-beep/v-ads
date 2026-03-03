"""
GeoIP Service — resolves IP addresses to geographic data using MaxMind GeoLite2.

Provides latitude, longitude, country, region, metro/DMA, city, and zip
so that ORTB bid requests always carry geo data even when publishers
don't send it.  Falls back gracefully when the database isn't available.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from liteads.common.logger import get_logger

logger = get_logger(__name__)

# Lazy import — geoip2 may not be installed yet.
_reader: object | None = None  # geoip2.database.Reader or None
_db_loaded = False

# Paths to try (in order) when looking for the MMDB file.
_DB_SEARCH_PATHS = [
    os.environ.get("GEOIP_DB_PATH", ""),
    "/app/data/GeoLite2-City.mmdb",
    "/app/GeoLite2-City.mmdb",
    "./data/GeoLite2-City.mmdb",
    "./GeoLite2-City.mmdb",
]

# ISO 3166-1 Alpha-2 → Alpha-3 mapping (ORTB 2.6 spec requires Alpha-3)
_ALPHA2_TO_ALPHA3: dict[str, str] = {
    "US": "USA", "GB": "GBR", "CA": "CAN", "AU": "AUS", "DE": "DEU",
    "FR": "FRA", "JP": "JPN", "BR": "BRA", "IN": "IND", "MX": "MEX",
    "IT": "ITA", "ES": "ESP", "KR": "KOR", "NL": "NLD", "SE": "SWE",
    "NO": "NOR", "DK": "DNK", "FI": "FIN", "PL": "POL", "AT": "AUT",
    "CH": "CHE", "BE": "BEL", "IE": "IRL", "NZ": "NZL", "SG": "SGP",
    "AR": "ARG", "CL": "CHL", "CO": "COL", "ZA": "ZAF", "PH": "PHL",
    "TH": "THA", "MY": "MYS", "ID": "IDN", "VN": "VNM", "TW": "TWN",
    "HK": "HKG", "IL": "ISR", "AE": "ARE", "SA": "SAU", "TR": "TUR",
    "RU": "RUS", "UA": "UKR", "CZ": "CZE", "PT": "PRT", "RO": "ROU",
    "HU": "HUN", "GR": "GRC", "CN": "CHN", "PK": "PAK", "BD": "BGD",
    "NG": "NGA", "EG": "EGY", "KE": "KEN", "PE": "PER", "VE": "VEN",
    "EC": "ECU", "PR": "PRI", "DO": "DOM", "GT": "GTM", "CR": "CRI",
}


def _to_alpha3(code: str) -> str:
    """Convert ISO Alpha-2 country code to Alpha-3 (ORTB 2.6 requirement)."""
    if not code:
        return ""
    upper = code.upper().strip()
    if len(upper) == 3:
        return upper  # Already Alpha-3
    return _ALPHA2_TO_ALPHA3.get(upper, upper)

# Default geo when lookup produces nothing (San Jose, CA — generic US)
DEFAULT_GEO = {
    "country": "USA",
    "region": "CA",
    "city": "",
    "zip": "",
    "lat": 37.3861,
    "lon": -122.0839,
    "metro": "807",
    "type": 2,        # IP-based
    "accuracy": 100,
    "ipservice": 3,   # MaxMind
}


@dataclass
class GeoResult:
    """Resolved geographic data for an IP address."""

    country: str
    region: str
    city: str
    zip: str  # noqa: A003
    lat: float
    lon: float
    metro: str
    type: int      # noqa: A003  (1=GPS, 2=IP, 3=User)
    accuracy: int
    ipservice: int


def _ensure_loaded() -> None:
    """Load the MaxMind database on first call (lazy init)."""
    global _reader, _db_loaded

    if _db_loaded:
        return
    _db_loaded = True

    try:
        import maxminddb  # lightweight C-extension reader
    except ImportError:
        logger.warning("maxminddb not installed — GeoIP enrichment disabled")
        return

    for path in _DB_SEARCH_PATHS:
        if path and os.path.isfile(path):
            try:
                _reader = maxminddb.open_database(path)
                logger.info("GeoIP database loaded", path=path)
                return
            except Exception as exc:
                logger.warning("Failed to open GeoIP DB", path=path, error=str(exc))

    logger.warning(
        "GeoLite2-City.mmdb not found — GeoIP enrichment disabled",
        searched=_DB_SEARCH_PATHS,
    )


@lru_cache(maxsize=8192)
def lookup(ip: str | None) -> GeoResult:
    """
    Look up geo data for *ip*.

    Returns a populated ``GeoResult`` (falling back to defaults when the
    database is unavailable or the IP is unknown).
    Results are cached (LRU, 8192 entries) to avoid repeated MMDB hits.
    """
    _ensure_loaded()

    if not ip or not _reader:
        return _defaults()

    try:
        data = _reader.get(ip)  # type: ignore[union-attr]
        if not data:
            return _defaults()

        loc = data.get("location") or {}
        country_rec = data.get("country") or {}
        subdivisions = data.get("subdivisions") or []
        city_rec = data.get("city") or {}
        postal_rec = data.get("postal") or {}

        return GeoResult(
            country=_to_alpha3(country_rec.get("iso_code") or "") or DEFAULT_GEO["country"],
            region=subdivisions[0].get("iso_code") if subdivisions else DEFAULT_GEO["region"],
            city=(city_rec.get("names") or {}).get("en") or DEFAULT_GEO["city"],
            zip=postal_rec.get("code") or DEFAULT_GEO["zip"],
            lat=loc.get("latitude") or DEFAULT_GEO["lat"],
            lon=loc.get("longitude") or DEFAULT_GEO["lon"],
            metro=str(loc.get("metro_code") or DEFAULT_GEO["metro"]),
            type=DEFAULT_GEO["type"],
            accuracy=loc.get("accuracy_radius") or DEFAULT_GEO["accuracy"],
            ipservice=DEFAULT_GEO["ipservice"],
        )
    except Exception as exc:
        logger.debug("GeoIP lookup failed", ip=ip, error=str(exc))
        return _defaults()


# Cached default GeoResult to avoid re-creating identical instances.
_DEFAULT_GEO_RESULT = GeoResult(
    country=DEFAULT_GEO["country"],
    region=DEFAULT_GEO["region"],
    city=DEFAULT_GEO["city"],
    zip=DEFAULT_GEO["zip"],
    lat=DEFAULT_GEO["lat"],
    lon=DEFAULT_GEO["lon"],
    metro=DEFAULT_GEO["metro"],
    type=DEFAULT_GEO["type"],
    accuracy=DEFAULT_GEO["accuracy"],
    ipservice=DEFAULT_GEO["ipservice"],
)


def _defaults() -> GeoResult:
    """Return a ``GeoResult`` filled with safe defaults."""
    return _DEFAULT_GEO_RESULT


# ---------------------------------------------------------------------------
# Schema factory helpers  (avoid 3× inline construction of Pydantic models)
# ---------------------------------------------------------------------------

def geoip_to_geo_info(ip: str | None) -> "GeoInfo":
    """Look up *ip* and return a ``GeoInfo`` schema (used by VAST-tag router)."""
    from liteads.schemas.request import GeoInfo  # avoid circular at module level

    g = lookup(ip)
    return GeoInfo(
        ip=ip or "",
        country=g.country or None,
        region=g.region or None,
        city=g.city or None,
        dma=g.metro or None,
        latitude=g.lat,
        longitude=g.lon,
        zip_code=g.zip or None,
        geo_type=g.type,
        ipservice=g.ipservice,
    )


def geoip_to_ortb_geo(ip: str | None) -> "OrtbGeo | None":
    """Look up *ip* and return an ``OrtbGeo`` schema (used by demand forwarder / enricher).

    Returns ``None`` when the lookup yields no country (MaxMind DB unavailable
    or the IP is unknown).
    """
    from liteads.schemas.openrtb import Geo as OrtbGeo  # avoid circular at module level

    g = lookup(ip)
    if not g or not g.country:
        return None
    return OrtbGeo(
        country=g.country,
        region=g.region,
        city=g.city,
        metro=g.metro,
        lat=g.lat,
        lon=g.lon,
        zip=g.zip,
        type=2,           # 2 = IP-based
        accuracy=g.accuracy,
        ipservice=3,       # 3 = MaxMind
    )
