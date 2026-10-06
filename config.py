"""
config.py — Runtime configuration for the Last247 service.

All values come from environment variables. A .env file in the working
directory is loaded automatically (via python-dotenv) if it exists. Explicit
environment variables always win over .env values.

Required:
    TURSO_DATABASE_URL  — libSQL connection string or local file path.

Optional (all have sensible defaults):
    TURSO_AUTH_TOKEN    PORT  INGEST_INTERVAL  INGEST_TIMEOUT  RETENTION_DAYS
    NEWS_API_KEY  GNEWS_API_KEY  NEWS_DATA_API_KEY  WEBFETCH_API_URL
    WEBFETCH_API_KEY  NEWS_LANGUAGE  MAX_ARTICLES  CORS_ALLOW_ORIGINS

Usage:
    from config import load_config
    cfg = load_config()   # raises ValueError if TURSO_DATABASE_URL is missing
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv  # python-dotenv


@dataclass
class Config:
    """
    All runtime settings in one flat dataclass.

    Database
    --------
    turso_url   : libSQL URL — "libsql://name.turso.io" for remote Turso,
                  or "file:./news.db" for a local SQLite file.
    turso_token : Bearer token for remote Turso databases. Empty for local
                  file: URLs (no auth required).

    Providers (sequential fallback order)
    ------
    newsapi_key      : NewsAPI  (#1)
    gnews_api_key    : GNews    (#2)
    newsdata_api_key : NewsData.io (#3)
    webfetch_api_url : WebFetch base URL (#4) — replaces an API key for this provider.
    webfetch_api_key : Optional key for the WebFetch endpoint.

    Fetch options
    -------------
    language     : BCP 47 language tag used by provider queries (default "en").
    max_articles : Max articles requested per provider per run (default 50).

    HTTP server
    -----------
    port             : TCP port the server listens on (default "8080").
                       Render provides PORT dynamically.
    ingest_interval  : Seconds between background ingestion runs (default 21600 = 6h).
    ingest_timeout   : Max seconds for one ingestion run (default 120).
    cors_origins     : List of allowed CORS origins (default ["*"] = allow all).
    retention_days   : Rolling article window in days; older rows are deleted (default 7).
    """

    # Database
    turso_url: str
    turso_token: str = ""
    retention_days: int = 7

    # Provider API keys
    newsapi_key: str = ""
    gnews_api_key: str = ""
    newsdata_api_key: str = ""
    webfetch_api_key: str = ""
    webfetch_api_url: str = ""

    # Fetch options
    language: str = "en"
    max_articles: int = 50

    # HTTP server
    port: str = "8080"
    ingest_interval: int = 21_600   # seconds (6 h)
    ingest_timeout: int = 120       # seconds
    cors_origins: list[str] = field(default_factory=lambda: ["*"])


def load_config() -> Config:
    """
    Load configuration from environment variables (and optionally a .env file).

    Raises ValueError if TURSO_DATABASE_URL is not set — the service cannot
    run without a database connection string.

    All other missing variables fall back to the defaults documented in Config.
    Invalid numeric values (e.g. non-integer RETENTION_DAYS) are silently
    ignored and the default stays in effect.
    """
    # Load .env if present; explicit env vars always win (override=False).
    load_dotenv(".env", override=False)

    turso_url = os.getenv("TURSO_DATABASE_URL", "").strip()
    if not turso_url:
        raise ValueError("TURSO_DATABASE_URL is required but not set")

    cfg = Config(turso_url=turso_url)

    # Database auth token (required only for remote libsql:// URLs).
    cfg.turso_token = os.getenv("TURSO_AUTH_TOKEN", "").strip()

    # Provider API keys.
    cfg.newsapi_key      = os.getenv("NEWS_API_KEY", "").strip()
    cfg.gnews_api_key    = os.getenv("GNEWS_API_KEY", "").strip()
    cfg.newsdata_api_key = os.getenv("NEWS_DATA_API_KEY", "").strip()
    cfg.webfetch_api_key = os.getenv("WEBFETCH_API_KEY", "").strip()
    cfg.webfetch_api_url = os.getenv("WEBFETCH_API_URL", "").strip()

    # Fetch options.
    cfg.language     = os.getenv("NEWS_LANGUAGE", "en").strip() or "en"
    cfg.max_articles = _parse_positive_int(os.getenv("MAX_ARTICLES", ""), 50)

    # Retention window.
    cfg.retention_days = _parse_positive_int(os.getenv("RETENTION_DAYS", ""), 7)

    # HTTP server.
    cfg.port = os.getenv("PORT", "8080").strip() or "8080"

    # Ingest interval: accept "6h", "30m", "3600s", or plain seconds integer.
    cfg.ingest_interval = _parse_duration_seconds(
        os.getenv("INGEST_INTERVAL", ""), default=21_600
    )
    cfg.ingest_timeout = _parse_duration_seconds(
        os.getenv("INGEST_TIMEOUT", ""), default=120
    )

    # CORS origins: comma-separated list, e.g. "https://app.last247.dev,https://..."
    raw_cors = os.getenv("CORS_ALLOW_ORIGINS", "").strip()
    if raw_cors:
        cfg.cors_origins = [o.strip() for o in raw_cors.split(",") if o.strip()]
    # else keep the default ["*"]

    return cfg


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _parse_positive_int(value: str, default: int) -> int:
    """Parse value as a positive integer; return default on failure."""
    try:
        n = int(value)
        return n if n > 0 else default
    except (ValueError, TypeError):
        return default


def _parse_duration_seconds(value: str, default: int) -> int:
    """
    Parse a duration string into whole seconds.

    Accepted formats:
        "6h"    → 21600
        "30m"   → 1800
        "120s"  → 120
        "3600"  → 3600   (bare integer treated as seconds)

    Returns default on any parse failure.
    """
    value = (value or "").strip()
    if not value:
        return default
    try:
        if value.endswith("h"):
            return int(value[:-1]) * 3600
        if value.endswith("m"):
            return int(value[:-1]) * 60
        if value.endswith("s"):
            return int(value[:-1])
        return int(value)
    except (ValueError, TypeError):
        return default
