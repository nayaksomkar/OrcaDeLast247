"""
tests/test_ingest.py — Tests for ingest.py fallback logic and ingestion lifecycle.
"""

from __future__ import annotations

from datetime import datetime, timezone
import pytest
import respx
import httpx

from config import Config
from database import count_articles, list_articles, upsert_article
from ingest import build_providers, provider_api_key, run_ingestion
from models import Article


def test_build_providers_orders_by_fallback_priority():
    cfg = Config(
        turso_url="file::memory:",
        newsapi_key="nk",
        gnews_api_key="gk",
        newsdata_api_key="ndk",
        webfetch_api_url="https://example.com/api",
    )
    providers = build_providers(cfg)
    names = [p.name for p in providers]
    assert names == ["newsapi", "gnews", "newsdata", "webfetch"]


def test_build_providers_skips_unconfigured_providers():
    cfg = Config(
        turso_url="file::memory:",
        gnews_api_key="gk",
    )
    providers = build_providers(cfg)
    assert len(providers) == 1
    assert providers[0].name == "gnews"


def test_provider_api_key_mapping():
    cfg = Config(
        turso_url="file::memory:",
        newsapi_key="k1",
        gnews_api_key="k2",
        newsdata_api_key="k3",
        webfetch_api_key="k4",
    )
    assert provider_api_key(cfg, "newsapi") == "k1"
    assert provider_api_key(cfg, "gnews") == "k2"
    assert provider_api_key(cfg, "newsdata") == "k3"
    assert provider_api_key(cfg, "webfetch") == "k4"
    assert provider_api_key(cfg, "unknown") == ""


@pytest.mark.asyncio
@respx.mock
async def test_run_ingestion_fetches_and_deduplicates(tmp_db):
    published = "2026-09-30T10:00:00Z"
    body = {
        "status": "ok",
        "articles": [
            {"title": "Story 1", "url": "https://example.com/1", "publishedAt": published},
            {"title": "Story 1 dup", "url": "https://example.com/1", "publishedAt": published},
            {"title": "", "url": "https://example.com/no-title", "publishedAt": published},
            {"title": "Story 2", "url": "https://example.com/2", "publishedAt": published},
        ],
    }
    respx.get("https://newsapi.org/v2/everything").mock(
        return_value=httpx.Response(200, json=body)
    )

    cfg = Config(
        turso_url="file::memory:",
        newsapi_key="test-key",
        retention_days=7,
    )

    result = await run_ingestion(cfg, tmp_db)

    assert result.provider == "newsapi"
    assert result.total == 3
    assert result.inserted == 2
    assert result.skipped == 1
    assert count_articles(tmp_db) == 2


@pytest.mark.asyncio
@respx.mock
async def test_run_ingestion_falls_back_on_failure(tmp_db):
    # Provider 1 (NewsAPI) fails
    respx.get("https://newsapi.org/v2/everything").mock(
        return_value=httpx.Response(500)
    )
    # Provider 2 (GNews) succeeds
    body = {
        "articles": [
            {"title": "GNews Story", "url": "https://example.com/gnews", "publishedAt": "2026-09-30T10:00:00Z"}
        ]
    }
    respx.get("https://gnews.io/v4/api/top-headlines").mock(
        return_value=httpx.Response(200, json=body)
    )

    cfg = Config(
        turso_url="file::memory:",
        newsapi_key="nk",
        gnews_api_key="gk",
    )

    result = await run_ingestion(cfg, tmp_db)
    assert result.provider == "gnews"
    assert result.inserted == 1
    assert count_articles(tmp_db) == 1
