"""
tests/test_repair.py — Tests for category extraction/fallback, the meta
marker store, the incomplete-row finder, whitelisted field updates, and the
idempotent NULL/empty repair pass (repair.py).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from config import Config
from database import (
    get_article_by_id,
    list_articles,
    list_incomplete_articles,
    meta_get,
    meta_set,
    save_llm_parse,
    update_article_fields,
    upsert_article,
)
from ingest import run_ingestion
from models import Article
from repair import (
    _clean_category,
    classify_from_article_data,
    extract_category,
    fallback_category,
    repair_articles,
)

CHAT_URL = "https://llmping.onrender.com/chat"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_article(
    tmp_db,
    suffix: str = "1",
    *,
    category: str | None = None,
    llm_answer: str | None = None,
    llm_provider: str | None = None,
    llm_model: str | None = None,
    llm_processed_at: str | None = None,
) -> Article:
    """Build, store, and return an article with the given field states."""
    published = datetime.now(timezone.utc) - timedelta(days=1)
    article = Article(
        id="a" * 15 + suffix,
        title=f"Story {suffix}",
        url=f"https://example.com/{suffix}",
        published_at=published,
        fetched_at=published,
        provider="newsapi",
        category=category,
        llm_answer=llm_answer,
        llm_provider=llm_provider,
        llm_model=llm_model,
        llm_processed_at=llm_processed_at,
    )
    upsert_article(tmp_db, article)
    # upsert_article() never writes llm_* — persist them like the real
    # pipeline does (save_llm_parse) so the row reflects the given state.
    if llm_answer or llm_provider or llm_model or llm_processed_at:
        save_llm_parse(
            tmp_db,
            article.id,
            llm_answer or "",
            llm_provider or "",
            llm_model or "",
            llm_processed_at or "",
        )
    return article


def full_llm(**overrides) -> dict:
    """A complete llm_* field set (healthy state)."""
    fields = {
        "llm_answer": json.dumps({"title": "t", "category": "science"}),
        "llm_provider": "groq",
        "llm_model": "llama-3",
        "llm_processed_at": "2026-10-01T00:00:00Z",
    }
    fields.update(overrides)
    return fields


@pytest.fixture
def repair_cfg() -> Config:
    return Config(turso_url="file::memory:")


# ---------------------------------------------------------------------------
# Category helpers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("Science", "science"),
    ("  World News  ", "world news"),
    ("Arts & Culture.", "arts & culture"),
    ("Sports", "sports"),
])
def test_clean_category_valid(raw, expected):
    assert _clean_category(raw) == expected


@pytest.mark.parametrize("raw", [
    None, "", "   ", "a" * 41, "one two three four",
    "Category: science!", "政治",
])
def test_clean_category_invalid(raw):
    assert _clean_category(raw) is None


def test_extract_category_from_parse_json():
    answer = json.dumps({
        "title": "t", "url": "u", "category": "Technology",
        "summary": "s", "key_points": [],
    })
    assert extract_category(answer) == "technology"


def test_extract_category_rejects_bad_answers():
    assert extract_category(None) is None
    assert extract_category("") is None
    assert extract_category("PARSED plain text") is None           # not JSON
    assert extract_category('["not", "a", "dict"]') is None        # not a dict
    assert extract_category('{"title": "no category key"}') is None
    assert extract_category('{"category": 42}') is None            # not a string


def test_fallback_category_uses_provider_category(tmp_db):
    article = make_article(tmp_db, "prov", category=" Finance ")
    assert fallback_category(article) == "finance"


def test_fallback_category_none_without_provider_category(tmp_db):
    article = make_article(tmp_db, "noprov")
    assert fallback_category(article) is None


# ---------------------------------------------------------------------------
# Local classification from stored article data
# ---------------------------------------------------------------------------

def make_unsaved_article(suffix: str, title: str, **kwargs) -> Article:
    return Article(
        id="a" * 15 + suffix,
        title=title,
        url=f"https://example.com/{suffix}",
        published_at=datetime.now(timezone.utc) - timedelta(days=1),
        fetched_at=datetime.now(timezone.utc) - timedelta(days=1),
        provider="newsapi",
        **kwargs,
    )


@pytest.mark.parametrize("title,expected", [
    ("Lakers defeat Celtics in NBA playoffs overtime thriller", "sports"),
    ("Yankees clinch championship after extra innings goal fest", "sports"),
    ("New AI chip from OpenAI pushes smartphone technology forward", "technology"),
    ("Federal Reserve raises rates as inflation squeezes markets", "business"),
    ("Senate passes election bill after tense congress vote", "politics"),
    ("NASA rover discovers new species data on Mars", "science"),
    ("Netflix releases trailer for reality tv series", "entertainment"),
])
def test_classify_from_article_data(title, expected):
    article = make_unsaved_article("cls1", title)
    assert classify_from_article_data(article) == expected


def test_classify_requires_clear_margin():
    # Two categories tie → no confident answer → None (LLM fallback).
    article = make_unsaved_article(
        "amb1", "Election debate over tech market economy policy"
    )
    assert classify_from_article_data(article) is None


def test_classify_none_without_signals():
    article = make_unsaved_article("nosig1", "Story nosig1")
    assert classify_from_article_data(article) is None


def test_classify_uses_source_hint():
    article = make_unsaved_article(
        "src1", "A big night at the arena",
        source="sportingnews.com/us/mlb",
    )
    assert classify_from_article_data(article) == "sports"


# ---------------------------------------------------------------------------
# Meta store
# ---------------------------------------------------------------------------

def test_meta_roundtrip_and_overwrite(tmp_db):
    assert meta_get(tmp_db, "missing") is None
    meta_set(tmp_db, "k", "v1")
    assert meta_get(tmp_db, "k") == "v1"
    meta_set(tmp_db, "k", "v2")            # idempotent upsert
    assert meta_get(tmp_db, "k") == "v2"


# ---------------------------------------------------------------------------
# update_article_fields — whitelist + selective update
# ---------------------------------------------------------------------------

def test_update_article_fields_updates_only_given_columns(tmp_db):
    article = make_article(tmp_db, "u1", **full_llm())
    n = update_article_fields(tmp_db, article.id, {"category": "sports"})
    assert n == 1
    row = get_article_by_id(tmp_db, article.id)
    assert row.category == "sports"
    assert row.llm_answer == article.llm_answer     # untouched
    assert row.title == "Story u1"                  # untouched


def test_update_article_fields_rejects_unknown_columns(tmp_db):
    article = make_article(tmp_db, "u2")
    # Whitelisted columns only — id/url/published_at can never be touched.
    assert update_article_fields(
        tmp_db, article.id, {"url": "https://evil.example", "id": "x"}
    ) == 0
    row = get_article_by_id(tmp_db, article.id)
    assert row.url == article.url
    assert row.id == article.id


def test_update_article_fields_unknown_id_returns_zero(tmp_db):
    assert update_article_fields(tmp_db, "ffffffffffffffff", {"category": "x"}) == 0


# ---------------------------------------------------------------------------
# list_incomplete_articles — healthy rows never returned
# ---------------------------------------------------------------------------

def test_list_incomplete_articles_predicate(tmp_db):
    healthy = make_article(tmp_db, "h1", category="science", **full_llm())
    no_category = make_article(tmp_db, "c1", **full_llm())          # category None
    no_llm = make_article(tmp_db, "l1", category="sports")          # llm_* all None
    empty_category = make_article(tmp_db, "c2", category="  ", **full_llm())

    incomplete = {a.id for a in list_incomplete_articles(tmp_db)}
    assert incomplete == {no_category.id, no_llm.id, empty_category.id}
    assert healthy.id not in incomplete

    # description/source/author/image_url alone do NOT make a row incomplete
    # (they cannot be repaired without fabricating content).
    sparse = make_article(tmp_db, "s1", category="science", **full_llm())
    update_article_fields(tmp_db, sparse.id, {"description": None})
    assert sparse.id not in {a.id for a in list_incomplete_articles(tmp_db)}


# ---------------------------------------------------------------------------
# repair_articles — full parse path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@respx.mock
async def test_repair_articles_parses_rows_missing_llm(tmp_db, repair_cfg):
    no_llm = make_article(tmp_db, "r1", category=None)
    make_article(tmp_db, "r2", category="science", **full_llm())  # healthy — untouched

    chat_route = respx.post(CHAT_URL).mock(
        return_value=httpx.Response(200, json={
            "answer": json.dumps({
                "title": "Story r1", "url": no_llm.url,
                "category": "Business", "summary": "s", "key_points": [],
            }),
            "provider": "groq", "model": "llama-3",
        })
    )

    result = await repair_articles(repair_cfg, tmp_db)

    assert result.checked == 1
    assert result.repaired == 1
    assert result.llm_called == 1                   # no local signal → fallback
    assert result.llm_fallback_used == 1
    assert chat_route.call_count == 1               # healthy row never called

    row = get_article_by_id(tmp_db, no_llm.id)
    assert row.llm_answer is not None
    assert row.llm_provider == "groq"
    assert row.category == "business"

    # Idempotency: a second pass finds nothing to do.
    result2 = await repair_articles(repair_cfg, tmp_db)
    assert result2.checked == 0
    assert chat_route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_repair_articles_category_from_existing_llm_no_call(tmp_db, repair_cfg):
    # Row has a full LLM result whose JSON contains a category, but the
    # category column is empty → repaired WITHOUT any LLM call.
    row = make_article(tmp_db, "e1", **full_llm())
    assert row.category is None

    chat_route = respx.post(CHAT_URL).mock(return_value=httpx.Response(500))

    result = await repair_articles(repair_cfg, tmp_db)

    assert result.checked == 1
    assert result.repaired == 1
    assert result.llm_called == 0
    assert chat_route.call_count == 0
    assert get_article_by_id(tmp_db, row.id).category == "science"


@pytest.mark.asyncio
@respx.mock
async def test_repair_articles_category_query_fallback(tmp_db, repair_cfg):
    # llm_answer exists (complete llm_*) but its JSON has no usable category
    # → one small category-only LLM query; the stored llm_answer is NOT
    # overwritten.
    answer = json.dumps({"title": "t", "summary": "s"})  # no category field
    row = make_article(tmp_db, "q1", llm_answer=answer, llm_provider="p",
                       llm_model="m", llm_processed_at="2026-10-01T00:00:00Z")

    chat_route = respx.post(CHAT_URL).mock(
        return_value=httpx.Response(200, json={"answer": "Health\n", "provider": "p", "model": "m"})
    )

    result = await repair_articles(repair_cfg, tmp_db)
    assert result.llm_fallback_used == 1
    assert result.llm_called == 1                   # one small category query
    assert chat_route.call_count == 1
    assert get_article_by_id(tmp_db, row.id).category == "health"
    # Stored LLM result preserved verbatim.
    assert get_article_by_id(tmp_db, row.id).llm_answer == answer


@pytest.mark.asyncio
@respx.mock
async def test_repair_articles_local_analysis_no_llm_call(tmp_db, repair_cfg):
    # Title carries a clear sports signal → repaired locally, zero requests.
    row = make_unsaved_article(
        "loc1", "Lakers defeat Celtics in NBA playoffs overtime thriller")
    upsert_article(tmp_db, row)

    chat_route = respx.post(CHAT_URL).mock(return_value=httpx.Response(500))

    result = await repair_articles(repair_cfg, tmp_db)

    assert result.checked == 1
    assert result.repaired == 1
    assert result.from_local == 1
    assert result.llm_fallback_used == 0
    assert chat_route.call_count == 0               # no LLM traffic at all
    assert get_article_by_id(tmp_db, row.id).category == "sports"


@pytest.mark.asyncio
@respx.mock
async def test_repair_articles_never_overwrites_valid_category(tmp_db, repair_cfg):
    # Category already valid → row is left alone even though llm_* missing.
    row = make_article(tmp_db, "v1", category="science")

    chat_route = respx.post(CHAT_URL).mock(return_value=httpx.Response(500))

    result = await repair_articles(repair_cfg, tmp_db)

    assert result.checked == 1
    assert result.left_alone == 1
    assert result.repaired == 0
    assert result.llm_called == 0
    assert chat_route.call_count == 0
    assert get_article_by_id(tmp_db, row.id).category == "science"


@pytest.mark.asyncio
@respx.mock
async def test_repair_articles_failure_leaves_row_for_next_pass(tmp_db, repair_cfg):
    row = make_article(tmp_db, "f1")
    respx.post(CHAT_URL).mock(return_value=httpx.Response(500))

    result = await repair_articles(repair_cfg, tmp_db)

    assert result.checked == 1
    assert result.failed == 1
    assert result.repaired == 0
    unchanged = get_article_by_id(tmp_db, row.id)
    assert unchanged.llm_answer is None
    assert unchanged.category is None
    # Row is still incomplete → picked up again by the next pass.
    assert [a.id for a in list_incomplete_articles(tmp_db)] == [row.id]


# ---------------------------------------------------------------------------
# Ingestion writes the category for NEW articles
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@respx.mock
async def test_run_ingestion_writes_category_for_new_articles(tmp_db):
    published = (datetime.now(timezone.utc) - timedelta(days=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    respx.get("https://newsapi.org/v2/everything").mock(
        return_value=httpx.Response(200, json={
            "status": "ok",
            "articles": [
                {"title": "Cat Story", "url": "https://example.com/cat",
                 "publishedAt": published},
            ],
        })
    )
    respx.post(CHAT_URL).mock(
        return_value=httpx.Response(200, json={
            "answer": json.dumps({
                "title": "Cat Story", "url": "https://example.com/cat",
                "category": "Sports", "summary": "s", "key_points": [],
            }),
            "provider": "groq", "model": "llama-3",
        })
    )

    cfg = Config(turso_url="file::memory:", newsapi_key="test-key", max_articles=10)
    result = await run_ingestion(cfg, tmp_db)

    assert result.parsed == 1
    assert result.categorized == 1
    articles, _ = list_articles(tmp_db, limit=10, offset=0)
    assert articles[0].category == "sports"
    assert articles[0].llm_answer is not None
