"""
ingest.py — Core ingestion logic for the Last247 service.

run_ingestion(cfg, conn)   : execute one full collection cycle.
build_providers(cfg)        : build the ordered provider fallback list.
provider_api_key(cfg, name): return the API key for a given provider name.

Data flow (one cycle):
    1. Compute retention window start (from_time = now - retention_days).
    2. Build eligible provider list (providers with an API key or base URL).
    3. Try each provider in order; stop on the first that returns ≥1 article.
    4. Normalize ProviderArticle → Article (add id, fetched_at, provider).
    5. Deduplicate by URL within the run (DB handles cross-run dupes via UNIQUE).
    6. Upsert each valid article into the DB.
    7. Delete articles older than the retention window.
    8. Return IngestionResult summary.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from config import Config
from database import (
    article_id,
    count_articles,
    delete_stale_articles,
    upsert_article,
)
from models import Article, IngestionResult
from providers import GNews, NewsAPI, NewsDataIO, Provider, WebFetch

logger = logging.getLogger(__name__)


async def run_ingestion(cfg: Config, conn: Any) -> IngestionResult:
    """
    Execute one full ingestion cycle.

    Sequential fallback: providers are tried in order. The first provider
    that returns usable articles wins; later providers are never called.

    All providers failing is not an error — the function returns an empty
    IngestionResult (total=0). The main loop logs this but does not crash.

    Args:
        cfg  : runtime configuration (provider keys, retention days, etc.)
        conn : open database connection

    Returns:
        IngestionResult describing what happened during this cycle.
    """
    from_time = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ) - __import__("datetime").timedelta(days=cfg.retention_days)

    result = IngestionResult(
        source_time=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )

    provider_list = build_providers(cfg)
    if not provider_list:
        logger.warning("[ingest] no providers configured — skipping run")
        return result

    # --- Sequential fallback loop ---
    raw_articles = []
    winning_provider = ""

    for p in provider_list:
        try:
            fetched = await p.fetch(
                api_key=provider_api_key(cfg, p.name),
                lang=cfg.language,
                max_articles=cfg.max_articles,
                from_time=from_time,
            )
        except Exception as exc:
            # Log and try the next provider — a single failure is not fatal.
            logger.warning("[ingest] %s failed: %s", p.name, exc)
            continue

        if not fetched:
            logger.warning("[ingest] %s returned 0 articles", p.name)
            continue

        # First provider with usable articles wins.
        raw_articles = fetched
        winning_provider = p.name
        break

    result.provider = winning_provider
    result.total = len(raw_articles)

    if not raw_articles:
        # All providers failed or returned nothing.
        return result

    # --- Normalize, deduplicate, upsert ---
    now = datetime.now(timezone.utc)
    seen_urls: set[str] = set()
    inserted = 0
    skipped = 0

    for pa in raw_articles:
        # Drop articles without a title or URL.
        if not pa.url or not pa.title:
            skipped += 1
            continue

        # In-run deduplication by URL (DB handles cross-run via UNIQUE).
        if pa.url in seen_urls:
            skipped += 1
            continue
        seen_urls.add(pa.url)

        article = Article(
            id=article_id(pa.url),
            title=pa.title,
            description=pa.description or None,
            content=pa.content or None,
            url=pa.url,
            image_url=pa.image_url or None,
            source=pa.source or None,
            author=pa.author or None,
            category=pa.category or None,
            published_at=pa.published_at,
            fetched_at=now,
            provider=winning_provider,
        )

        try:
            # Run the blocking DB call in a thread so we don't block the
            # FastAPI event loop.
            await asyncio.to_thread(upsert_article, conn, article)
            inserted += 1
        except Exception as exc:
            logger.warning("[ingest] failed to store %s: %s", pa.url, exc)
            skipped += 1

    result.inserted = inserted
    result.skipped = skipped

    # --- Retention sweep ---
    try:
        deleted = await asyncio.to_thread(
            delete_stale_articles, conn, cfg.retention_days
        )
        result.deleted = deleted
    except Exception as exc:
        # A failed sweep is logged but does not abort the run — all stored
        # data is still valid, just one cycle larger than the window.
        logger.warning("[ingest] retention sweep failed: %s", exc)

    return result


def build_providers(cfg: Config) -> list[Provider]:
    """
    Build the ordered provider fallback list from config.

    A provider is eligible only when it has its API key (or, for WebFetch,
    its base URL) configured. Providers with empty keys are excluded.

    Fallback order is fixed:
        1. NewsAPI  — NEWS_API_KEY
        2. GNews    — GNEWS_API_KEY
        3. NewsData — NEWS_DATA_API_KEY
        4. WebFetch — WEBFETCH_API_URL  (uses URL, not a key)
    """
    providers: list[Provider] = []

    if cfg.newsapi_key:
        providers.append(NewsAPI())
    if cfg.gnews_api_key:
        providers.append(GNews())
    if cfg.newsdata_api_key:
        providers.append(NewsDataIO())
    # WebFetch is enabled by its base URL (no API key required).
    if cfg.webfetch_api_url:
        providers.append(WebFetch(base_url=cfg.webfetch_api_url))

    return providers


def provider_api_key(cfg: Config, name: str) -> str:
    """
    Return the API key for the given provider name.

    Unknown names return "" — the provider will surface an auth error at
    fetch time. This is the single mapping from provider name → config field,
    so adding a new provider means updating this alongside build_providers().
    """
    return {
        "newsapi":  cfg.newsapi_key,
        "gnews":    cfg.gnews_api_key,
        "newsdata": cfg.newsdata_api_key,
        "webfetch": cfg.webfetch_api_key,
    }.get(name, "")
