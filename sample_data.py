"""
sample_data.py — SAMPLE_DATA mode: local sample articles instead of real APIs.

When SAMPLE_DATA=true, ingest.run_ingestion() replaces the provider fallback
loop with load_sample_articles() from here. Everything after that point —
the MAX_ARTICLES cap, the ProviderArticle → Article normalization, URL
dedup, the Turso upsert, the LLMPing parse phase and the retention sweep —
is the SAME code used for real API data, so this mode tests the actual
application flow without consuming any news-API quota.

The JSON file (data/sample_news.json) is grouped by provider key:

    {"newsapi": [...], "gnews": [...], "newsdata": [...]}

Each entry uses the normalized field names (the same shape every provider
converts its native JSON into):

    title, url            (required — invalid entries are logged as errors
    description, content    and skipped, never silently dropped)
    image_url, source, author, category
    published_at          (ISO 8601; unparsable values fall back to now,
                           matching provider behaviour)

No API keys belong in this file — it is committed and safe to share.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

from providers.base import ProviderArticle, parse_time

logger = logging.getLogger(__name__)

# Absolute default so the loader works regardless of the process's cwd
# (repo root in production, any cwd when invoked from scripts/tests).
_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SAMPLE_PATH = os.path.join(_REPO_ROOT, "data", "sample_news.json")

# Display order + labels: matches the ingestion provider fallback ordering.
_SAMPLE_GROUPS = (
    ("newsapi", "NewsAPI"),
    ("gnews", "GNews"),
    ("newsdata", "NewsData.io"),
)


def load_sample_articles(path: Optional[str] = None) -> list[ProviderArticle]:
    """
    Load the sample JSON and convert every entry to ProviderArticle.

    Returns articles in file order (newsapi first, then gnews, then
    newsdata), each tagged with .provider = "<group key>" so the DB record
    records which provider the article represents — proving all configured
    provider pipelines work.

    Entries without a title or URL are reported via logger.error (never
    silently ignored) and skipped; valid ones continue to the pipeline.
    A missing/invalid JSON file raises — the caller logs it and the run
    fails loudly rather than pretending to ingest.
    """
    path = path or DEFAULT_SAMPLE_PATH
    logger.info("[SAMPLE] Loading sample news from %s", path)

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    articles: list[ProviderArticle] = []
    for group_key, display in _SAMPLE_GROUPS:
        count = 0
        for i, entry in enumerate(data.get(group_key, []) or [], 1):
            pa = _normalize_sample_entry(group_key, entry)
            if pa is None:
                ident = entry.get("url") or entry.get("title") or f"#{i}"
                logger.error(
                    "[SAMPLE] invalid %s sample article (missing title or url): %s — skipped",
                    group_key, ident,
                )
                continue
            pa.provider = group_key
            articles.append(pa)
            count += 1
        logger.info("[SAMPLE] %s: %d article(s)", display, count)

    return articles


def _normalize_sample_entry(group_key: str, entry: dict) -> Optional[ProviderArticle]:
    """
    Convert one sample entry to ProviderArticle — the exact same normalized
    shape every real provider produces. Returns None for invalid entries.
    """
    if not isinstance(entry, dict):
        return None
    title = (entry.get("title") or "").strip()
    url = (entry.get("url") or "").strip()
    if not title or not url:
        return None  # same validity rule the real normalize/filter loop applies

    return ProviderArticle(
        title=title,
        url=url,
        description=entry.get("description") or "",
        content=entry.get("content") or "",
        image_url=entry.get("image_url") or "",
        source=entry.get("source") or "",
        author=entry.get("author") or "",
        category=entry.get("category") or "",
        published_at=parse_time(entry.get("published_at", "")),
    )
