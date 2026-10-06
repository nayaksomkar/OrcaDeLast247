"""
scripts/test_providers_live.py — Live test of every configured news provider.

Makes ONE small real API request per configured provider (max 3 articles each)
and reports a clear PASS/FAIL/UNCONFIGURED verdict per provider:

  - key detected from environment (length only — never the key itself)
  - API request succeeds (auth accepted)
  - response parses into the normalized ProviderArticle shape
  - required fields (title, url, published_at) are present
  - failures are reported honestly (quota/plan/limits), never faked

Usage:
    uv run python scripts/test_providers_live.py
"""

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config import load_config
from providers.gnews import GNews
from providers.newsapi import NewsAPI
from providers.newsdata import NewsDataIO
from providers.webfetch import WebFetch

# Small fetch per provider — live testing should not burn quota.
_PER_PROVIDER_MAX = 3

REQUIRED_FIELDS = ("title", "url", "published_at")


def _check_articles(articles) -> tuple[bool, str]:
    """Verify every article carries the fields the DB layer requires."""
    for i, a in enumerate(articles, 1):
        for f in REQUIRED_FIELDS:
            value = getattr(a, f)
            if not value:
                return False, f"article {i} missing required field '{f}'"
    return True, f"{len(articles)} article(s), all required fields present"


async def _test_provider(name: str, provider, api_key: str, cfg) -> str:
    """Run one live fetch for a provider. Returns PASS / FAIL / SKIP verdict."""
    print(f"\n=== {name} ===")
    if not api_key:
        print("SKIP: key/url not configured in environment ("
              f"{name.upper().replace('NEWS', 'NEWS_')}) — nothing to test.")
        return "skip"

    print(f"✓ key configured (length {len(api_key)}) — value never printed")

    from_time = datetime.now(timezone.utc) - timedelta(days=cfg.retention_days)
    try:
        articles = await provider.fetch(
            api_key=api_key,
            lang=cfg.language,
            max_articles=_PER_PROVIDER_MAX,
            from_time=from_time,
        )
    except Exception as exc:
        # The message is already key-scrubbed by providers/base.fetch_json.
        print(f"FAIL: {exc}")
        return "fail"

    ok, detail = _check_articles(articles)
    if not ok:
        print(f"FAIL: {detail}")
        return "fail"

    print(f"PASS: {detail}")
    for i, a in enumerate(articles, 1):
        print(f"  [{i}] {a.title[:70]}")
        print(f"      {a.url}  ({a.published_at:%Y-%m-%dT%H:%M:%SZ})")
    return "pass"


async def main() -> int:
    cfg = load_config()
    print(f"Language: {cfg.language} | Fetching up to {_PER_PROVIDER_MAX} articles "
          f"per provider (one live request each)")

    verdicts = {}
    verdicts["newsapi"] = await _test_provider("NewsAPI", NewsAPI(), cfg.newsapi_key, cfg)
    verdicts["gnews"] = await _test_provider("GNews", GNews(), cfg.gnews_api_key, cfg)
    verdicts["newsdata"] = await _test_provider("NewsData.io", NewsDataIO(), cfg.newsdata_api_key, cfg)
    # WebFetch is a self-hosted fallback endpoint; without WEBFETCH_API_URL
    # there is nothing to call — reported as such, not as a failure.
    verdicts["webfetch"] = await _test_provider(
        "WebFetch", WebFetch(base_url=cfg.webfetch_api_url), cfg.webfetch_api_key, cfg
    )

    print("\n=== Summary ===")
    for name, v in verdicts.items():
        print(f"  {name:9s}: {v.upper()}")
    failed = [k for k, v in verdicts.items() if v == "fail"]
    print("Overall:", "FAIL" if failed else "PASS (all live-tested providers working)")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
