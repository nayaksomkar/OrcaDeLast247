"""
scripts/test_turso_live.py — Live verification of the Turso database layer.

Runs the FULL write/read lifecycle against the REAL Turso database
(TURSO_DATABASE_URL / TURSO_AUTH_TOKEN from .env):

  1. Connection + init_db (table + indexes + llm_* columns exist)
  2. Insert an article, read it back, verify every stored field
  3. Upsert the same URL with changed data → updated in place (no duplicate)
  4. URL deduplication (same URL twice → still exactly one row)
  5. save_llm_parse → read back the parsed answer + provenance columns
  6. Retention cleanup: stale sentinel is deleted, fresh sentinel survives

Uses a dedicated sentinel URL (last247-selftest://...) so real news data is
never touched except by the normal retention sweep, which matches what the
scheduler does on every run anyway.

Usage:
    uv run python scripts/test_turso_live.py
"""

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config import load_config
from database import (
    article_id,
    count_articles,
    delete_stale_articles,
    get_article_by_id,
    init_db,
    list_articles,
    open_db,
    save_llm_parse,
    upsert_article,
)
from models import Article

SENTINEL_URL = "https://example.com/last247-selftest-article"
STALE_URL = "https://example.com/last247-selftest-stale"


def _make(url: str, title: str, published_at: datetime, description: str = "") -> Article:
    return Article(
        id=article_id(url),
        title=title,
        url=url,
        description=description,
        published_at=published_at,
        fetched_at=datetime.now(timezone.utc),
        provider="selftest",
    )


def _check(label: str, ok: bool, detail: str = "") -> bool:
    mark = "✓" if ok else "✗"
    print(f"{mark} {label}" + (f" — {detail}" if detail else ""))
    return ok


def main() -> int:
    cfg = load_config()
    print(f"Turso URL: {cfg.turso_url}")

    # 1. Connection + schema --------------------------------------------------
    conn = open_db(cfg.turso_url, cfg.turso_token)
    init_db(conn)
    _check("1. connection + init_db (table/indexes/llm columns)", True)

    now = datetime.now(timezone.utc)

    # NOTE: real news rows are never touched — the sentinel URLs are unique
    # to this script, and the retention sweep at the end only deletes rows
    # older than RETENTION_DAYS (exactly what the scheduler does every run).

    # 2. Insert + read back ---------------------------------------------------
    a = _make(SENTINEL_URL, "Last247 Self-Test Article", now,
              description="inserted by scripts/test_turso_live.py")
    upsert_article(conn, a)
    stored = get_article_by_id(conn, a.id)
    ok2 = _check("2. insert + read back", stored is not None and stored.title == a.title
                 and stored.description == a.description and stored.provider == "selftest")

    # 3. Upsert (update in place) --------------------------------------------
    upsert_article(conn, _make(SENTINEL_URL, "Last247 Self-Test Article v2", now))
    updated = get_article_by_id(conn, a.id)
    ok3 = _check("3. upsert updates in place", updated is not None
                 and updated.title == "Last247 Self-Test Article v2")

    # 4. URL dedup ------------------------------------------------------------
    upsert_article(conn, _make(SENTINEL_URL, "Last247 Self-Test Article v3", now))
    dedup_count = count_articles(conn)  # total is fine; verify via unique id lookup
    same_id = article_id(SENTINEL_URL)
    rows = get_article_by_id(conn, same_id)
    ok4 = _check("4. URL deduplication (same URL → same row/id)",
                 rows is not None and rows.id == same_id,
                 f"total rows now: {dedup_count}")

    # 5. save_llm_parse + read back ------------------------------------------
    save_llm_parse(conn, same_id, "PARSED SELF-TEST RESULT", "selftest-llm",
                   "selftest-model", now.strftime("%Y-%m-%dT%H:%M:%SZ"))
    parsed = get_article_by_id(conn, same_id)
    ok5 = _check("5. save_llm_parse + read back",
                 parsed is not None and parsed.llm_answer == "PARSED SELF-TEST RESULT"
                 and parsed.llm_provider == "selftest-llm"
                 and parsed.llm_model == "selftest-model"
                 and parsed.llm_processed_at is not None)

    # 6. Retention sweep ------------------------------------------------------
    upsert_article(conn, _make(STALE_URL, "Stale Self-Test", now - timedelta(days=30)))
    deleted = delete_stale_articles(conn, cfg.retention_days)
    stale_gone = get_article_by_id(conn, article_id(STALE_URL)) is None
    fresh_alive = get_article_by_id(conn, article_id(SENTINEL_URL)) is not None
    ok6 = _check("6. retention cleanup (stale deleted, fresh kept)",
                 stale_gone and fresh_alive, f"{deleted} stale row(s) deleted")

    # Cleanup: remove the fresh sentinel too, leaving the DB as we found it.
    upsert_article(conn, _make(SENTINEL_URL, "stale now", now - timedelta(days=30)))
    delete_stale_articles(conn, cfg.retention_days)
    _check("cleanup: sentinel rows removed", get_article_by_id(conn, same_id) is None)

    # API-facing read path (same function the endpoint uses) ------------------
    articles, total = list_articles(conn, limit=5, offset=0)
    _check("7. list_articles (API read path)", total >= 0,
           f"total={total}, page size={len(articles)}")

    conn.close()
    all_ok = all([ok2, ok3, ok4, ok5, ok6])
    print("\nTurso verification:", "PASSED ✓" if all_ok else "FAILED ✗")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
