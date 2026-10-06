"""
tests/test_sample_mode.py — SAMPLE_DATA=true pipeline tests.

Verifies the bundled sample articles flow through the EXACT same pipeline as
real provider data:

    sample_news.json → ProviderArticle normalization → MAX_ARTICLES cap →
    dedup → upsert (Turso) → LLMPing parse (respx-mocked /chat) →
    save_llm_parse → read back

The LLMPing /chat endpoint is mocked with respx — the project's established
mechanism for integration tests without real network runs (real LLMPing is
exercised separately by scripts/test_sample_mode_e2e.py).
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from pathlib import Path

import httpx
import pytest
import respx

import sample_data
from config import Config
from database import list_articles
from ingest import run_ingestion
from llmping import DEFAULT_SYSTEM_PROMPT
from sample_data import load_sample_articles

CHAT_URL = "https://llmping.onrender.com/chat"
SAMPLE_FILE = Path(__file__).resolve().parent.parent / "data" / "sample_news.json"


def mock_llm() -> "respx.Route":
    """
    Standard LLMPing /chat mock returning the documented response shape.
    Must be called inside a @respx.mock test so the route attaches to the
    test's active router. Returns the route (for call-count assertions).
    """
    route = respx.post(CHAT_URL).mock(
        return_value=httpx.Response(
            200,
            json={"answer": "PARSED SAMPLE", "provider": "google_genai",
                  "model": "gemini-2.5-flash"},
        )
    )
    return route


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def test_sample_file_exists_and_is_valid_json():
    data = json.loads(SAMPLE_FILE.read_text(encoding="utf-8"))
    assert set(data.keys()) >= {"newsapi", "gnews", "newsdata"}
    for group in ("newsapi", "gnews", "newsdata"):
        assert len(data[group]) >= 2, f"{group} needs >= 2 sample articles"


def test_load_sample_articles_tags_provider_and_counts():
    articles = load_sample_articles()

    assert len(articles) == 6
    tally = Counter(a.provider for a in articles)
    assert tally == {"newsapi": 2, "gnews": 2, "newsdata": 2}
    assert len({a.url for a in articles}) == 6          # all URLs unique
    assert all(a.title for a in articles)
    assert all(a.published_at.tzinfo is not None for a in articles)


def test_load_sample_articles_missing_file_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(sample_data, "DEFAULT_SAMPLE_PATH", str(tmp_path / "none.json"))
    with pytest.raises(FileNotFoundError):
        load_sample_articles()


def test_load_sample_articles_reports_invalid_entries(tmp_path, monkeypatch, caplog):
    """A dirty entry is reported as an error log — never silently dropped."""
    data = {
        "newsapi": [
            {"title": "OK", "url": "https://example.com/ok-1", "published_at": "2026-10-06T00:00:00Z"},
        ],
        "gnews": [
            {"title": "", "url": "https://example.com/no-title"},      # no title → invalid
        ],
        "newsdata": [
            {"title": "No URL", "url": "", "published_at": "2026-10-06T00:00:00Z"},  # no url → invalid
        ],
    }
    p = tmp_path / "sample.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(sample_data, "DEFAULT_SAMPLE_PATH", str(p))

    with caplog.at_level(logging.ERROR, logger="sample_data"):
        articles = load_sample_articles()

    assert len(articles) == 1                    # only the valid one survives
    assert articles[0].provider == "newsapi"
    refused = [r for r in caplog.records if "gnews" in r.getMessage() or "newsdata" in r.getMessage()]
    assert len(refused) == 2                     # both invalid entries were reported


# ---------------------------------------------------------------------------
# Full pipeline e2e (LLMPing mocked with respx — project's standard mechanism)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@respx.mock
async def test_run_ingestion_sample_mode_full_pipeline(tmp_db):
    """6 sample articles: loaded → capped → upserted → parsed → retrievable."""
    mock_llm()

    # No provider keys at all — proves the sample path never touches providers.
    cfg = Config(turso_url="file::memory:", sample_data=True)

    result = await run_ingestion(cfg, tmp_db)

    assert result.provider == "sample"
    assert result.total == 6
    assert result.inserted == 6
    assert result.skipped == 0
    assert result.parsed == 6
    assert result.parse_failed == 0

    # Read back through the same function the API uses.
    articles, total = list_articles(tmp_db, limit=10, offset=0)
    assert total == 6
    tally = Counter(a.provider for a in articles)
    assert tally == {"newsapi": 2, "gnews": 2, "newsdata": 2}
    # Every article went through the LLM and carries the parsed result.
    for a in articles:
        assert a.llm_answer == "PARSED SAMPLE"
        assert a.llm_provider == "google_genai"
        assert a.llm_model == "gemini-2.5-flash"
        assert a.llm_processed_at is not None


@pytest.mark.asyncio
@respx.mock
async def test_run_ingestion_sample_mode_respects_max_articles(tmp_db):
    """MAX_ARTICLES stays the shared cap: 3 → only 3 of the 6 samples processed."""
    chat_route = mock_llm()

    cfg = Config(turso_url="file::memory:", sample_data=True, max_articles=3)
    result = await run_ingestion(cfg, tmp_db)

    assert result.inserted == 3
    assert result.parsed == 3
    assert chat_route.call_count == 3            # only the first three (newsapi group)
    articles, _ = list_articles(tmp_db, limit=10, offset=0)
    assert Counter(a.provider for a in articles) == {"newsapi": 2, "gnews": 1}


@pytest.mark.asyncio
@respx.mock
async def test_run_ingestion_sample_mode_llm_failure_keeps_row(tmp_db, monkeypatch):
    """One LLMPing failure must not lose the run or the row (llm_answer=None)."""
    mock_llm()

    # Make the LLM call fail every time — simplest queue: chat raises on 500.
    respx.post(CHAT_URL).mock(return_value=httpx.Response(500))

    cfg = Config(turso_url="file::memory:", sample_data=True)
    result = await run_ingestion(cfg, tmp_db)

    assert result.inserted == 6
    assert result.parsed == 0
    assert result.parse_failed == 6              # reported, not silent
    articles, total = list_articles(tmp_db, limit=10, offset=0)
    assert total == 6                            # rows still stored
    assert all(a.llm_answer is None for a in articles)


@pytest.mark.asyncio
@respx.mock
async def test_sample_mode_logs_requested_lines(tmp_db, caplog):
    """[SAMPLE]/[LLM]/[DB] progress lines + the final summary must be emitted."""
    mock_llm()

    cfg = Config(turso_url="file::memory:", sample_data=True)
    with caplog.at_level(logging.INFO):
        await run_ingestion(cfg, tmp_db)

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "[SAMPLE] Loading sample news" in text
    assert "[SAMPLE] NewsAPI: 2 article(s)" in text
    assert "[SAMPLE] GNews: 2 article(s)" in text
    assert "[SAMPLE] NewsData.io: 2 article(s)" in text
    assert "[LLM] Processing article 1/6" in text      # progress counter
    assert "[LLM] Processing article 6/6" in text
    assert "[DB] Stored article:" in text
    assert "Articles loaded: 6" in text                # completion summary
    assert "LLM processed: 6" in text
    assert "Database stored: 6" in text
    assert "Failed: 0" in text


@pytest.mark.asyncio
@respx.mock
async def test_sample_mode_prompt_includes_article_data(tmp_db):
    """The LLM query must carry the system prompt and the article context."""
    route = mock_llm()

    cfg = Config(turso_url="file::memory:", sample_data=True)
    await run_ingestion(cfg, tmp_db)

    bodies = [json.loads(c.request.content.decode()) for c in route.calls]
    # respx call count may be lower than 6? No — exactly 6 posts expected.
    assert len(bodies) == 6
    for body in bodies:
        assert DEFAULT_SYSTEM_PROMPT.splitlines()[0] in body["query"]
        assert "ARTICLE DATA" in body["query"]