"""
tests/test_database.py — Unit tests for database.py.

All tests use the `tmp_db` fixture (in-memory SQLite, schema pre-created).
No network, no Turso account, no files on disk.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from database import (
    article_id,
    count_articles,
    delete_stale_articles,
    get_article_by_id,
    list_articles,
    upsert_article,
)
from models import Article


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _make_article(
    url: str = "https://example.com/article",
    title: str = "Test Article",
    published_at: datetime | None = None,
) -> Article:
    return Article(
        id=article_id(url),
        title=title,
        url=url,
        published_at=published_at or _now(),
        fetched_at=_now(),
        provider="newsapi",
    )


# ---------------------------------------------------------------------------
# article_id
# ---------------------------------------------------------------------------

def test_article_id_is_16_chars():
    assert len(article_id("https://example.com")) == 16


def test_article_id_is_deterministic():
    url = "https://example.com/story"
    assert article_id(url) == article_id(url)


def test_article_id_differs_for_different_urls():
    assert article_id("https://a.com") != article_id("https://b.com")


# ---------------------------------------------------------------------------
# upsert_article + count_articles
# ---------------------------------------------------------------------------

def test_upsert_inserts_new_article(tmp_db):
    upsert_article(tmp_db, _make_article())
    assert count_articles(tmp_db) == 1


def test_count_returns_zero_on_empty_db(tmp_db):
    assert count_articles(tmp_db) == 0


def test_upsert_does_not_duplicate_same_url(tmp_db):
    a = _make_article(url="https://example.com/dup")
    upsert_article(tmp_db, a)
    upsert_article(tmp_db, a)   # second insert → ON CONFLICT update
    assert count_articles(tmp_db) == 1


def test_upsert_updates_fields_on_conflict(tmp_db):
    url = "https://example.com/update"
    old = Article(
        id=article_id(url), title="Old Title", url=url,
        published_at=_now(), fetched_at=_now(), provider="newsapi",
    )
    upsert_article(tmp_db, old)

    new = Article(
        id=article_id(url), title="New Title", url=url,
        published_at=_now(), fetched_at=_now(), provider="gnews",
        description="Updated desc",
    )
    upsert_article(tmp_db, new)

    stored = get_article_by_id(tmp_db, article_id(url))
    assert stored is not None
    assert stored.title == "New Title"
    assert stored.provider == "gnews"


def test_multiple_articles_stored(tmp_db):
    for i in range(5):
        upsert_article(tmp_db, _make_article(url=f"https://example.com/{i}"))
    assert count_articles(tmp_db) == 5


# ---------------------------------------------------------------------------
# get_article_by_id
# ---------------------------------------------------------------------------

def test_get_article_by_id_returns_article(tmp_db):
    a = _make_article(url="https://example.com/find-me")
    upsert_article(tmp_db, a)
    found = get_article_by_id(tmp_db, a.id)
    assert found is not None
    assert found.url == a.url
    assert found.title == a.title


def test_get_article_by_id_returns_none_when_missing(tmp_db):
    assert get_article_by_id(tmp_db, "nonexistent0000") is None


# ---------------------------------------------------------------------------
# list_articles
# ---------------------------------------------------------------------------

def test_list_articles_returns_all(tmp_db):
    for i in range(3):
        upsert_article(tmp_db, _make_article(url=f"https://example.com/{i}"))
    articles, total = list_articles(tmp_db, limit=50, offset=0)
    assert total == 3
    assert len(articles) == 3


def test_list_articles_empty_db(tmp_db):
    articles, total = list_articles(tmp_db, limit=50, offset=0)
    assert articles == []
    assert total == 0


def test_list_articles_pagination(tmp_db):
    for i in range(10):
        upsert_article(tmp_db, _make_article(url=f"https://example.com/{i:02d}"))

    page1, total = list_articles(tmp_db, limit=4, offset=0)
    page2, _     = list_articles(tmp_db, limit=4, offset=4)
    page3, _     = list_articles(tmp_db, limit=4, offset=8)

    assert total == 10
    assert len(page1) == 4
    assert len(page2) == 4
    assert len(page3) == 2
    # All returned URLs should be unique across pages.
    all_urls = {a.url for a in page1 + page2 + page3}
    assert len(all_urls) == 10


def test_list_articles_filter_by_category(tmp_db):
    a = Article(
        id=article_id("https://example.com/tech"),
        title="Tech Story", url="https://example.com/tech",
        category="technology",
        published_at=_now(), fetched_at=_now(), provider="newsapi",
    )
    b = Article(
        id=article_id("https://example.com/sport"),
        title="Sport Story", url="https://example.com/sport",
        category="sports",
        published_at=_now(), fetched_at=_now(), provider="newsapi",
    )
    upsert_article(tmp_db, a)
    upsert_article(tmp_db, b)

    results, total = list_articles(tmp_db, limit=50, offset=0, category="technology")
    assert total == 1
    assert results[0].category == "technology"


def test_list_articles_filter_by_source(tmp_db):
    for source, n in [("BBC", 2), ("CNN", 3)]:
        for i in range(n):
            art = Article(
                id=article_id(f"https://example.com/{source}/{i}"),
                title=f"{source} story {i}",
                url=f"https://example.com/{source}/{i}",
                source=source,
                published_at=_now(), fetched_at=_now(), provider="newsapi",
            )
            upsert_article(tmp_db, art)

    results, total = list_articles(tmp_db, limit=50, offset=0, source="BBC")
    assert total == 2
    assert all(a.source == "BBC" for a in results)


def test_list_articles_ordered_newest_first(tmp_db):
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i in range(3):
        a = _make_article(
            url=f"https://example.com/{i}",
            published_at=base + timedelta(hours=i),
        )
        upsert_article(tmp_db, a)

    articles, _ = list_articles(tmp_db, limit=50, offset=0)
    # Should be newest first.
    for j in range(len(articles) - 1):
        assert articles[j].published_at >= articles[j + 1].published_at


# ---------------------------------------------------------------------------
# delete_stale_articles
# ---------------------------------------------------------------------------

def test_delete_stale_articles_removes_old_rows(tmp_db):
    old = _make_article(
        url="https://example.com/old",
        published_at=datetime.now(timezone.utc) - timedelta(days=10),
    )
    fresh = _make_article(
        url="https://example.com/fresh",
        published_at=datetime.now(timezone.utc),
    )
    upsert_article(tmp_db, old)
    upsert_article(tmp_db, fresh)

    deleted = delete_stale_articles(tmp_db, retention_days=7)
    assert deleted == 1
    assert count_articles(tmp_db) == 1


def test_delete_stale_articles_returns_zero_when_nothing_stale(tmp_db):
    upsert_article(tmp_db, _make_article())
    deleted = delete_stale_articles(tmp_db, retention_days=7)
    assert deleted == 0


def test_delete_stale_articles_on_empty_db(tmp_db):
    assert delete_stale_articles(tmp_db, retention_days=7) == 0
