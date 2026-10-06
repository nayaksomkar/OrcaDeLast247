"""
tests/test_providers.py — Unit tests for all four news providers.

All HTTP calls are intercepted using respx (or a local httpx mock transport)
— no real network requests are made.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
import pytest_asyncio
import httpx
import respx

from providers.base import parse_time
from providers.newsapi import NewsAPI
from providers.gnews import GNews
from providers.newsdata import NewsDataIO
from providers.webfetch import WebFetch


# ---------------------------------------------------------------------------
# parse_time
# ---------------------------------------------------------------------------

def test_parse_time_parses_rfc3339_with_z():
    dt = parse_time("2026-09-30T10:00:00Z")
    assert dt.year == 2026
    assert dt.month == 9
    assert dt.day == 30
    assert dt.tzinfo is not None


def test_parse_time_parses_space_separated():
    dt = parse_time("2026-09-30 10:00:00")
    assert dt.year == 2026


def test_parse_time_falls_back_to_now_for_bad_input():
    before = datetime.now(timezone.utc)
    dt = parse_time("not-a-date")
    after = datetime.now(timezone.utc)
    assert before <= dt <= after


def test_parse_time_handles_empty_string():
    before = datetime.now(timezone.utc)
    dt = parse_time("")
    assert dt >= before


# ---------------------------------------------------------------------------
# NewsAPI
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@respx.mock
async def test_newsapi_fetch_normalizes_articles():
    published = "2026-09-30T10:00:00Z"
    body = {
        "status": "ok",
        "articles": [
            {
                "title": "Test headline",
                "url": "https://example.com/a",
                "description": "desc",
                "content": "body",
                "urlToImage": "https://example.com/a.jpg",
                "publishedAt": published,
                "author": "Jane Doe",
                "source": {"id": "bbc", "name": "BBC News"},
            }
        ],
    }
    respx.get("https://newsapi.org/v2/everything").mock(
        return_value=httpx.Response(200, json=body)
    )
    provider = NewsAPI()
    articles = await provider.fetch("key", "en", 10, datetime(2026, 9, 1, tzinfo=timezone.utc))

    assert len(articles) == 1
    a = articles[0]
    assert a.title == "Test headline"
    assert a.url == "https://example.com/a"
    assert a.image_url == "https://example.com/a.jpg"
    assert a.source == "BBC News"
    assert a.author == "Jane Doe"


@pytest.mark.asyncio
@respx.mock
async def test_newsapi_raises_on_error_status():
    respx.get("https://newsapi.org/v2/everything").mock(
        return_value=httpx.Response(200, json={"status": "error", "message": "invalid key"})
    )
    with pytest.raises(RuntimeError, match="invalid key"):
        await NewsAPI().fetch("bad", "en", 10, datetime(2026, 9, 1, tzinfo=timezone.utc))


@pytest.mark.asyncio
@respx.mock
async def test_newsapi_raises_on_http_error():
    respx.get("https://newsapi.org/v2/everything").mock(
        return_value=httpx.Response(429)
    )
    with pytest.raises(Exception):
        await NewsAPI().fetch("key", "en", 10, datetime(2026, 9, 1, tzinfo=timezone.utc))


@pytest.mark.asyncio
@respx.mock
async def test_newsapi_drops_articles_without_url():
    body = {
        "status": "ok",
        "articles": [
            {"title": "No URL article", "url": "", "publishedAt": "2026-09-30T10:00:00Z"},
            {"title": "Good article", "url": "https://example.com/b", "publishedAt": "2026-09-30T10:00:00Z"},
        ],
    }
    respx.get("https://newsapi.org/v2/everything").mock(
        return_value=httpx.Response(200, json=body)
    )
    articles = await NewsAPI().fetch("key", "en", 10, datetime(2026, 9, 1, tzinfo=timezone.utc))
    assert len(articles) == 1
    assert articles[0].url == "https://example.com/b"


# ---------------------------------------------------------------------------
# GNews
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@respx.mock
async def test_gnews_fetch_normalizes_articles():
    body = {
        "articles": [
            {
                "title": "G headline",
                "url": "https://example.com/g",
                "description": "gdesc",
                "image": "https://example.com/g.jpg",
                "publishedAt": "2026-09-30T10:00:00Z",
                "author": "John",
                "source": {"name": "Reuters"},
            }
        ]
    }
    respx.get("https://gnews.io/api/v4/top-headlines").mock(
        return_value=httpx.Response(200, json=body)
    )
    articles = await GNews().fetch("tok", "en", 10, datetime(2026, 9, 1, tzinfo=timezone.utc))

    assert len(articles) == 1
    a = articles[0]
    assert a.title == "G headline"
    assert a.image_url == "https://example.com/g.jpg"
    assert a.source == "Reuters"


# ---------------------------------------------------------------------------
# NewsData.io
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@respx.mock
async def test_newsdata_joins_creators_and_takes_first_category():
    body = {
        "status": "success",
        "results": [
            {
                "title": "ND headline",
                "link": "https://example.com/nd",
                "description": "nddesc",
                "image_url": "https://example.com/nd.jpg",
                "creator": ["Alice", "Bob"],
                "category": ["technology", "science"],
                "source_id": "AP",
                "pubDate": "2026-09-30 10:00:00",
            }
        ],
    }
    respx.get("https://newsdata.io/api/1/news").mock(
        return_value=httpx.Response(200, json=body)
    )
    articles = await NewsDataIO().fetch("key", "en", 10, datetime(2026, 9, 1, tzinfo=timezone.utc))

    assert len(articles) == 1
    a = articles[0]
    assert a.author == "Alice, Bob"          # creators joined
    assert a.category == "technology"        # only first category
    assert a.url == "https://example.com/nd" # 'link' mapped to url


@pytest.mark.asyncio
@respx.mock
async def test_newsdata_raises_on_error_status():
    respx.get("https://newsdata.io/api/1/news").mock(
        return_value=httpx.Response(200, json={"status": "error", "message": "invalid"})
    )
    with pytest.raises(RuntimeError, match="invalid"):
        await NewsDataIO().fetch("bad", "en", 10, datetime(2026, 9, 1, tzinfo=timezone.utc))


# ---------------------------------------------------------------------------
# WebFetch
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@respx.mock
async def test_webfetch_prefers_image_url_over_image():
    body = {
        "articles": [
            {
                "title": "WF headline",
                "url": "https://example.com/wf",
                "image_url": "https://example.com/wf1.jpg",
                "image":     "https://example.com/wf2.jpg",   # fallback
                "published_at": "2026-09-30T10:00:00Z",
            }
        ]
    }
    url = "https://webfetch.example.com/news"
    respx.get(url).mock(return_value=httpx.Response(200, json=body))
    articles = await WebFetch(base_url=url).fetch("", "en", 10, datetime(2026, 9, 1, tzinfo=timezone.utc))

    assert articles[0].image_url == "https://example.com/wf1.jpg"  # preferred


@pytest.mark.asyncio
@respx.mock
async def test_webfetch_falls_back_to_image_field():
    body = {
        "articles": [
            {
                "title": "WF headline",
                "url": "https://example.com/wf",
                "image": "https://example.com/wf2.jpg",  # no image_url
                "published_at": "2026-09-30T10:00:00Z",
            }
        ]
    }
    url = "https://webfetch.example.com/news"
    respx.get(url).mock(return_value=httpx.Response(200, json=body))
    articles = await WebFetch(base_url=url).fetch("", "en", 10, datetime(2026, 9, 1, tzinfo=timezone.utc))

    assert articles[0].image_url == "https://example.com/wf2.jpg"


@pytest.mark.asyncio
async def test_webfetch_raises_when_no_base_url():
    with pytest.raises(RuntimeError, match="WEBFETCH_API_URL"):
        await WebFetch(base_url="").fetch("", "en", 10, datetime(2026, 9, 1, tzinfo=timezone.utc))


# ---------------------------------------------------------------------------
# Auth params + secret scrubbing (keys must never appear in error logs)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@respx.mock
async def test_gnews_sends_token_param():
    route = respx.get("https://gnews.io/api/v4/top-headlines").mock(
        return_value=httpx.Response(200, json={"articles": []})
    )
    await GNews().fetch("topsecret-token", "en", 7, datetime(2026, 9, 1, tzinfo=timezone.utc))

    params = dict(route.calls.last.request.url.params)
    assert params["token"] == "topsecret-token"
    assert params["max"] == "7"
    assert params["lang"] == "en"
    assert "from" not in params  # from/to are paid-plan-only on GNews


@pytest.mark.asyncio
@respx.mock
async def test_newsdata_sends_apikey_param():
    route = respx.get("https://newsdata.io/api/1/news").mock(
        return_value=httpx.Response(200, json={"status": "success", "results": []})
    )
    await NewsDataIO().fetch("topsecret-key", "en", 7, datetime(2026, 9, 1, tzinfo=timezone.utc))

    params = dict(route.calls.last.request.url.params)
    assert params["apikey"] == "topsecret-key"   # lowercase 'k' — NewsData requirement
    assert params["language"] == "en"


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize("provider_factory,routed,secret", [
    ("newsapi", "https://newsapi.org/v2/everything", "leaky-newsapi-key"),
    ("gnews", "https://gnews.io/api/v4/top-headlines", "leaky-gnews-token"),
    ("newsdata", "https://newsdata.io/api/1/news", "leaky-newsdata-key"),
])
async def test_provider_http_error_does_not_leak_api_key(provider_factory, routed, secret):
    """Failures must be raised as RuntimeError without the query string."""
    respx.get(routed).mock(return_value=httpx.Response(429))

    factories = {"newsapi": NewsAPI, "gnews": GNews, "newsdata": NewsDataIO}
    with pytest.raises(RuntimeError) as excinfo:
        await factories[provider_factory]().fetch(
            secret, "en", 7, datetime(2026, 9, 1, tzinfo=timezone.utc)
        )

    # The key must not be anywhere in the error text, and no URL with its
    # query string either (that's where the key travels for these APIs).
    rendered = str(excinfo.value)
    assert secret not in rendered
    assert "429" in rendered
    assert "apiKey=" not in rendered
    assert "token=" not in rendered


@pytest.mark.asyncio
@respx.mock
async def test_provider_connection_error_does_not_leak_api_key():
    respx.get("https://gnews.io/api/v4/top-headlines").mock(
        side_effect=httpx.ConnectError("refused")
    )
    with pytest.raises(RuntimeError) as excinfo:
        await GNews().fetch(
            "leaky-secret", "en", 7, datetime(2026, 9, 1, tzinfo=timezone.utc)
        )
    assert "leaky-secret" not in str(excinfo.value)
