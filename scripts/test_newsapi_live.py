"""
scripts/test_newsapi_live.py — Test live connection and response from NewsAPI.
"""

import asyncio
import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config import load_config
from providers.newsapi import NewsAPI

async def test_live_newsapi():
    print("=" * 60)
    print("Testing Live NewsAPI Connection & Authentication")
    print("=" * 60)
    
    cfg = load_config()
    if not cfg.newsapi_key:
        print("FAIL: NEWS_API_KEY is missing from configuration.")
        return False
        
    print(f"✓ Configuration loaded. NEWS_API_KEY is present (length: {len(cfg.newsapi_key)} chars)")
    
    provider = NewsAPI()
    from_time = datetime.now(timezone.utc) - timedelta(days=cfg.retention_days)
    
    print(f"Connecting to {provider.name} endpoint...")
    try:
        articles = await provider.fetch(
            api_key=cfg.newsapi_key,
            lang=cfg.language,
            max_articles=5,
            from_time=from_time,
        )
        print(f"✓ Authentication successful! Received {len(articles)} articles.")
        print("-" * 60)
        for i, a in enumerate(articles[:3], 1):
            print(f"[{i}] Title: {a.title}")
            print(f"    Source: {a.source or 'N/A'}")
            print(f"    URL: {a.url}")
            print(f"    Published: {a.published_at.isoformat()}")
            print()
        print("=" * 60)
        print("NewsAPI connection test PASSED! ✓")
        print("=" * 60)
        return True
    except Exception as exc:
        print(f"FAIL: NewsAPI request failed: {exc}")
        print("=" * 60)
        return False

if __name__ == "__main__":
    success = asyncio.run(test_live_newsapi())
    sys.exit(0 if success else 1)
