"""
tests/test_categories.py — Tests for GET /api/news/categories.

The unique category list must come dynamically from the news table with
DISTINCT, the NULL/empty filter and the ordering performed inside the SQL
query — never hardcoded, and never by loading the whole table into Python.
The existing /api/news pagination behavior must stay untouched.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from database import article_id, list_categories, upsert_article
from models import Article


def _seed_cat(tmp_db, suffix: str, category: str | None) -> None:
    upsert_article(tmp_db, Article(
        id=article_id(f"https://example.com/cat/{suffix}"),
        title=f"Cat article {suffix}",
        url=f"https://example.com/cat/{suffix}",
        published_at=datetime.now(timezone.utc) - timedelta(minutes=int(suffix)),
        fetched_at=datetime.now(timezone.utc),
        provider="newsapi",
        category=category,
    ))


# ---------------------------------------------------------------------------
#database.list_categories — SQL-side distinct/trim/order
# ---------------------------------------------------------------------------

def test_list_categories_unique_nonempty_sorted(tmp_db):
    for cat, suffix in (
        ("sports", "1"),
        ("technology", "2"),
        ("business", "3"),
        ("sports", "4"),          # exact duplicate
        (" sports ", "5"),        # padded duplicate (trims to same value)
        ("", "6"),                # empty string → excluded
        (None, "7"),              # NULL → excluded
    ):
        _seed_cat(tmp_db, suffix, cat)

    cats = list_categories(tmp_db)

    assert cats == ["business", "sports", "technology"]


def test_list_categories_case_insensitive_sort_stable(tmp_db):
    # insert 'Tech' and 'technology' — distinct raw values, same lower() key;
    # ordering must be deterministic either way.
    for cat, suffix in (("tech", "1"), ("Tech", "2"), ("basketball", "3")):
        _seed_cat(tmp_db, suffix, cat)

    cats = list_categories(tmp_db)
    assert cats == list_categories(tmp_db)          # stable across calls
    assert cats == sorted(cats, key=str.lower)


def test_list_categories_empty_table_is_empty_list(tmp_db):
    assert list_categories(tmp_db) == []


# ---------------------------------------------------------------------------
# GET /api/news/categories — endpoint contract
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_categories_endpoint_contract(async_client, tmp_db):
    for cat, suffix in (
        ("sports black", "1"),
        ("technology", "2"),
        ("business", "3"),
        ("sports black", "4"),    # duplicate row value
        ("", "5"),                # empty
        (None, "6"),              # NULL
    ):
        _seed_cat(tmp_db, suffix, cat)

    resp = await async_client.get("/api/news/categories")
    assert resp.status_code == 200
    data = resp.json()
    assert list(data.keys()) == ["categories"]

    categories = data["categories"]
    assert isinstance(categories, list)
    assert all(isinstance(c, str) for c in categories)   # no nulls
    assert all(c != "" for c in categories)              # no empty strings
    assert len(categories) == len(set(categories))       # no duplicates
    assert categories == ["business", "sports black", "technology"]


# ---------------------------------------------------------------------------
# /api/news pagination + filtering still work with a value from this list
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_articles_for_category_returned_by_categories_endpoint(
    async_client, tmp_db
):
    _test_seed(tmp_db)

    cats = (await async_client.get("/api/news/categories")).json()["categories"]
    assert cats == ["sports", "technology"]

    tested = 0
    for cat in cats:
        resp = await async_client.get(
            "/api/news", params={"category": cat, "limit": 5, "offset": 0}
        )
        assert resp.status_code == 200
        articles = resp.json()["articles"]
        assert all(a["category"].lower() == cat.lower() for a in articles)
        tested += 1
    assert tested == 2

    # Unknown category → empty page, still 200.
    resp = await async_client.get(
        "/api/news", params={"category": "nonexistent", "limit": 5, "offset": 0}
    )
    assert resp.status_code == 200
    assert resp.json()["articles"] == []


def _test_seed(tmp_db) -> None:
    upsert_article(tmp_db, Article(
        id=article_id("https://example.com/more/1"),
        title="Technology weekly",
        url="https://example.com/more/1",
        published_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        fetched_at=datetime.now(timezone.utc),
        provider="newsapi",
        category="technology",
    ))
    for i in range(2):
        upsert_article(tmp_db, Article(
            id=article_id(f"https://example.com/more/s{i}"),
            title=f"Sports brief {i}",
            url=f"https://example.com/more/s{i}",
            published_at=datetime.now(timezone.utc) - timedelta(hours=1, minutes=i),
            fetched_at=datetime.now(timezone.utc),
            provider="newsapi",
            category="sports",
        ))