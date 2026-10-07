"""
config.py — Runtime configuration for the Last247 service.

All values come from environment variables. A .env file in the working
directory is loaded automatically (via python-dotenv) if it exists. Explicit
environment variables always win over .env values.

Required:
    TURSO_DATABASE_URL  — libSQL connection string or local file path.

Optional (all have sensible defaults):
    TURSO_AUTH_TOKEN  PORT  INGEST_INTERVAL  INGEST_TIMEOUT  RETENTION_DAYS
    NEWS_API_KEY  GNEWS_API_KEY  NEWS_DATA_API_KEY  WEBFETCH_API_URL
    WEBFETCH_API_KEY  NEWS_LANGUAGE  MAX_ARTICLES  CORS_ALLOW_ORIGINS
    LLMPING_BASE_URL  LLMPING_CHAT_PATH  LLMPING_TIMEOUT  LLMPING_API_TOKEN
    LLM_SYSTEM_PROMPT  LLM_MAX_CONTENT_CHARS
    RUN_ONCE  NULL_CHECK_INTERVAL  CATEGORY_BACKFILL_ONCE

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
    max_articles : Max articles requested per provider per run (default 10).

    HTTP server
    -----------
    port             : TCP port the server listens on (default "8080").
                       Render provides PORT dynamically.
    ingest_interval  : Seconds between background ingestion runs (default 7200 = 2h).
    ingest_timeout   : Max seconds for one ingestion run, including the per-article
                       LLM parse phase (default 900).
    cors_origins     : List of allowed CORS origins (default ["*"] = allow all).
    retention_days   : Rolling article window in days; older rows are deleted (default 7).

    Scheduling / maintenance
    ------------------------
    run_once                : When true (only honored by `python main.py`), run one
                              backfill/ingestion/repair pass and exit — no scheduler.
                              For local testing; continuous deploys keep this false.
    null_check_interval     : Seconds between NULL/empty-field repair checks
                              (default 7200 = 2h). The repair only touches
                              incomplete rows; healthy rows are never reprocessed.
    category_backfill_once  : When true, run the one-time category backfill over
                              EXISTING rows at startup — but only if the
                              "category_backfill_done" marker is absent from the DB
                              meta table. After a successful run the marker is
                              written, so every later start is a no-op.

    LLMPing (LLM Brain)
    -------------------
    llmping_base_url : Base URL of the LLMPing service (default https://llmping.onrender.com).
    llmping_chat_path: Path of the chat endpoint appended to the base URL (default "/chat").
    llmping_api_url  : Optional FULL endpoint URL (e.g. https://llmping.onrender.com/chat).
                       When set it overrides base_url + chat_path.
    llmping_timeout  : Max seconds per LLMPing /chat call (default 60).
    llmping_api_token: Bearer token for LLMPing. Empty by default — no auth header
                       is sent unless this is set.
    system_prompt    : System instructions sent with every article. Empty by default;
                       the placeholder in llmping.DEFAULT_SYSTEM_PROMPT is used instead.
    llm_max_content_chars : Max characters of article description/content included
                       in a prompt (default 4000).
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
    max_articles: int = 10

    # HTTP server
    port: str = "8080"
    ingest_interval: int = 7_200   # seconds (2 h — 12 runs/day)
    ingest_timeout: int = 900      # seconds, covers fetch + sequential LLM calls
    cors_origins: list[str] = field(default_factory=lambda: ["*"])

    # Scheduling / maintenance
    run_once: bool = False
    null_check_interval: int = 7_200   # seconds (2 h repair check)
    category_backfill_once: bool = False

    # SAMPLE_DATA mode (testing): when true the real news APIs are NOT called
    # and data/sample_news.json is fed through the same pipeline instead.
    # Default false — production always uses the real providers.
    sample_data: bool = False

    # LLMPing (LLM Brain)
    llmping_base_url: str = "https://llmping.onrender.com"
    llmping_chat_path: str = "/chat"
    llmping_api_url: str = ""       # full-URL override; empty = build from base+path
    llmping_timeout: int = 60
    llmping_api_token: str = ""
    system_prompt: str = ""
    llm_max_content_chars: int = 4000


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
    cfg.max_articles = _parse_positive_int(os.getenv("MAX_ARTICLES", ""), 10)

    # Retention window.
    cfg.retention_days = _parse_positive_int(os.getenv("RETENTION_DAYS", ""), 7)

    # HTTP server.
    cfg.port = os.getenv("PORT", "8080").strip() or "8080"

    # Ingest interval: accept "2h", "30m", "3600s", or plain seconds integer.
    cfg.ingest_interval = _parse_duration_seconds(
        os.getenv("INGEST_INTERVAL", ""), default=7_200
    )
    # Run timeout must cover the fetch plus up to 10 sequential LLM parse calls.
    cfg.ingest_timeout = _parse_duration_seconds(
        os.getenv("INGEST_TIMEOUT", ""), default=900
    )
    # NULL/empty-field repair check cadence (2 h default).
    cfg.null_check_interval = _parse_duration_seconds(
        os.getenv("NULL_CHECK_INTERVAL", ""), default=7_200
    )

    # LLMPing (LLM Brain) options.
    cfg.llmping_base_url = (
        os.getenv("LLMPING_BASE_URL", "").strip() or "https://llmping.onrender.com"
    )
    cfg.llmping_chat_path = os.getenv("LLMPING_CHAT_PATH", "").strip() or "/chat"
    cfg.llmping_api_url   = os.getenv("LLMPING_API_URL", "").strip()
    cfg.llmping_timeout   = _parse_duration_seconds(os.getenv("LLMPING_TIMEOUT", ""), 60)
    cfg.llmping_api_token = os.getenv("LLMPING_API_TOKEN", "").strip()

    # System prompt: empty means llmping.DEFAULT_SYSTEM_PROMPT (placeholder) is used.
    cfg.system_prompt = os.getenv("LLM_SYSTEM_PROMPT", "").strip()
    cfg.llm_max_content_chars = _parse_positive_int(
        os.getenv("LLM_MAX_CONTENT_CHARS", ""), 4000
    )

    # CORS origins: comma-separated list, e.g. "https://app.last247.dev,https://..."
    raw_cors = os.getenv("CORS_ALLOW_ORIGINS", "").strip()
    if raw_cors:
        cfg.cors_origins = [o.strip() for o in raw_cors.split(",") if o.strip()]
    # else keep the default ["*"]

    # SAMPLE_DATA mode: strict truthy set — anything else (including unset)
    # means false so a typo can never accidentally turn sample mode on.
    cfg.sample_data = (
        os.getenv("SAMPLE_DATA", "").strip().lower() in ("1", "true", "yes", "on")
    )

    # One-shot / one-time flags — same strict truthy set as SAMPLE_DATA.
    cfg.run_once = (
        os.getenv("RUN_ONCE", "").strip().lower() in ("1", "true", "yes", "on")
    )
    cfg.category_backfill_once = (
        os.getenv("CATEGORY_BACKFILL_ONCE", "").strip().lower()
        in ("1", "true", "yes", "on")
    )

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
