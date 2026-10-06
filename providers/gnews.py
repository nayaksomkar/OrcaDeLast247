"""
providers/gnews.py — GNews provider (fallback priority #2).

Upstream API: https://gnews.io/v4/api/top-headlines
Auth        : ?token=<key>  (query param)
Docs        : https://gnews.io/docs/v4
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from providers.base import Provider, ProviderArticle, fetch_json, parse_time

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://gnews.io/v4/api/top-headlines"


class GNews(Provider):
    """
    Wraps the GNews /v4/api/top-headlines endpoint.

    base_url: Override in tests to point at a local test server.
    """

    def __init__(self, base_url: str | None = None) -> None:
        self._base_url = base_url or _DEFAULT_BASE_URL

    @property
    def name(self) -> str:
        return "gnews"

    async def fetch(
        self,
        api_key: str,
        lang: str,
        max_articles: int,
        from_time: datetime,
    ) -> list[ProviderArticle]:
        """
        Fetch articles from GNews top-headlines.

        GNews auth is via the 'token' query parameter.
        'from' restricts to articles published after from_time (ISO 8601).
        'max' controls the page size.
        """
        params = {
            "token": api_key,
            "lang": lang,
            "max": str(max_articles),
            # GNews expects RFC 3339 format for the 'from' parameter.
            "from": from_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

        data = await fetch_json(self._base_url, params=params)

        articles: list[ProviderArticle] = []
        for raw in data.get("articles", []):
            url = raw.get("url", "")
            title = raw.get("title", "")
            if not url or not title:
                continue

            # GNews nests source as {"name": "...", "url": "..."}
            source_obj = raw.get("source") or {}
            source_name = source_obj.get("name", "") if isinstance(source_obj, dict) else ""

            articles.append(
                ProviderArticle(
                    title=title,
                    url=url,
                    description=raw.get("description") or "",
                    content=raw.get("content") or "",
                    image_url=raw.get("image") or "",
                    source=source_name,
                    author=raw.get("author") or "",
                    published_at=parse_time(raw.get("publishedAt", "")),
                )
            )

        return articles
