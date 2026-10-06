"""
providers/base.py — Shared types and HTTP helper for all news providers.

ProviderArticle : intermediate normalized article shape produced by every
                  provider before the ingestion service converts it to Article.
Provider        : abstract base class every provider must implement.
fetch_json      : shared async HTTP GET → JSON helper used by all providers.
parse_time      : best-effort datetime parser for provider timestamp strings.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx  # async HTTP

logger = logging.getLogger(__name__)

# Default per-request HTTP timeout in seconds (connect + read).
_DEFAULT_TIMEOUT = 20.0


@dataclass
class ProviderArticle:
    """
    Normalized intermediate article shape shared by all providers.

    Providers convert their upstream JSON shapes into this dataclass.
    ingest.run_ingestion() then converts it to models.Article (adding id,
    fetched_at, and provider name) before storing it in the database.

    Only title and url are required — everything else is optional.
    """

    title: str
    url: str
    published_at: datetime
    description: str = ""
    content: str = ""
    image_url: str = ""
    source: str = ""
    author: str = ""
    category: str = ""
    # attribution override: empty for real providers (run_ingestion stamps
    # the winning provider's name); SAMPLE_DATA mode sets it per article so a
    # single mixed batch can carry newsapi/gnews/newsdata sources through the
    # exact same downstream pipeline.
    provider: str = ""


class Provider(ABC):
    """
    Contract every news source must implement.

    name   : stable lowercase identifier used in logs, API responses, and
             providerAPIKey() dispatch — e.g. "newsapi", "gnews".
    fetch  : retrieve articles from the provider.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable lowercase identifier for this provider."""
        ...

    @abstractmethod
    async def fetch(
        self,
        api_key: str,
        lang: str,
        max_articles: int,
        from_time: datetime,
    ) -> list[ProviderArticle]:
        """
        Fetch articles published after from_time.

        Args:
            api_key     : provider-specific auth key (may be "" for open endpoints).
            lang        : BCP 47 language tag, e.g. "en".
            max_articles: maximum articles to request.
            from_time   : fetch only articles published on or after this UTC time.

        Returns a (possibly empty) list of ProviderArticle.
        Raises an exception on transport errors or non-2xx responses — the
        ingestion loop catches these and moves to the next provider.
        """
        ...


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

async def fetch_json(
    url: str,
    timeout: float = _DEFAULT_TIMEOUT,
    *,
    params: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> Any:
    """
    Async HTTP GET → parsed JSON.

    Raises httpx.HTTPStatusError on non-2xx responses and
    httpx.RequestError on transport failures. The caller (each provider's
    fetch()) propagates these to the ingestion loop which logs the error and
    tries the next provider.

    A fixed User-Agent header is added because some providers reject
    requests without one.

    SECURITY: provider keys travel in the query string (apiKey/token), and
    httpx error messages restate the full URL. Every failure is re-raised as
    a RuntimeError carrying only the status/host WITHOUT the query string,
    so API keys never leak into application logs.
    """
    default_headers = {"User-Agent": "Last247-Ingestion/1.0"}
    if headers:
        default_headers.update(headers)

    # Safe identifier for error messages: scheme + host + path only —
    # never the query string, which is where API keys live for these APIs.
    safe_url = url.split("?", 1)[0]

    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            resp = await client.get(url, params=params, headers=default_headers)
            resp.raise_for_status()  # raises HTTPStatusError on 4xx/5xx
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"{safe_url} returned HTTP {exc.response.status_code}"
            ) from exc
        except httpx.RequestError as exc:
            # Wrap instead of propagating: str(exc) may embed the full URL.
            raise RuntimeError(
                f"{safe_url} request failed: {type(exc).__name__}"
            ) from exc
        return resp.json()


def parse_time(value: str) -> datetime:
    """
    Best-effort datetime parser for provider timestamp strings.

    Tries common formats used by news APIs:
      1. RFC 3339 / ISO 8601 with Z or ±HH:MM offset (most APIs)
      2. Space-separated "YYYY-MM-DD HH:MM:SS" (some APIs)

    Falls back to utcnow() on any parse failure, matching the Go behavior
    where an unparseable timestamp does not drop the article.
    """
    if not value:
        return datetime.now(timezone.utc)

    # Normalize "Z" suffix so fromisoformat() (Python ≥3.11) handles it,
    # and also handle Python < 3.11 where "Z" is not accepted.
    normalized = value.replace("Z", "+00:00")

    for fmt in (
        None,          # try fromisoformat first (handles most modern formats)
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S%z",
    ):
        try:
            if fmt is None:
                dt = datetime.fromisoformat(normalized)
            else:
                dt = datetime.strptime(value, fmt)
            # Ensure timezone-aware; assume UTC if naive.
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (ValueError, TypeError):
            continue

    logger.debug("parse_time: could not parse %r, using utcnow", value)
    return datetime.now(timezone.utc)
