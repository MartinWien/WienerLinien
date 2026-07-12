"""Application settings, sourced from environment variables (prefix ``WL_``).

The fair-use clamp lives here rather than in the poller so that *any* consumer
of the settings object sees the corrected value: Wiener Linien requires a
minimum of 15 seconds between requests to the same feed.
"""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DATA_SOURCE_ATTRIBUTION = "Datenquelle: Stadt Wien - data.wien.gv.at (Wiener Linien), CC BY 4.0"
MIN_POLL_INTERVAL_SECONDS = 15

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="WL_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Database
    database_url: str = "sqlite+aiosqlite:///./data/wl.db"

    # Cache
    cache_backend: Literal["auto", "redis", "memory"] = "auto"
    redis_url: str = "redis://localhost:6379/0"
    cache_ttl_seconds: int = Field(default=180, ge=30)

    # Poller / fair use
    poll_interval_seconds: int = 30
    watched_stations: str = "Karlsplatz,Stephansplatz,Praterstern,Schwedenplatz"
    poller_enabled: bool = True
    monitor_max_rbls_per_request: int = Field(default=25, ge=1, le=50)
    monitor_concurrency: int = Field(default=3, ge=1, le=5)
    # On-demand watching: stations requested via the API join the poll set
    # until nobody has asked for them for this long, capped in number so the
    # upstream request volume stays bounded.
    dynamic_watch_max: int = Field(default=25, ge=0, le=100)
    dynamic_watch_ttl_minutes: int = Field(default=60, ge=1)

    # Upstream HTTP
    monitor_base_url: str = "https://www.wienerlinien.at/ogd_realtime"
    http_timeout_seconds: float = Field(default=10.0, gt=0)
    http_max_retries: int = Field(default=2, ge=0, le=5)
    http_backoff_base_seconds: float = Field(default=0.5, gt=0)

    # Circuit breaker
    cb_failure_threshold: int = Field(default=5, ge=1)
    cb_cooldown_seconds: float = Field(default=120.0, gt=0)

    # History / stats
    snapshot_retention_minutes: int = Field(default=180, ge=10)
    snapshot_max_departures_per_line: int = Field(default=4, ge=1)
    stats_on_time_threshold_seconds: int = Field(default=120, ge=0)

    # Registry
    data_dir: str = "./data"
    registry_auto_download: bool = True

    # API
    rate_limit_per_minute: int = Field(default=120, ge=1)

    # Logging
    log_level: str = "INFO"
    log_json: bool = True

    @field_validator("poll_interval_seconds")
    @classmethod
    def _enforce_fair_use(cls, value: int) -> int:
        if value < MIN_POLL_INTERVAL_SECONDS:
            logger.warning(
                "WL_POLL_INTERVAL_SECONDS=%s violates the Wiener Linien fair-use minimum; "
                "clamping to %s seconds",
                value,
                MIN_POLL_INTERVAL_SECONDS,
            )
            return MIN_POLL_INTERVAL_SECONDS
        return value

    @property
    def watched_station_names(self) -> list[str]:
        return [name.strip() for name in self.watched_stations.split(",") if name.strip()]

    @property
    def staleness_threshold_seconds(self) -> float:
        """Data older than twice the poll interval is flagged ``stale``."""
        return self.poll_interval_seconds * 2.0
