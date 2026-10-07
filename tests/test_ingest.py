"""
tests/test_ingest.py — Tests for ingest.py fallback logic, ingestion lifecycle,
and the per-article LLM parse phase.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import httpx
import pytest
import respx

from config import Config
from database import count_articles, get_article_by_id, list_articles, upsert_article, article_id
from ingest import build_providers, provider_api_key, run_ingestion
from models import Article

CHAT_URL = "https://llmping.onrender.com/chat"


def _recent_published() -> str:
    """
    Fixture articles must sit inside the retention window no matter when the
    suite runs — hardcoded dates age out of RETENTION_DAYS and the retention
    sweep silently deletes the rows mid-test.
    """
    return (datetime.now(timezone.utc) - timedelta(days=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


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
    published = _recent_published()
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
    chat_route = respx.post(CHAT_URL).mock(
        return_value=httpx.Response(
            200, json={"answer": "PARSED", "provider": "groq", "model": "llama-3"}
        )
    )

    cfg = Config(
        turso_url="file::memory:",
        newsapi_key="test-key",
        retention_days=7,
    )

    result = await run_ingestion(cfg, tmp_db)

    assert result.provider == "newsapi"
    # Dedup now happens while accumulating the fetch set: the duplicate URL
    # and the title-less entry are dropped before counting, so `total` is
    # the usable collected set (≤ MAX_ARTICLES).
    assert result.total == 2
    assert result.inserted == 2
    assert result.skipped == 0
    assert count_articles(tmp_db) == 2

    # LLM parse phase: one /chat call per stored article.
    assert chat_route.call_count == 2
    assert result.parsed == 2
    assert result.parse_failed == 0

    # The prompt carries instructions and article data in separate sections.
    payload = json.loads(chat_route.calls.last.request.content)
    assert "=== SYSTEM INSTRUCTIONS ===" in payload["query"]
    assert "=== ARTICLE DATA ===" in payload["query"]
    assert "TITLE: Story 2" in payload["query"]

    # Parsed answers are stored on the rows.
    articles, _ = list_articles(tmp_db, 10, 0)
    for a in articles:
        assert a.llm_answer == "PARSED"
        assert a.llm_provider == "groq"
        assert a.llm_model == "llama-3"
        assert a.llm_processed_at


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
            {"title": "GNews Story", "url": "https://example.com/gnews", "publishedAt": _recent_published()}
        ]
    }
    respx.get("https://gnews.io/api/v4/top-headlines").mock(
        return_value=httpx.Response(200, json=body)
    )
    respx.post(CHAT_URL).mock(
        return_value=httpx.Response(200, json={"answer": "PARSED", "provider": "p", "model": "m"})
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
    assert result.parsed == 1
    assert result.parse_failed == 0


# ---------------------------------------------------------------------------
# LLM parse phase — failure handling
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@respx.mock
async def test_run_ingestion_caps_articles_at_max_articles(tmp_db):
    """MAX_ARTICLES is a hard cap per run (NewsData ignores page-size → 10 rows)."""
    published = _recent_published()
    respx.get("https://newsapi.org/v2/everything").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "ok",
                "articles": [
                    {"title": f"Story {i}", "url": f"https://example.com/{i}",
                     "publishedAt": published}
                    for i in range(10)
                ],
            },
        )
    )
    chat_route = respx.post(CHAT_URL).mock(
        return_value=httpx.Response(200, json={"answer": "PARSED", "provider": "p", "model": "m"})
    )

    cfg = Config(
        turso_url="file::memory:",
        newsapi_key="test-key",
        max_articles=7,          # 10 fetched upstream → only 7 processed
    )
    result = await run_ingestion(cfg, tmp_db)

    assert result.inserted == 7
    assert result.parsed == 7
    assert chat_route.call_count == 7     # exactly 7 LLM calls, no more
    assert count_articles(tmp_db) == 7


@pytest.mark.asyncio
@respx.mock
async def test_run_ingestion_llm_failure_skips_article_and_continues(tmp_db):
    published = _recent_published()
    respx.get("https://newsapi.org/v2/everything").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "ok",
                "articles": [
                    {"title": "Story 1", "url": "https://example.com/1", "publishedAt": published},
                    {"title": "Story 2", "url": "https://example.com/2", "publishedAt": published},
                    {"title": "Story 3", "url": "https://example.com/3", "publishedAt": published},
                ],
            },
        )
    )

    # Story 2's parse fails upstream; the others succeed.
    def chat_side_effect(request: httpx.Request) -> httpx.Response:
        if b"Story 2" in request.content:
            return httpx.Response(500)
        return httpx.Response(200, json={"answer": "PARSED", "provider": "p", "model": "m"})

    respx.post(CHAT_URL).mock(side_effect=chat_side_effect)

    cfg = Config(turso_url="file::memory:", newsapi_key="test-key")
    result = await run_ingestion(cfg, tmp_db)

    # The failed article did not abort the run — the next one was processed.
    assert result.inserted == 3
    assert result.parsed == 2
    assert result.parse_failed == 1
    assert count_articles(tmp_db) == 3

    failed = get_article_by_id(tmp_db, article_id("https://example.com/2"))
    assert failed is not None           # article itself is kept
    assert failed.llm_answer is None    # parse result simply absent
    ok = get_article_by_id(tmp_db, article_id("https://example.com/3"))
    assert ok.llm_answer == "PARSED"


@pytest.mark.asyncio
@respx.mock
async def test_run_ingestion_llm_down_run_completes(tmp_db):
    """LLMPing fully unreachable: run completes, no data fabricated, rows kept."""
    published = _recent_published()
    respx.get("https://newsapi.org/v2/everything").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "ok",
                "articles": [
                    {"title": "Story 1", "url": "https://example.com/1", "publishedAt": published},
                    {"title": "Story 2", "url": "https://example.com/2", "publishedAt": published},
                ],
            },
        )
    )
    respx.post(CHAT_URL).mock(side_effect=httpx.ConnectError("service down"))

    cfg = Config(turso_url="file::memory:", newsapi_key="test-key")
    result = await run_ingestion(cfg, tmp_db)

    assert result.inserted == 2
    assert result.parsed == 0
    assert result.parse_failed == 2
    assert count_articles(tmp_db) == 2
    articles, _ = list_articles(tmp_db, 10, 0)
    assert all(a.llm_answer is None for a in articles)
