"""
providers/webfetch.py — WebFetch provider (fallback priority #4).

WebFetch is a configurable fallback endpoint — a custom REST API that returns
articles in a Last247-native JSON format. Useful for self-hosted or private
news aggregation endpoints.

Expected response shape:
    {
        "articles": [
            {
                "title":        "...",
                "url":          "...",
                "published_at": "2026-01-01T12:00:00Z",
                "description":  "...",   // optional
                "content":      "...",   // optional
                "image_url":    "...",   // optional  (preferred)
                "image":        "...",   // optional  (fallback if image_url absent)
                "source":       "...",   // optional
                "author":       "...",   // optional
                "category":     "..."   // optional
            }
        ]
    }

Authentication is optional — if WEBFETCH_API_KEY is set, it is sent as the
'Authorization: Bearer <key>' header. The endpoint must accept GET requests.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from providers.base import Provider, ProviderArticle, fetch_json, parse_time

logger = logging.getLogger(__name__)


class WebFetch(Provider):
    """
    Wraps a configurable REST endpoint that returns articles in the
    Last247-native format.

    base_url : The full URL to GET. Set via WEBFETCH_API_URL env var.
               Must be non-empty for this provider to be eligible.

    In tests, pass base_url pointing at an httpx test server.
    """

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url

    @property
    def name(self) -> str:
        return "webfetch"

    async def fetch(
        self,
        api_key: str,
        lang: str,
        max_articles: int,
        from_time: datetime,
    ) -> list[ProviderArticle]:
        """
        GET the configured base_url and normalize the response.

        If api_key is non-empty it is sent as a Bearer token in the
        Authorization header. The lang, max_articles, and from_time args
        are forwarded as query parameters for endpoints that support them
        (ignored by endpoints that don't).

        Raises RuntimeError if base_url is empty (the ingestion loop skips
        this provider when WEBFETCH_API_URL is not configured).
        """
        if not self._base_url:
            raise RuntimeError("webfetch: WEBFETCH_API_URL is not configured")

        headers: dict[str, str] = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        # Optional query params — endpoint may ignore them.
        params: dict[str, str] = {
            "lang": lang,
            "max": str(max_articles),
            "from": from_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

        data = await fetch_json(
            self._base_url, params=params, headers=headers
        )

        articles: list[ProviderArticle] = []
        for raw in data.get("articles", []):
            url = raw.get("url", "")
            title = raw.get("title", "")
            if not url or not title:
                continue

            # Prefer 'image_url'; fall back to 'image' if 'image_url' absent.
            image_url = raw.get("image_url") or raw.get("image") or ""

            articles.append(
                ProviderArticle(
                    title=title,
                    url=url,
                    description=raw.get("description") or "",
                    content=raw.get("content") or "",
                    image_url=image_url,
                    source=raw.get("source") or "",
                    author=raw.get("author") or "",
                    category=raw.get("category") or "",
                    published_at=parse_time(raw.get("published_at", "")),
                )
            )

        return articles
