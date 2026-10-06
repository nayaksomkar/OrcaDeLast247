"""
scripts/test_sample_mode_e2e.py — Live end-to-end run of SAMPLE_DATA mode.

Forces SAMPLE_DATA=true and a LOCAL throwaway database (data/sample_test.db —
never your production Turso), then runs run_ingestion() exactly as the
scheduler does. Verifies the real integration:

    data/sample_news.json → provider normalization → cap → upsert →
    real LLMPing /chat calls (one per article) → parsed results on rows →
    read back through list_articles (the API's read path)

No real news-API calls are made — only LLMPing is hit (6 requests).

Usage (from the repo root):
    uv run python scripts/test_sample_mode_e2e.py
"""

import asyncio
import logging
import os
import sys
from collections import Counter

# Force sample mode + local DB BEFORE importing config (dotenv with
# override=False never clobbers variables set here explicitly).
os.environ["SAMPLE_DATA"] = "true"
os.environ["TURSO_DATABASE_URL"] = "file:data/sample_test.db"
os.environ.pop("TURSO_AUTH_TOKEN", None)
os.environ["NEWS_API_KEY"] = ""
os.environ["GNEWS_API_KEY"] = ""
os.environ["NEWS_DATA_API_KEY"] = ""
os.environ["WEBFETCH_API_URL"] = ""

# Same basic setup main.py uses, so [SAMPLE]/[LLM]/[DB] lines are visible.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Fresh database for each run → deterministic counts.
for artifact in ("data/sample_test.db", "data/sample_test.db-journal",
                 "data/sample_test.db-shm", "data/sample_test.db-wal"):
    if os.path.exists(artifact):
        os.remove(artifact)

from config import load_config                       # noqa: E402
from database import init_db, list_articles, open_db  # noqa: E402
from ingest import run_ingestion                     # noqa: E402


async def main() -> int:
    cfg = load_config()
    print(f"Turso URL: {cfg.turso_url} (local sample DB — production untouched)")
    print(f"SAMPLE_DATA: {cfg.sample_data} | MAX_ARTICLES: {cfg.max_articles} | "
          f"INGEST_INTERVAL: {cfg.ingest_interval}s")

    if not cfg.sample_data:
        print("FAIL: SAMPLE_DATA did not load as true")
        return 1

    conn = open_db(cfg.turso_url, cfg.turso_token)
    init_db(conn)

    result = await run_ingestion(cfg, conn)

    articles, total = list_articles(conn, limit=10, offset=0)
    tally = Counter(a.provider for a in articles if a.llm_answer)
    unparsed = [a.url for a in articles if not a.llm_answer]

    print("\n=== Sample mode end-to-end report ===")
    print(f"IngestionResult : provider={result.provider} total={result.total} "
          f"inserted={result.inserted} skipped={result.skipped} "
          f"parsed={result.parsed} parse_failed={result.parse_failed}")
    print(f"DB rows         : {total}")
    print(f"Parsed per provider: {dict(tally)}")
    if unparsed:
        print(f"UNPARSED rows   : {unparsed}")

    ok = (
        result.total == 6 and result.inserted == 6 and result.parsed == 6
        and result.parse_failed == 0 and total == 6 and not unparsed
        and tally == {"newsapi": 2, "gnews": 2, "newsdata": 2}
    )
    print("SAMPLE_DATA e2e :", "PASSED ✓" if ok else "FAILED ✗")
    conn.close()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
