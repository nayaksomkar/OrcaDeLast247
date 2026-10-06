"""
llmping.py — Client for the LLMPing LLM Brain service (HTTP only).

OrcaDeLast247 never runs a model itself: all inference happens in the external
LLMPing service. This module is the only place in the project that talks to it.

Service contract
----------------
Endpoint : POST {LLMPING_BASE_URL}{LLMPING_CHAT_PATH}   (default /chat)
Request  : {"query": "..."}               ("session_id" optional, passthrough)
Response : {"answer": "...", "provider": "...", "model": "..."}

Provider selection, model selection, and provider fallback all happen inside
LLMPing — they are deliberately NOT implemented here.

Public API
----------
DEFAULT_SYSTEM_PROMPT  placeholder system instructions (used when
                       LLM_SYSTEM_PROMPT env var is empty; the real editorial
                       prompt is supplied via configuration, never hardcoded)
resolve_system_prompt(cfg)          → the active system instructions
build_article_prompt(cfg, article)  → system prompt + article data as one query
chat_url(cfg)                       → full /chat URL (LLMPING_API_URL wins)
chat(cfg, query, session_id=None)   → LLMChatResult(answer, provider, model)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:  # avoid an import cycle; config never imports llmping at runtime
    from config import Config

from models import Article

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

# PLACEHOLDER — override via LLM_SYSTEM_PROMPT env var with the real editorial
# prompt. Never hardcoded elsewhere in the codebase.
DEFAULT_SYSTEM_PROMPT = (
    "You are the Last247 news parser. Parse the article provided in the "
    "ARTICLE DATA section into clean structured news data. Use ONLY the "
    "supplied article fields — do not invent facts, do not add outside "
    "knowledge. Reply with the parsed result only."
)


def chat_url(cfg: "Config") -> str:
    """
    Full URL of the LLMPing /chat endpoint.

    LLMPING_API_URL (full URL) wins when set; otherwise built from
    LLMPING_BASE_URL + LLMPING_CHAT_PATH (both with sane defaults).
    """
    if cfg.llmping_api_url:
        return cfg.llmping_api_url
    return cfg.llmping_base_url.rstrip("/") + cfg.llmping_chat_path


def resolve_system_prompt(cfg: "Config") -> str:
    """Return the configured system instructions, or the placeholder default."""
    return cfg.system_prompt or DEFAULT_SYSTEM_PROMPT


def _truncate(text: str, limit: int) -> str:
    """Trim text to `limit` characters (ellipsis marks a hard cut)."""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def build_article_prompt(cfg: "Config", article: Article) -> str:
    """
    Build the query payload sent to LLMPing /chat for one article.

    The system instructions and the article data live in separate delimited
    sections so the instruction layer is never mixed into the article content.
    Long description/content fields are truncated to bound prompt size.
    """
    pub = (
        article.published_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        if article.published_at
        else ""
    )
    fields = [
        ("TITLE", article.title),
        ("URL", article.url),
        ("SOURCE", article.source),
        ("AUTHOR", article.author),
        ("CATEGORY", article.category),
        ("PUBLISHED_AT", pub),
        ("DESCRIPTION", article.description),
        ("CONTENT", article.content),
    ]

    lines = []
    for label, value in fields:
        if value:
            lines.append(f"{label}: {_truncate(str(value), cfg.llm_max_content_chars)}")

    return (
        "=== SYSTEM INSTRUCTIONS ===\n"
        f"{resolve_system_prompt(cfg)}\n\n"
        "=== ARTICLE DATA ===\n"
        + "\n".join(lines)
    )


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class LLMPingError(Exception):
    """Base class for all LLMPing client failures."""


class LLMPingConnectionError(LLMPingError):
    """Could not establish a connection to LLMPing (DNS/refused/network)."""


class LLMPingTimeoutError(LLMPingError):
    """The request to LLMPing exceeded the configured timeout."""


class LLMPingHTTPError(LLMPingError):
    """LLMPing answered with a non-2xx HTTP status."""

    def __init__(self, message: str, status_code: int = 0) -> None:
        super().__init__(message)
        self.status_code = status_code


class LLMPingInvalidResponseError(LLMPingError):
    """LLMPing answered with malformed JSON, no 'answer', or an empty 'answer'."""


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass
class LLMChatResult:
    """
    Parsed LLMPing /chat response.

    answer          : the LLM's reply, stored as received — this is the parsed
                      article data persisted to the `news` row.
    provider/model  : which LLM provider/model LLMPing used (useful metadata;
                      LLMPing owns these choices, we only preserve them).
    """

    answer: str
    provider: str = ""
    model: str = ""


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

async def chat(cfg: "Config", query: str, session_id: str | None = None) -> LLMChatResult:
    """
    Send one query to LLMPing POST /chat.

    Raises LLMPingConnectionError / LLMPingTimeoutError / LLMPingHTTPError /
    LLMPingInvalidResponseError on every failure mode — the caller decides how
    to surface it (the ingestion loop skips the article, keeps its row, and
    moves on to the next one).

    No auth header is sent unless cfg.llmping_api_token is configured.
    """
    if not query or not query.strip():
        raise LLMPingInvalidResponseError("query must be a non-empty string")

    payload: dict[str, str] = {"query": query}
    if session_id:
        payload["session_id"] = session_id

    headers = {"Content-Type": "application/json", "User-Agent": "Last247/1.0"}
    if cfg.llmping_api_token:
        headers["Authorization"] = f"Bearer {cfg.llmping_api_token}"

    url = chat_url(cfg)

    try:
        async with httpx.AsyncClient(timeout=cfg.llmping_timeout) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
    except httpx.TimeoutException as exc:
        raise LLMPingTimeoutError(
            f"LLMPing did not respond within {cfg.llmping_timeout}s: {exc}"
        ) from exc
    except httpx.HTTPStatusError as exc:
        raise LLMPingHTTPError(
            f"LLMPing returned HTTP {exc.response.status_code}",
            status_code=exc.response.status_code,
        ) from exc
    except httpx.RequestError as exc:
        raise LLMPingConnectionError(f"LLMPing unreachable at {url}: {exc}") from exc

    try:
        data = resp.json()
    except ValueError as exc:
        raise LLMPingInvalidResponseError(f"LLMPing returned malformed JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise LLMPingInvalidResponseError(
            f"LLMPing response must be a JSON object, got {type(data).__name__}"
        )

    answer = data.get("answer")
    if not isinstance(answer, str) or not answer.strip():
        raise LLMPingInvalidResponseError("LLMPing response has no non-empty 'answer'")

    return LLMChatResult(
        answer=answer,
        provider=data.get("provider") or "",
        model=data.get("model") or "",
    )
