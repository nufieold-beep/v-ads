"""
Configuration management for LiteAds – CPM CTV & In-App Video Only.

Supports loading from environment variables and YAML files.
All settings are tailored for video ad serving with VAST / OpenRTB 2.6 support.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# ---------------------------------------------------------------------------
# Infrastructure
# ---------------------------------------------------------------------------

class DatabaseSettings(BaseSettings):
    """PostgreSQL configuration."""

    host: str = "localhost"
    port: int = 5432
    name: str = "liteads"
    user: str = "liteads"
    password: str = ""
    pool_size: int = 10
    max_overflow: int = 20

    @property
    def async_url(self) -> str:
        return f"postgresql+asyncpg://{self.user}:{self.password}@{self.host}:{self.port}/{self.name}"

    @property
    def sync_url(self) -> str:
        return f"postgresql://{self.user}:{self.password}@{self.host}:{self.port}/{self.name}"


class RedisSettings(BaseSettings):
    """Redis configuration."""

    host: str = "localhost"
    port: int = 6379
    db: int = 0
    password: str = ""
    pool_size: int = 10

    @property
    def url(self) -> str:
        if self.password:
            return f"redis://:{self.password}@{self.host}:{self.port}/{self.db}"
        return f"redis://{self.host}:{self.port}/{self.db}"


class ServerSettings(BaseSettings):
    """Uvicorn / HTTP server configuration."""

    host: str = "0.0.0.0"
    port: int = 8000
    workers: int = 4
    reload: bool = False


# ---------------------------------------------------------------------------
# Video / VAST Settings
# ---------------------------------------------------------------------------

class VideoSettings(BaseSettings):
    """Video creative defaults & validation constraints."""

    # Supported MIME types for video creatives
    supported_mimes: list[str] = [
        "video/mp4",
        "video/webm",
        "video/ogg",
        "video/3gpp",
        "application/x-mpegURL",       # HLS
        "application/dash+xml",         # DASH
    ]

    # Duration constraints (seconds)
    min_duration: int = 5
    max_duration: int = 120
    default_duration: int = 30

    # Bitrate constraints (kbps)
    min_bitrate: int = 500
    max_bitrate: int = 25000

    # Supported VAST protocols (IAB: 2 = VAST 2.0, 3 = VAST 3.0, 6 = VAST 4.0, 7 = VAST 4.1, 8 = VAST 4.2)
    supported_vast_protocols: list[int] = [2, 3, 6, 7, 8]

    # Default VAST version to generate
    default_vast_version: str = "4.0"


class VASTSettings(BaseSettings):
    """VAST XML generation and serving configuration."""

    # Supported VAST versions for XML generation
    supported_versions: list[str] = ["2.0", "3.0", "4.0", "4.1", "4.2"]

    # Default VAST version to generate
    default_vast_version: str = "4.0"

    # Base URL for VAST tracking events (auto-detected if empty)
    tracking_base_url: str = ""

    # Whether to include companion ads in VAST XML
    include_companion: bool = True

    # Default skip offset (seconds, 0 = non-skippable)
    default_skip_offset: int = 5

    # VAST error tracking enabled
    error_tracking: bool = True


# ---------------------------------------------------------------------------
# OpenRTB Settings
# ---------------------------------------------------------------------------

class OpenRTBSettings(BaseSettings):
    """OpenRTB 2.6 bid request / response configuration."""

    # Seat identifier used in seatbid — identifies the bidder/DSP entity
    seat_id: str = "liteads"

    # Supported environments
    supported_environments: list[str] = ["ctv", "inapp"]

    # Auction type: 1 = first-price, 2 = second-price
    default_auction_type: int = 2

    # Maximum processing time (ms) for bid responses
    default_tmax: int = 200

    # Default currency
    default_currency: str = "USD"

    # nurl / burl macro support
    auction_price_macro: str = "${AUCTION_PRICE}"

    # lurl loss reason macro
    auction_loss_macro: str = "${AUCTION_LOSS}"

    # Minimum bid floor (CPM)
    default_bid_floor: float = 0.50

    # Maximum bid floor (CPM)
    max_bid_floor: float = 100.0


# ---------------------------------------------------------------------------
# Ad Serving
# ---------------------------------------------------------------------------

class AdServingSettings(BaseSettings):
    """Core ad-serving configuration — CPM video only."""

    # Request defaults
    default_num_ads: int = 1
    max_num_ads: int = 10
    timeout_ms: int = 100

    # ML / fill-rate prediction
    enable_ml_prediction: bool = True
    model_path: str = ""

    # Supported environments (CTV + In-App video)
    supported_environments: list[str] = ["ctv", "inapp"]

    # CPM billing
    default_bid_floor_cpm: float = 1.0
    min_cpm: float = 0.50

    # Fill-rate optimization
    fallback_fill_rate: float = 0.85
    fallback_vtr: float = 0.70
    fallback_ctr: float = 0.005

    # CTV device families recognised by targeting
    ctv_device_families: list[str] = [
        "roku", "firetv", "tvos", "tizen",
        "androidtv", "webos", "vizio",
        "chromecast", "playstation", "xbox",
    ]


class FrequencySettings(BaseSettings):
    """Frequency-cap configuration."""

    default_daily_cap: int = 3
    default_hourly_cap: int = 1
    ttl_hours: int = 24


# ---------------------------------------------------------------------------
# ML
# ---------------------------------------------------------------------------

class MLSettings(BaseSettings):
    """ML / fill-rate model configuration."""

    model_dir: str = "./models"
    fill_rate_model: str = "fillrate_v1"
    vtr_model: str = "vtr_v1"
    embedding_dim: int = 8
    batch_size: int = 128


# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------

class LoggingSettings(BaseSettings):
    """Logging configuration."""

    level: str = "INFO"
    format: Literal["json", "console"] = "json"


class MonitoringSettings(BaseSettings):
    """Prometheus / monitoring configuration."""

    enabled: bool = True
    prometheus_port: int = 9090


# ---------------------------------------------------------------------------
# Root Settings
# ---------------------------------------------------------------------------

class Settings(BaseSettings):
    """Main application settings — CPM CTV & In-App Video."""

    model_config = SettingsConfigDict(
        env_prefix="LITEADS_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    # Application
    app_name: str = "LiteAds"
    app_version: str = "0.2.0"
    debug: bool = False
    env: Literal["dev", "prod", "test"] = "dev"

    # Nested settings
    server: ServerSettings = Field(default_factory=ServerSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    ad_serving: AdServingSettings = Field(default_factory=AdServingSettings)
    frequency: FrequencySettings = Field(default_factory=FrequencySettings)
    ml: MLSettings = Field(default_factory=MLSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    monitoring: MonitoringSettings = Field(default_factory=MonitoringSettings)
    video: VideoSettings = Field(default_factory=VideoSettings)
    vast: VASTSettings = Field(default_factory=VASTSettings)
    openrtb: OpenRTBSettings = Field(default_factory=OpenRTBSettings)

    @field_validator("env")
    @classmethod
    def validate_env(cls, v: str) -> str:
        if v not in ("dev", "prod", "test"):
            raise ValueError(f"Invalid environment: {v}")
        return v


# ---------------------------------------------------------------------------
# YAML Loader
# ---------------------------------------------------------------------------

def load_yaml_config(config_path: Path) -> dict:
    """Load configuration from YAML file."""
    if not config_path.exists():
        return {}
    with open(config_path) as f:
        return yaml.safe_load(f) or {}


def merge_configs(base: dict, override: dict) -> dict:
    """Deep-merge two configuration dictionaries."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = merge_configs(result[key], value)
        else:
            result[key] = value
    return result


_SECTION_CLASSES: dict[str, type[BaseSettings]] = {
    "server": ServerSettings,
    "database": DatabaseSettings,
    "redis": RedisSettings,
    "ad_serving": AdServingSettings,
    "frequency": FrequencySettings,
    "ml": MLSettings,
    "logging": LoggingSettings,
    "monitoring": MonitoringSettings,
    "video": VideoSettings,
    "vast": VASTSettings,
    "openrtb": OpenRTBSettings,
}


@lru_cache
def get_settings() -> Settings:
    """
    Get application settings.

    Loads from:
    1. configs/base.yaml (base configuration)
    2. configs/{env}.yaml (environment-specific overrides)
    3. Environment variables (highest priority)
    """
    import os

    env = os.getenv("LITEADS_ENV", "dev")

    # Find config directory
    config_dir = Path(__file__).parent.parent.parent / "configs"

    # Load base config
    base_config = load_yaml_config(config_dir / "base.yaml")

    # Load environment-specific config
    env_config = load_yaml_config(config_dir / f"{env}.yaml")

    # Merge configurations
    merged = merge_configs(base_config, env_config)

    # Flatten nested config for Pydantic
    flat_config: dict = {}
    if "app" in merged:
        flat_config["app_name"] = merged["app"].get("name", "LiteAds")
        flat_config["app_version"] = merged["app"].get("version", "0.2.0")
        flat_config["debug"] = merged["app"].get("debug", False)

    flat_config["env"] = env

    # Create nested settings dynamically, allowing env vars to override YAML.
    # pydantic-settings gives init kwargs higher priority than env vars,
    # so we manually merge env-var overrides into the YAML dict first.
    for section_key, settings_cls in _SECTION_CLASSES.items():
        section_data = dict(merged.get(section_key, {}))

        # Check for env-var overrides: LITEADS_SECTION__FIELD → field
        prefix = f"LITEADS_{section_key.upper()}__"
        for env_key, env_value in os.environ.items():
            if env_key.startswith(prefix):
                field_name = env_key[len(prefix):].lower()
                section_data[field_name] = env_value

        flat_config[section_key] = settings_cls(**section_data)

    return Settings(**flat_config)


# Convenience alias
settings = get_settings()
