"""
providers/newsdata.py — NewsData.io provider (fallback priority #3).

Upstream API: https://newsdata.io/api/1/news
Auth        : ?apikey=<key>  (query param)
Docs        : https://newsdata.io/documentation

NewsData.io quirks handled here:
  - 'creator' field is a list of strings → joined with ", " into a single author.
  - 'category' field is a list of strings → only the first value is stored.
  - 'link' is the article URL (not 'url').
  - Status success check: data["status"] == "success" (not "ok").
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from providers.base import Provider, ProviderArticle, fetch_json, parse_time

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://newsdata.io/api/1/news"


class NewsDataIO(Provider):
    """
    Wraps the NewsData.io /api/1/news endpoint.

    base_url: Override in tests to point at a local test server.
    """

    def __init__(self, base_url: str | None = None) -> None:
        self._base_url = base_url or _DEFAULT_BASE_URL

    @property
    def name(self) -> str:
        return "newsdata"

    async def fetch(
        self,
        api_key: str,
        lang: str,
        max_articles: int,
        from_time: datetime,
    ) -> list[ProviderArticle]:
        """
        Fetch articles from NewsData.io /api/1/news.

        Auth is via the 'apikey' query parameter (note lowercase 'k').
        The 'language' param filters by language code.

        The 'from_date' parameter is PAID-plan-only on NewsData.io — the
        free tier answers HTTP 422 when it is sent (verified live). The API
        already returns the newest articles; cross-run overlap is handled by
        DB URL dedup, so no date filter is needed.
        """
        params: dict[str, str] = {
            "apikey": api_key,
            "language": lang,
        }

        data = await fetch_json(self._base_url, params=params)

        # NewsData.io signals failure via {"status": "error", "message": "..."}
        if data.get("status") != "success":
            msg = data.get("message", "unknown error")
            raise RuntimeError(f"newsdata: API error — {msg}")

        articles: list[ProviderArticle] = []
        for raw in data.get("results", []):
            url = raw.get("link", "")       # NewsData uses 'link', not 'url'
            title = raw.get("title", "")
            if not url or not title:
                continue

            # 'creator' is a list (possibly None) → join into a single string.
            creators = raw.get("creator") or []
            author = ", ".join(creators) if isinstance(creators, list) else ""

            # 'category' is a list → take the first element only.
            categories = raw.get("category") or []
            category = categories[0] if isinstance(categories, list) and categories else ""

            articles.append(
                ProviderArticle(
                    title=title,
                    url=url,
                    description=raw.get("description") or "",
                    content=raw.get("content") or "",
                    image_url=raw.get("image_url") or "",
                    source=raw.get("source_id") or "",
                    author=author,
                    category=category,
                    published_at=parse_time(raw.get("pubDate", "")),
                )
            )

        return articles
