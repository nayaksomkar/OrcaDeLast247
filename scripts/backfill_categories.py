"""
scripts/backfill_categories.py — ONE-TIME category backfill for existing rows.

For every row in the configured Turso/SQLite `news` table that is missing a
usable category (or missing its LLM parse result), this script:

  1. reads the EXISTING article data,
  2. sends it through the regular LLMPing integration (full parse when the
     row has no LLM result yet; a small category query otherwise),
  3. writes ONLY the missing fields back to the SAME row
     (category via repair.py; llm_* via save_llm_parse),
  4. and finally records a "category_backfill_done" marker in the `meta`
     table so the migration is never repeated automatically — neither by
     this script nor by the CATEGORY_BACKFILL_ONCE startup hook.

Run-once guarantee: after a successful pass the marker exists and re-running
this script is a no-op (exit 0). Delete the marker from the meta table if
you ever truly need to re-run it.

Usage:
    uv run python scripts/backfill_categories.py

Prints a before/after verification summary. Never prints secrets.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timezone

sys.path.insert(0, ".")

from config import load_config
from database import (
    count_articles,
    init_db,
    list_incomplete_articles,
    meta_get,
    meta_set,
    open_db,
)
from repair import CATEGORY_MARKER_KEY, repair_articles

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("backfill")


def _category_stats(conn) -> tuple[int, int]:
    """(rows_with_category, rows_missing_category)"""
    total = count_articles(conn)
    rows = conn.execute(
        "SELECT COUNT(*) FROM news WHERE category IS NOT NULL AND TRIM(category) != ''"
    ).fetchone()
    with_cat = rows[0] if rows else 0
    return with_cat, total - with_cat


async def main() -> int:
    cfg = load_config()
    conn = open_db(cfg.turso_url, cfg.turso_token)
    init_db(conn)

    if meta_get(conn, CATEGORY_MARKER_KEY) is not None:
        with_cat, missing = _category_stats(conn)
        logger.info(
            "category backfill already done (marker present) — nothing to do. "
            "rows=%d with_category=%d missing=%d",
            count_articles(conn), with_cat, missing,
        )
        conn.close()
        return 0

    total_before = count_articles(conn)
    with_cat_before, missing_before = _category_stats(conn)
    logger.info(
        "BEFORE: total=%d with_category=%d missing_category=%d",
        total_before, with_cat_before, missing_before,
    )

    result = await repair_articles(cfg, conn)

    with_cat_after, missing_after = _category_stats(conn)
    total_after = count_articles(conn)

    if result.failed == 0:
        meta_set(conn, CATEGORY_MARKER_KEY,
                 "backfilled " + datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        logger.info("marker %r written — migration will not run again",
                    CATEGORY_MARKER_KEY)
    else:
        logger.warning(
            "%d row(s) failed — marker NOT written; re-run this script (or "
            "restart with CATEGORY_BACKFILL_ONCE=true) to retry them",
            result.failed,
        )

    logger.info(
        "AFTER:  total=%d with_category=%d missing_category=%d\n"
        "SUMMARY: %s\n"
        "CATEGORY REPAIR\n"
        "  repaired from stored LLM answer: %d\n"
        "  repaired by local analysis:      %d\n"
        "  required LLM fallback:           %d\n"
        "  still incomplete (category):     %d\n"
        "  failures:                        %d\n"
        "  left as-is (valid category):     %d\n"
        "rows before=%d after=%d (upserts only — no duplicates created)",
        total_after, with_cat_after, missing_after,
        result,
        result.from_llm_answer, result.from_local, result.llm_fallback_used,
        result.still_incomplete, result.failed, result.left_alone,
        total_before, total_after,
    )

    conn.close()
    return 0 if result.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
