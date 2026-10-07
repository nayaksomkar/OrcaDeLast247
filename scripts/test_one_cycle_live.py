"""
scripts/test_one_cycle_live.py — Run exactly ONE complete ingestion + LLM cycle.

Flow: providers (max MAX_ARTICLES, default 10) → normalize/upsert → per-article
LLMPing parse (sequential) → parsed answers + categories stored in Turso.

Usage:
    TURSO_DATABASE_URL=file:./news.db MAX_ARTICLES=10 python scripts/test_one_cycle_live.py

Requires at least one news provider API key and network access to the
providers and to LLMPING_BASE_URL. This performs REAL fetches and REAL
LLM parse calls (bounded by MAX_ARTICLES).
"""

import asyncio
import os
import sys

# Ensure the project root is importable when run from anywhere.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config import load_config
from database import init_db, list_articles, open_db
from ingest import run_ingestion


async def main() -> int:
    cfg = load_config()
    print(f"DB:           {cfg.turso_url}")
    print(f"LLMPing:      {cfg.llmping_base_url}{cfg.llmping_chat_path} "
          f"(timeout {cfg.llmping_timeout}s)")
    print(f"Max articles: {cfg.max_articles}")

    conn = open_db(cfg.turso_url, cfg.turso_token)
    init_db(conn)

    print("\nRunning one complete cycle (fetch → store → LLMPing parse)...")
    result = await run_ingestion(cfg, conn)

    print("\n=== One-cycle summary ===")
    print(f"Provider:     {result.provider or '(none)'}")
    print(f"Fetched:      {result.total}")
    print(f"Inserted:     {result.inserted}")
    print(f"Skipped:      {result.skipped}")
    print(f"LLM Parsed:   {result.parsed}")
    print(f"LLM Failed:   {result.parse_failed}")
    print(f"Deleted:      {result.deleted}")

    # Verify the parsed results landed in the DB.
    articles, _ = list_articles(conn, 50, 0)
    parsed_rows = [a for a in articles if a.llm_answer]
    missing = [a.url for a in articles if not a.llm_answer]
    print(f"\nDB rows with a parsed result: {len(parsed_rows)}")
    for a in parsed_rows[:3]:
        print(f"  ✓ {a.title[:60]}  ({a.llm_provider}/{a.llm_model})")
        print(f"    llm_answer: {a.llm_answer[:120]}")
    if missing:
        print(f"Rows still missing llm_answer: {len(missing)}")
        for u in missing[:10]:
            print(f"  - {u}")
    conn.close()

    # Exit non-zero when articles were fetched but none could be parsed.
    if result.inserted > 0 and result.parsed == 0:
        print("\nFAILED: articles were fetched but none were parsed by LLMPing.")
        return 1
    print("\nCycle complete. ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
