"""
providers/newsapi.py — NewsAPI provider (fallback priority #1).

Upstream API: https://newsapi.org/v2/everything
Auth        : ?apiKey=<key>  (query param)
Docs        : https://newsapi.org/docs/endpoints/everything

The provider fetches the most recent English headlines, sorted newest-first,
filtered by the configured from_time date.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from providers.base import Provider, ProviderArticle, fetch_json, parse_time

logger = logging.getLogger(__name__)

# Production endpoint — overridden in tests via the base_url kwarg.
_DEFAULT_BASE_URL = "https://newsapi.org/v2/everything"


class NewsAPI(Provider):
    """
    Wraps the NewsAPI /v2/everything endpoint.

    base_url: Override in tests to point at a local httptest server.
              In production leave as None to use the real NewsAPI endpoint.
    """

    def __init__(self, base_url: str | None = None) -> None:
        self._base_url = base_url or _DEFAULT_BASE_URL

    @property
    def name(self) -> str:
        return "newsapi"

    async def fetch(
        self,
        api_key: str,
        lang: str,
        max_articles: int,
        from_time: datetime,
    ) -> list[ProviderArticle]:
        """
        Fetch articles from NewsAPI /v2/everything.

        NewsAPI auth is via the 'apiKey' query parameter.
        The 'from' param limits results to articles published after from_time.
        Results are sorted 'publishedAt' descending (newest first).

        Raises on HTTP error or non-"ok" status field in the response body.
        """
        params = {
            "apiKey": api_key,
            "language": lang,
            "pageSize": str(max_articles),
            "sortBy": "publishedAt",
            # ISO 8601 date string — NewsAPI accepts "YYYY-MM-DD"
            "from": from_time.strftime("%Y-%m-%d"),
            # Broad query so we get general top news (not topic-specific).
            "q": "news",
        }

        data = await fetch_json(self._base_url, params=params)

        # NewsAPI signals failure via {"status": "error", "message": "..."}
        if data.get("status") != "ok":
            msg = data.get("message", "unknown error")
            raise RuntimeError(f"newsapi: API error — {msg}")

        articles: list[ProviderArticle] = []
        for raw in data.get("articles", []):
            url = raw.get("url", "")
            title = raw.get("title", "")
            if not url or not title:
                continue  # drop articles without a URL or title

            # NewsAPI nests source as {"id": "...", "name": "..."}
            source_obj = raw.get("source") or {}
            source_name = source_obj.get("name", "") if isinstance(source_obj, dict) else ""

            articles.append(
                ProviderArticle(
                    title=title,
                    url=url,
                    description=raw.get("description") or "",
                    content=raw.get("content") or "",
                    image_url=raw.get("urlToImage") or "",
                    source=source_name,
                    author=raw.get("author") or "",
                    published_at=parse_time(raw.get("publishedAt", "")),
                )
            )

        return articles
