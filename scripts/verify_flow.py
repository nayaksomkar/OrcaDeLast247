"""
scripts/verify_flow.py — Comprehensive end-to-end verification script.

Tests:
1. Environment and configuration loading
2. Database connectivity and table schema initialization on a real SQLite file (news.db)
3. Data write path: upserting articles with deterministic IDs and deduplication
4. Data read path: querying articles, filtering by category/source, pagination
5. API endpoints via ASGI test client:
   - GET /health
   - GET /api/news
   - GET /api/news/{id}
   - GET /api/stats
   - POST /api/ingest
6. Retention sweep: deleting stale articles older than retention window
"""

import sys
import os
from datetime import datetime, timezone, timedelta

# Ensure root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient
from config import load_config
from database import (
    open_db, init_db, upsert_article, list_articles,
    get_article_by_id, count_articles, delete_stale_articles, article_id
)
from models import Article
import main as app_module

def run_verification():
    print("=" * 60)
    print("1. Testing Configuration Loading")
    print("=" * 60)
    os.environ["TURSO_DATABASE_URL"] = "file:./news.db"
    cfg = load_config()
    print(f"✓ Config loaded successfully: DB={cfg.turso_url}, Port={cfg.port}, Retention={cfg.retention_days} days")

    print("\n" + "=" * 60)
    print("2. Testing Database Connection & Schema Initialization")
    print("=" * 60)
    conn = open_db(cfg.turso_url, cfg.turso_token)
    init_db(conn)
    print(f"✓ Connected to {cfg.turso_url} and initialized 'news' table.")

    print("\n" + "=" * 60)
    print("3. Testing Article Upsert and Deduplication")
    print("=" * 60)
    now = datetime.now(timezone.utc)
    art1 = Article(
        id=article_id("https://example.com/tech-news-1"),
        title="AI Breakthrough in 2026",
        description="Major developments in LLM agents and microservices.",
        content="Full text of the AI breakthrough article...",
        url="https://example.com/tech-news-1",
        image_url="https://example.com/img1.jpg",
        source="TechCrunch",
        author="Jane Developer",
        category="technology",
        published_at=now,
        fetched_at=now,
        provider="manual-test",
    )
    upsert_article(conn, art1)
    print(f"✓ Inserted article 1: id={art1.id}, title='{art1.title}'")

    # Upsert with same URL (updating title)
    art1_updated = Article(
        id=article_id("https://example.com/tech-news-1"),
        title="AI Breakthrough in 2026 (Updated)",
        description="Updated description.",
        url="https://example.com/tech-news-1",
        category="technology",
        published_at=now,
        fetched_at=now,
        provider="manual-test",
    )
    upsert_article(conn, art1_updated)
    stored1 = get_article_by_id(conn, art1.id)
    assert stored1.title == "AI Breakthrough in 2026 (Updated)", "Update on conflict failed"
    print(f"✓ Verified upsert idempotency: updated title to '{stored1.title}'")

    art2 = Article(
        id=article_id("https://example.com/finance-news-1"),
        title="Global Markets Update",
        url="https://example.com/finance-news-1",
        category="finance",
        source="Bloomberg",
        published_at=now - timedelta(hours=2),
        fetched_at=now,
        provider="manual-test",
    )
    upsert_article(conn, art2)
    print(f"✓ Inserted article 2: id={art2.id}, category='finance'")

    total_count = count_articles(conn)
    print(f"✓ Total articles in database: {total_count}")

    print("\n" + "=" * 60)
    print("4. Testing FastAPI Endpoints End-to-End")
    print("=" * 60)
    # Set app globals to point to this verified DB and config
    app_module._conn = conn
    app_module._cfg = cfg

    client = TestClient(app_module.app)

    # 4.1 GET /health
    res = client.get("/health")
    assert res.status_code == 200, f"Health failed: {res.text}"
    print(f"✓ GET /health -> {res.status_code}: {res.json()}")

    # 4.2 GET /api/news
    res = client.get("/api/news?limit=10")
    assert res.status_code == 200, f"List failed: {res.text}"
    news_data = res.json()
    assert news_data["total"] >= 2
    print(f"✓ GET /api/news -> {res.status_code}: returned {len(news_data['articles'])} articles, total={news_data['total']}")

    # 4.3 GET /api/news with filter
    res = client.get("/api/news?category=technology")
    assert res.status_code == 200
    tech_data = res.json()
    assert tech_data["total"] == 1
    assert tech_data["articles"][0]["id"] == art1.id
    print(f"✓ GET /api/news?category=technology -> {res.status_code}: filtered correctly to 1 article")

    # 4.4 GET /api/news/{id}
    res = client.get(f"/api/news/{art1.id}")
    assert res.status_code == 200
    assert res.json()["id"] == art1.id
    print(f"✓ GET /api/news/{art1.id} -> {res.status_code}: retrieved '{res.json()['title']}'")

    # 4.5 GET /api/news/{invalid_id}
    res = client.get("/api/news/non_existent_id")
    assert res.status_code == 404
    print(f"✓ GET /api/news/non_existent_id -> 404: {res.json()}")

    # 4.6 GET /api/stats
    res = client.get("/api/stats")
    assert res.status_code == 200
    print(f"✓ GET /api/stats -> {res.status_code}: {res.json()}")

    # 4.7 POST /api/ingest
    res = client.post("/api/ingest")
    assert res.status_code == 200
    print(f"✓ POST /api/ingest -> {res.status_code}: {res.json()}")

    print("\n" + "=" * 60)
    print("5. Testing Retention Sweep Cleanup")
    print("=" * 60)
    # Insert an old article outside the 7-day window
    old_art = Article(
        id=article_id("https://example.com/stale-news"),
        title="Old Stale News",
        url="https://example.com/stale-news",
        published_at=now - timedelta(days=10),
        fetched_at=now,
        provider="manual-test",
    )
    upsert_article(conn, old_art)
    print(f"✓ Inserted 10-day-old article: {old_art.id}")

    deleted = delete_stale_articles(conn, retention_days=7)
    print(f"✓ Retention sweep deleted {deleted} stale article(s).")
    assert deleted == 1, "Expected exactly 1 article deleted"

    print("\n" + "=" * 60)
    print("ALL VERIFICATIONS COMPLETED SUCCESSFULLY! ✓")
    print("=" * 60)

if __name__ == "__main__":
    run_verification()
