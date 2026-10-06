"""
ingest.py — Core ingestion logic for the Last247 service.

run_ingestion(cfg, conn)   : execute one full collection cycle.
build_providers(cfg)        : build the ordered provider fallback list.
provider_api_key(cfg, name): return the API key for a given provider name.

Data flow (one cycle):
    1. Compute retention window start (from_time = now - retention_days).
    2. SAMPLE_DATA mode (cfg.sample_data=true): skip the provider loop and
       load data/sample_news.json via sample_data.load_sample_articles()
       instead — every other step below is shared with the real path.
    3. Build eligible provider list (providers with an API key or base URL).
    4. Try each provider in order; stop on the first that returns ≥1 article.
    5. Normalize ProviderArticle → Article (add id, fetched_at, provider —
       sample data carries its own provider tag per article).
    6. Deduplicate by URL within the run (DB handles cross-run dupes via UNIQUE).
    7. Upsert each valid article into the DB.
    8. LLM phase: for each upserted article (max cfg.max_articles = 7), send
       system prompt + article data to LLMPing /chat and store the parsed
       answer on the row (llm_* columns). Failures are logged and skipped.
    9. Delete articles older than the retention window.
   10. Return IngestionResult summary.
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
    save_llm_parse,
    upsert_article,
)
from llmping import build_article_prompt, resolve_system_prompt
from llmping import chat as llmping_chat
from models import Article, IngestionResult
from providers import GNews, NewsAPI, NewsDataIO, Provider, WebFetch
import sample_data

logger = logging.getLogger(__name__)


async def run_ingestion(cfg: Config, conn: Any) -> IngestionResult:
    """
    Execute one full ingestion cycle.

    Sequential fallback: providers are tried in order. The first provider
    that returns usable articles wins; later providers are never called.

    After the upsert loop, every stored article is sent to the LLMPing LLM
    Brain (system prompt + article data) and the parsed answer is persisted
    on the article's row. All providers failing is not an error — the
    function returns an empty IngestionResult (total=0). The main loop logs
    this but does not crash.

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

    if cfg.sample_data:
        # --- SAMPLE_DATA mode -------------------------------------------
        # Testing shortcut: skip the provider fallback loop entirely and
        # feed the bundled sample articles through the exact same pipeline
        # below (cap → normalize → dedup → upsert → LLMPing → retention).
        # Provider keys are ignored here; each sample entry carries its own
        # provider attribution (newsapi/gnews/newsdata) on .provider.
        logger.info("[SAMPLE] SAMPLE_DATA=true — real news APIs are NOT called")
        raw_articles = sample_data.load_sample_articles()
        winning_provider = ""       # per-article attribution comes from the data
    else:
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

    result.provider = winning_provider if not cfg.sample_data else "sample"
    result.total = len(raw_articles)

    if not raw_articles:
        # All providers failed or returned nothing.
        return result

    # --- Normalize, deduplicate, upsert ---
    # HARD CAP: some providers (NewsData.io) ignore the page-size param and
    # return more rows than requested. MAX_ARTICLES is the per-run budget
    # (7 → 7 sequential LLM calls), so trim the raw list before processing.
    now = datetime.now(timezone.utc)
    seen_urls: set[str] = set()
    stored_this_run: list[Article] = []
    inserted = 0
    skipped = 0

    for pa in raw_articles[: cfg.max_articles]:
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
            # SAMPLE_DATA mode tags each article with its own provider
            # (empty for real providers → the winning provider's name).
            provider=pa.provider or winning_provider,
        )

        try:
            # Run the blocking DB call in a thread so we don't block the
            # FastAPI event loop.
            await asyncio.to_thread(upsert_article, conn, article)
            inserted += 1
            stored_this_run.append(article)
            logger.info("[DB] Stored article: %s (%s)", article.title, article.provider)
        except Exception as exc:
            logger.warning("[ingest] failed to store %s: %s", pa.url, exc)
            skipped += 1

    result.inserted = inserted
    result.skipped = skipped

    # --- LLM parse phase ---
    # Each stored article is parsed by the LLMPing LLM Brain exactly once per
    # run, sequentially, and its parsed answer is persisted on the row.
    # A failure for one article never aborts the run — the article stays
    # stored with llm_answer=None and the next article is processed.
    logger.info("[ingest] LLM parse phase: %d article(s) queued", len(stored_this_run))
    total_articles_to_parse = len(stored_this_run)
    for idx, article in enumerate(stored_this_run, 1):
        logger.info("[LLM] Processing article %d/%d: %s", idx, total_articles_to_parse, article.title)
        try:
            res = await llmping_chat(
                cfg,
                query=build_article_prompt(cfg, article),
            )
            await asyncio.to_thread(
                save_llm_parse,
                conn,
                article.id,
                res.answer,
                res.provider,
                res.model,
                datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
            result.parsed += 1
            logger.info(
                "[ingest] parsed %s via %s/%s",
                article.url, res.provider or "?", res.model or "?",
            )
        except Exception as exc:
            logger.warning(
                "[ingest] LLM parse failed for %s: %s — skipping article",
                article.url, exc,
            )
            result.parse_failed += 1

    if stored_this_run:
        logger.info(
            "[ingest] LLM phase done: parsed=%d failed=%d",
            result.parsed, result.parse_failed,
        )

    # --- Sample-mode completion summary ------------------------------------
    # Explicit end-of-run report for SAMPLE_DATA mode (failed = skipped during
    # normalize/upsert + LLM parse failures — dirty articles are counted, never
    # silently discarded).
    if cfg.sample_data and raw_articles:
        failed = skipped + result.parse_failed
        logger.info(
            "[SAMPLE] Sample ingestion completed\n"
            "Articles loaded: %d\n"
            "LLM processed: %d\n"
            "Database stored: %d\n"
            "Failed: %d",
            len(raw_articles), result.parsed, inserted, failed,
        )

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
