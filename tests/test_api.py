"""
tests/test_api.py — Integration tests for the FastAPI HTTP endpoints.

Uses the `async_client` fixture (HTTPX AsyncClient wired to the FastAPI app
with an in-memory SQLite DB). No real server is started.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from database import article_id, upsert_article
from models import Article


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now():
    return datetime.now(timezone.utc)


def _article(url: str, title: str = "Test", **kwargs) -> Article:
    return Article(
        id=article_id(url),
        title=title,
        url=url,
        published_at=kwargs.pop("published_at", _now()),
        fetched_at=_now(),
        provider=kwargs.pop("provider", "newsapi"),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_health_returns_200(async_client):
    resp = await async_client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "timestamp" in data


# ---------------------------------------------------------------------------
# GET /api/news
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_articles_empty_db(async_client, tmp_db):
    resp = await async_client.get("/api/news")
    assert resp.status_code == 200
    data = resp.json()
    assert data["articles"] == []
    assert data["total"] == 0
    assert data["limit"] == 50
    assert data["offset"] == 0


@pytest.mark.asyncio
async def test_list_articles_returns_stored_articles(async_client, tmp_db):
    upsert_article(tmp_db, _article("https://example.com/a", "Story A"))
    upsert_article(tmp_db, _article("https://example.com/b", "Story B"))

    resp = await async_client.get("/api/news")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    assert len(data["articles"]) == 2


@pytest.mark.asyncio
async def test_list_articles_respects_limit_and_offset(async_client, tmp_db):
    for i in range(5):
        upsert_article(tmp_db, _article(f"https://example.com/{i}"))

    resp = await async_client.get("/api/news?limit=2&offset=0")
    data = resp.json()
    assert data["total"] == 5
    assert len(data["articles"]) == 2
    assert data["limit"] == 2
    assert data["offset"] == 0


@pytest.mark.asyncio
async def test_list_articles_clamps_limit_to_100(async_client, tmp_db):
    resp = await async_client.get("/api/news?limit=999")
    # FastAPI Query(le=100) rejects >100 with a 422
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_list_articles_filter_by_category(async_client, tmp_db):
    upsert_article(tmp_db, _article("https://a.com/1", category="tech"))
    upsert_article(tmp_db, _article("https://a.com/2", category="sports"))

    resp = await async_client.get("/api/news?category=tech")
    data = resp.json()
    assert data["total"] == 1
    assert data["articles"][0]["category"] == "tech"


@pytest.mark.asyncio
async def test_list_articles_filter_by_source(async_client, tmp_db):
    upsert_article(tmp_db, _article("https://a.com/bbc", source="BBC"))
    upsert_article(tmp_db, _article("https://a.com/cnn", source="CNN"))

    resp = await async_client.get("/api/news?source=BBC")
    data = resp.json()
    assert data["total"] == 1
    assert data["articles"][0]["source"] == "BBC"


@pytest.mark.asyncio
async def test_list_articles_omits_none_fields(async_client, tmp_db):
    """Optional fields with None should be omitted from JSON (not present as null)."""
    upsert_article(tmp_db, _article("https://example.com/min"))
    resp = await async_client.get("/api/news")
    article = resp.json()["articles"][0]
    # Fields with None values must NOT appear in the JSON at all.
    assert "description" not in article
    assert "image_url" not in article
    assert "author" not in article


# ---------------------------------------------------------------------------
# GET /api/news/{id}
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_article_by_id_found(async_client, tmp_db):
    a = _article("https://example.com/find-me", "Find Me")
    upsert_article(tmp_db, a)

    resp = await async_client.get(f"/api/news/{a.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == a.id
    assert data["title"] == "Find Me"


@pytest.mark.asyncio
async def test_get_article_by_id_not_found(async_client):
    resp = await async_client.get("/api/news/deadbeef00000000")
    assert resp.status_code == 404
    data = resp.json()
    assert data["code"] == "NOT_FOUND"
    assert "deadbeef00000000" in data["error"]


# ---------------------------------------------------------------------------
# GET /api/stats
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stats_returns_zero_on_empty_db(async_client, tmp_db):
    resp = await async_client.get("/api/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_articles"] == 0
    # No ingestion has run via this fixture → last_ingestion is absent.
    assert "last_ingestion" not in data


@pytest.mark.asyncio
async def test_stats_returns_correct_count(async_client, tmp_db):
    for i in range(3):
        upsert_article(tmp_db, _article(f"https://example.com/{i}"))

    resp = await async_client.get("/api/stats")
    assert resp.json()["total_articles"] == 3


# ---------------------------------------------------------------------------
# POST /api/ingest
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ingest_returns_success_with_no_providers(async_client):
    """With no provider keys configured the run succeeds but fetches nothing."""
    resp = await async_client.post("/api/ingest")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    result = data["result"]
    assert result["total"] == 0
    assert result["inserted"] == 0
    assert result["provider"] == ""


# ---------------------------------------------------------------------------
# CORS headers
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cors_headers_present_on_response(async_client):
    resp = await async_client.get(
        "/health", headers={"Origin": "https://app.last247.dev"}
    )
    # With CORS_ALLOW_ORIGINS=* the header should be present.
    assert "access-control-allow-origin" in resp.headers


@pytest.mark.asyncio
async def test_cors_options_preflight_returns_204(async_client):
    resp = await async_client.options(
        "/api/news",
        headers={
            "Origin": "https://app.last247.dev",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert resp.status_code == 200  # FastAPI CORSMiddleware returns 200 for OPTIONS
