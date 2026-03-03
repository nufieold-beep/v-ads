"""
Shared device / environment detection helpers.

Consolidates CTV detection, IFA type inference, and placement mapping
previously duplicated across vast_tag.py, openrtb_service.py, and
openrtb.py schema.  A single source of truth prevents divergence.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# CTV OS keywords (shared by env detection + schema)
# ---------------------------------------------------------------------------

CTV_OS_KEYWORDS: tuple[str, ...] = (
    "roku", "firetv", "fireos", "tvos", "tizen",
    "webos", "webostv", "vizio", "androidtv",
    "chromecast", "playstation", "xbox", "googletv",
)

CTV_UA_MARKERS: tuple[str, ...] = (
    "smarttv", "smart tv", "smart-tv", "ctv", "roku", "tizen",
    "webos", "web0s", "firetv", "aftb", "aftm",
    "appletv", "apple tv", "googletv", "google tv",
    "bravia", "philipstv", "hisense", "vizio",
    "crkey",        # Chromecast
    "mitv",         # Xiaomi TV
    "hbbtv",        # HbbTV standard
)

_OS_ENV_MAP: dict[str, str] = {
    "roku": "ctv",   "firetv": "ctv",    "fireos": "ctv",
    "tvos": "ctv",   "tizen": "ctv",     "webos": "ctv",
    "webostv": "ctv", "vizio": "ctv",    "androidtv": "ctv",
    "chromecast": "ctv", "playstation": "ctv", "xbox": "ctv",
    "android": "inapp", "ios": "inapp",
}


# ---------------------------------------------------------------------------
# Environment detection
# ---------------------------------------------------------------------------

def detect_environment(
    os_str: str,
    ua: str = "",
    device_type: int | None = None,
) -> str:
    """Infer ``ctv`` or ``inapp`` from OS, UA string, and device type.

    Priority:
    1. Explicit device type 3 (CTV) or 7 (STB)
    2. Known CTV OS keywords
    3. UA heuristics (SmartTV, Fire TV models, etc.)
    4. ``AFT*`` model prefix in UA (Amazon Fire TV sticks)
    5. Fallback to ``_OS_ENV_MAP`` → ``"inapp"``
    """
    key = os_str.lower().replace(" ", "")

    # 0. Publisher explicitly said CTV
    if device_type in (3, 7):
        return "ctv"

    # 1. Explicit CTV OS identifiers (before generic 'android')
    for kw in CTV_OS_KEYWORDS:
        if kw in key:
            return "ctv"

    # 2. UA heuristics
    ua_lower = (ua or "").lower()
    if any(marker in ua_lower for marker in CTV_UA_MARKERS):
        return "ctv"

    # 2b. Amazon Fire TV sticks (AFTR, AFTB, AFTGAZL, …)
    if re.search(r"\bAFT[A-Z0-9]", ua or ""):
        return "ctv"

    # 3. OS map (android → inapp, etc.)
    if key in _OS_ENV_MAP:
        return _OS_ENV_MAP[key]

    return "inapp"


# ---------------------------------------------------------------------------
# IFA type inference
# ---------------------------------------------------------------------------

def infer_ifa_type(os_str: str, make: str = "") -> str:
    """Map OS / device make to advertising-ID type.

    Returns one of: ``rida``, ``afai``, ``idfa``, ``tifa``, ``lgudid``,
    ``gaid``, ``vida``, or ``unknown``.
    """
    key = os_str.lower().replace(" ", "")
    make_lower = make.lower()

    if "roku" in key:
        return "rida"
    if "fire" in key or "amazon" in make_lower:
        return "afai"
    if "tvos" in key or "apple" in key:
        return "idfa"
    if "tizen" in key or "samsung" in make_lower:
        return "tifa"
    if "webos" in key or "lg" in make_lower:
        return "lgudid"
    if "vizio" in key:
        return "vida"
    if "googletv" in key or "google tv" in key:
        return "gaid"
    if "android" in key:
        return "gaid"
    if "chromecast" in key:
        return "gaid"
    if "ios" in key:
        return "idfa"
    return "unknown"


# ---------------------------------------------------------------------------
# Placement mapping
# ---------------------------------------------------------------------------

def map_placement(
    start_delay: int | None = None,
    placement: int | None = None,
) -> str:
    """Convert OpenRTB ``startdelay`` / ``placement`` to an internal string.

    * ``0`` → ``pre_roll``
    * ``>0`` or ``-1`` → ``mid_roll``
    * ``-2`` → ``post_roll``
    * fallback on ``placement == 1`` → ``pre_roll``
    """
    if start_delay is not None:
        if start_delay == 0:
            return "pre_roll"
        if start_delay > 0 or start_delay == -1:
            return "mid_roll"
        if start_delay == -2:
            return "post_roll"
    if placement and placement == 1:
        return "pre_roll"
    return "pre_roll"


# ---------------------------------------------------------------------------
# OS inference from UA
# ---------------------------------------------------------------------------

# Pre-compiled regex for OS inference from UA.  Ordering matters: first
# match wins.  Common platforms (Android/iOS) are checked last because
# CTV-specific markers like "tizen" or "web0s" could also carry
# "Mozilla/5.0" and a generic "Android" token.
_OS_FROM_UA_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"roku", re.I),                   "Roku"),
    (re.compile(r"web0s|webos", re.I),             "webOS TV"),
    (re.compile(r"tizen", re.I),                   "Tizen"),
    (re.compile(r"firetv|\baftb\b", re.I),         "Fire OS"),
    (re.compile(r"appletv|apple\s*tv", re.I),      "tvOS"),
    (re.compile(r"crkey", re.I),                   "Chromecast"),
    (re.compile(r"android", re.I),                 "Android"),
    (re.compile(r"ios|iphone|ipad", re.I),         "iOS"),
)


def infer_os_from_ua(ua: str) -> str:
    """Best-effort OS guess from a User-Agent string."""
    for pattern, os_name in _OS_FROM_UA_PATTERNS:
        if pattern.search(ua):
            return os_name
    return "unknown"


# ---------------------------------------------------------------------------
# Device-type mapping
# ---------------------------------------------------------------------------

# Module-level constant — avoid re-creating dict on every call
_DEVICE_TYPE_MAP: dict[int, str] = {
    1: "mobile",
    2: "pc",
    3: "ctv",
    4: "phone",
    5: "tablet",
    6: "connected_device",
    7: "set_top_box",
}

_CONNECTION_TYPE_MAP: dict[int, str] = {
    1: "ethernet",
    2: "wifi",
    3: "cellular_unknown",
    4: "2g",
    5: "3g",
    6: "4g",
    7: "5g",
}


def map_device_type(device_type: int | None) -> str:
    """Map IAB device-type int to a human-readable string."""
    return _DEVICE_TYPE_MAP.get(device_type or 0, "unknown")


def map_connection_type(conn_type: int | None) -> str:
    """Map IAB connection-type int to a string."""
    if conn_type is None:
        return "unknown"
    return _CONNECTION_TYPE_MAP.get(conn_type, "unknown")
