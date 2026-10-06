"""
scripts/test_llmping_live.py — Manual staged verification of the LLMPing service.

Stage 1: plain sanity call  {"query": "Hello"} → raw response printed,
         verifies the endpoint is reachable and returns a non-empty 'answer'.
Stage 2: context-aware call — builds a real prompt (system prompt + article
         data) from one article stored in the configured database and sends
         it, verifying OrcaDeLast247's prompt construction end to end.

Usage:
    python scripts/test_llmping_live.py

Requires network access to LLMPING_BASE_URL (default https://llmping.onrender.com).
Stage 2 needs at least one article in the DB (run ingestion first if empty).
"""

import asyncio
import os
import sys

# Ensure the project root is importable when run from anywhere.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import httpx

from config import load_config
from llmping import build_article_prompt


async def main() -> int:
    cfg = load_config()
    url = cfg.llmping_base_url.rstrip("/") + cfg.llmping_chat_path
    print(f"LLMPing endpoint: {url}")

    # ------------------------------------------------------------------
    # Stage 1 — plain query
    # ------------------------------------------------------------------
    print("\n=== Stage 1: plain query ===")
    async with httpx.AsyncClient(timeout=cfg.llmping_timeout) as client:
        resp = await client.post(url, json={"query": "Hello"})
        resp.raise_for_status()
        data = resp.json()

    print(f"raw response: {data}")
    answer = data.get("answer")
    assert isinstance(answer, str) and answer.strip(), "LLMPing returned no 'answer'"
    print(f"✓ answer: {answer[:300]}")
    print(f"✓ provider/model: {data.get('provider')}/{data.get('model')}")

    # ------------------------------------------------------------------
    # Stage 2 — article-aware query (system prompt + article data)
    # ------------------------------------------------------------------
    print("\n=== Stage 2: article-aware query ===")
    from database import init_db, list_articles, open_db

    conn = open_db(cfg.turso_url, cfg.turso_token)
    init_db(conn)
    articles, total = list_articles(conn, limit=1, offset=0)
    if not articles:
        print("No articles in the DB — run an ingestion first (POST /api/ingest).")
        conn.close()
        return 0

    article = articles[0]
    prompt = build_article_prompt(cfg, article)
    print("--- prompt (first 2000 chars) ---")
    print(prompt[:2000])
    print("--- sending ---")

    async with httpx.AsyncClient(timeout=cfg.llmping_timeout) as client:
        resp = await client.post(url, json={"query": prompt})
        resp.raise_for_status()
        data = resp.json()

    print(f"provider/model: {data.get('provider')}/{data.get('model')}")
    answer = data.get("answer", "")
    print(f"answer (first 500 chars):\n{answer[:500]}")
    assert isinstance(answer, str) and answer.strip(), "LLMPing returned no 'answer'"

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
