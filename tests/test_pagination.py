"""
tests/test_pagination.py — Tests for /api/news pagination + category filtering.

Covers the requested matrix: offset paging (0/5/10), category paging,
categories with more/fewer than a page, end-of-results, invalid values, and
no-filter behavior. Pagination, filtering, and ordering must happen in the
Turso SQL query (list_articles), with has_more derived from a limit+1
sentinel fetch — never from loading the whole table into Python.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from database import article_id, list_articles, upsert_article
from models import Article

PAGE = 5


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------

def _seed(tmp_db, n_tech: int = 7, n_sports: int = 3, n_none: int = 2) -> None:
    """Insert articles with staggered timestamps (newest first = high index)."""
    base = datetime.now(timezone.utc)
    for i in range(n_tech):
        upsert_article(tmp_db, Article(
            id=article_id(f"https://example.com/tech/{i}"),
            title=f"Tech {i}",
            url=f"https://example.com/tech/{i}",
            published_at=base - timedelta(minutes=i),  # tech/0 is newest
            fetched_at=base,
            provider="newsapi",
            category="technology",
        ))
    for i in range(n_sports):
        upsert_article(tmp_db, Article(
            id=article_id(f"https://example.com/sports/{i}"),
            title=f"Sports {i}",
            url=f"https://example.com/sports/{i}",
            published_at=base - timedelta(hours=1, minutes=i),
            fetched_at=base,
            provider="newsapi",
            category="sports",
        ))
    for i in range(n_none):
        upsert_article(tmp_db, Article(
            id=article_id(f"https://example.com/none/{i}"),
            title=f"None {i}",
            url=f"https://example.com/none/{i}",
            published_at=base - timedelta(hours=2, minutes=i),
            fetched_at=base,
            provider="newsapi",
        ))


async def _get(client, **params) -> dict:
    resp = await client.get("/api/news", params=params or None)
    assert resp.status_code == 200
    return resp.json()


# ---------------------------------------------------------------------------
# Plain pagination (no filters)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pagination_offsets_page_through_all_rows(async_client, tmp_db):
    _seed(tmp_db)  # 12 articles total
    total_seen: list[str] = []

    for offset in (0, 5, 10):
        data = await _get(async_client, limit=PAGE, offset=offset)
        assert len(data["articles"]) == (5 if offset in (0, 5) else 2)
        assert data["limit"] == PAGE
        assert data["offset"] == offset
        assert data["total"] == 12
        # has_more via the limit+1 sentinel: true while rows remain beyond
        # the page, false on the final page.
        assert data["has_more"] is (offset in (0, 5))
        total_seen.extend(a["id"] for a in data["articles"])

    # Pages are disjoint and cover the whole set exactly once (stable order).
    assert len(total_seen) == len(set(total_seen)) == 12

    # Beyond the end: empty page, has_more false.
    data = await _get(async_client, limit=PAGE, offset=15)
    assert data["articles"] == []
    assert data["has_more"] is False


@pytest.mark.asyncio
async def test_pagination_stable_ordering(async_client, tmp_db):
    _seed(tmp_db)
    page1 = await _get(async_client, limit=PAGE, offset=0)
    page2 = await _get(async_client, limit=PAGE, offset=PAGE)

    ids1 = [a["id"] for a in page1["articles"]]
    ids2 = [a["id"] for a in page2["articles"]]
    assert not set(ids1) & set(ids2)                     # no overlap
    # Newest first across the page boundary.
    assert page1["articles"][-1]["published_at"] >= page2["articles"][0]["published_at"]


@pytest.mark.asyncio
async def test_pagination_sentinel_fetch_returns_requested_page_only(
    async_client, tmp_db
):
    """limit=5 must yield exactly 5 rows even when more exist (sentinel
    row is trimmed before the response)."""
    _seed(tmp_db)
    data = await _get(async_client, limit=PAGE, offset=0)
    assert len(data["articles"]) == PAGE
    assert data["has_more"] is True


# ---------------------------------------------------------------------------
# Category filtering (SQL-side, case-insensitive)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_category_filter_with_paging_more_than_a_page(async_client, tmp_db):
    _seed(tmp_db)  # 7 technology articles

    page0 = await _get(async_client, category="technology", limit=PAGE, offset=0)
    assert len(page0["articles"]) == 5
    assert page0["total"] == 7
    assert page0["has_more"] is True
    assert all(a["category"] == "technology" for a in page0["articles"])

    page1 = await _get(async_client, category="technology", limit=PAGE, offset=PAGE)
    assert len(page1["articles"]) == 2
    assert page1["has_more"] is False

    # Offset 5 continues the category set without overlap.
    ids0 = {a["id"] for a in page0["articles"]}
    ids1 = {a["id"] for a in page1["articles"]}
    assert ids0.isdisjoint(ids1) and len(ids0 | ids1) == 7


@pytest.mark.asyncio
async def test_category_filter_case_insensitive(async_client, tmp_db):
    _seed(tmp_db)
    for variant in ("Technology", "TECHNOLOGY", "TeChNoLoGy"):
        data = await _get(async_client, category=variant, limit=PAGE, offset=0)
        assert data["total"] == 7, variant
        assert len(data["articles"]) == 5, variant


@pytest.mark.asyncio
async def test_category_filter_fewer_than_a_page(async_client, tmp_db):
    _seed(tmp_db)  # 3 sports articles < 5 per page
    data = await _get(async_client, category="sports", limit=PAGE, offset=0)
    assert len(data["articles"]) == 3
    assert data["total"] == 3
    assert data["has_more"] is False

    # offset past the filtered set: empty.
    data2 = await _get(async_client, category="sports", limit=PAGE, offset=PAGE)
    assert data2["articles"] == []
    assert data2["has_more"] is False


@pytest.mark.asyncio
async def test_no_category_filter_returns_all(async_client, tmp_db):
    _seed(tmp_db)
    data = await _get(async_client, limit=PAGE, offset=0)
    assert data["total"] == 12            # unfiltered
    filtered = await _get(async_client, category="", limit=PAGE, offset=0)
    assert filtered["total"] == 12        # empty category param = no filter
    # Articles without a category are never matched by a category filter.
    none_page = await _get(async_client, category="nonexistent", limit=PAGE, offset=0)
    assert none_page["total"] == 0
    assert none_page["articles"] == []


@pytest.mark.asyncio
async def test_category_filter_combined_with_source(async_client, tmp_db):
    base = datetime.now(timezone.utc)
    for i in range(7):
        upsert_article(tmp_db, Article(
            id=article_id(f"https://example.com/combo/{i}"),
            title=f"Combo {i}",
            url=f"https://example.com/combo/{i}",
            published_at=base - timedelta(minutes=i),
            fetched_at=base,
            provider="newsapi",
            source="Tech Daily",
            category="technology",
        ))
    # AND-combined: matching source keeps the rows...
    data = await _get(async_client, category="technology", source="Tech Daily",
                      limit=PAGE, offset=0)
    assert data["total"] == 7
    # ...a non-matching source (combined with the same category) drops them.
    data = await _get(async_client, category="technology", source="BBC",
                      limit=PAGE, offset=0)
    assert data["total"] == 0


# ---------------------------------------------------------------------------
# SQL-level check: filtering + pagination happen in the query
# ---------------------------------------------------------------------------

def test_list_articles_sql_page_is_exact(tmp_db):
    """Direct SQL-level check: the page function itself returns only the
    requested slice (the endpoint adds the +1 sentinel on top)."""
    _seed(tmp_db)
    page, total = list_articles(tmp_db, limit=4, offset=4)
    assert len(page) == 4
    assert total == 12

    beyond, total = list_articles(tmp_db, limit=4, offset=100)
    assert beyond == []
    assert total == 12


def test_list_articles_category_filter_is_sql_side(tmp_db):
    _seed(tmp_db)
    tech, total = list_articles(tmp_db, limit=50, offset=0, category="Technology")
    assert total == 7
    assert len(tech) == 7
    assert all(a.category == "technology" for a in tech)


# ---------------------------------------------------------------------------
# Invalid limit/offset values
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("params", [
    {"limit": 0},          # below ge=1
    {"limit": 101},        # above le=100
    {"limit": -5},
    {"limit": "abc"},      # not an int
    {"offset": -1},        # below ge=0
    {"offset": "xyz"},
])
async def test_invalid_limit_offset_rejected(async_client, params):
    resp = await async_client.get("/api/news", params=params)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_default_limit_and_offset(async_client, tmp_db):
    _seed(tmp_db)
    data = await _get(async_client)
    assert data["limit"] == 50
    assert data["offset"] == 0
    assert len(data["articles"]) == 12     # all 12 fit on one default page
    assert data["has_more"] is False
