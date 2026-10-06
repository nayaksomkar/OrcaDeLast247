"""
tests/test_llmping.py — Unit tests for llmping.py (LLMPing HTTP client + prompts).

Uses respx to mock the LLMPing /chat endpoint — no network access.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest
import respx

from config import Config
from database import article_id
from llmping import (
    DEFAULT_SYSTEM_PROMPT,
    LLMPingConnectionError,
    LLMPingHTTPError,
    LLMPingInvalidResponseError,
    LLMPingTimeoutError,
    build_article_prompt,
    chat,
    chat_url,
    resolve_system_prompt,
)
from models import Article

CHAT_URL = "https://llmping.onrender.com/chat"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cfg(**kwargs) -> Config:
    return Config(turso_url="file::memory:", **kwargs)


def _article(**kwargs) -> Article:
    url = kwargs.pop("url", "https://example.com/story")
    return Article(
        id=article_id(url),
        title=kwargs.pop("title", "Test Title"),
        url=url,
        published_at=kwargs.pop(
            "published_at", datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
        ),
        fetched_at=datetime.now(timezone.utc),
        provider=kwargs.pop("provider", "newsapi"),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Endpoint URL resolution
# ---------------------------------------------------------------------------

def test_chat_url_built_from_base_and_path_by_default():
    assert chat_url(_cfg()) == "https://llmping.onrender.com/chat"
    assert chat_url(_cfg(llmping_base_url="http://x:9/api/")) == "http://x:9/api/chat"


def test_chat_url_prefers_full_url_override():
    cfg = _cfg(llmping_api_url="https://custom.example.com/llm/chat")
    assert chat_url(cfg) == "https://custom.example.com/llm/chat"


@pytest.mark.asyncio
@respx.mock
async def test_chat_hits_full_url_override_when_set():
    cfg = _cfg(llmping_api_url="https://custom.example.com/llm/chat")
    route = respx.post("https://custom.example.com/llm/chat").mock(
        return_value=httpx.Response(200, json={"answer": "ok"})
    )

    await chat(cfg, query="Hi")

    assert route.call_count == 1


# ---------------------------------------------------------------------------
# chat() — happy path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@respx.mock
async def test_chat_posts_query_and_extracts_answer():
    route = respx.post(CHAT_URL).mock(
        return_value=httpx.Response(
            200, json={"answer": "parsed answer", "provider": "groq", "model": "llama-3"}
        )
    )

    result = await chat(_cfg(), query="Hello")

    assert result.answer == "parsed answer"
    assert result.provider == "groq"
    assert result.model == "llama-3"
    assert route.call_count == 1
    payload = json.loads(route.calls.last.request.content)
    assert payload == {"query": "Hello"}


@pytest.mark.asyncio
@respx.mock
async def test_chat_includes_session_id_when_given():
    route = respx.post(CHAT_URL).mock(
        return_value=httpx.Response(200, json={"answer": "ok"})
    )

    await chat(_cfg(), query="Hi", session_id="sess-42")

    payload = json.loads(route.calls.last.request.content)
    assert payload == {"query": "Hi", "session_id": "sess-42"}


@pytest.mark.asyncio
@respx.mock
async def test_chat_uses_custom_base_url_and_path():
    cfg = _cfg(llmping_base_url="http://localhost:9999/api/", llmping_chat_path="/chat")
    route = respx.post("http://localhost:9999/api/chat").mock(
        return_value=httpx.Response(200, json={"answer": "ok"})
    )

    await chat(cfg, query="Hi")

    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_chat_sends_auth_header_only_when_token_configured():
    route = respx.post(CHAT_URL).mock(
        return_value=httpx.Response(200, json={"answer": "ok"})
    )

    # No token → no Authorization header.
    await chat(_cfg(), query="Hi")
    assert "authorization" not in route.calls.last.request.headers

    # Token set → Bearer header sent.
    await chat(_cfg(llmping_api_token="tok"), query="Hi")
    assert route.calls.last.request.headers["authorization"] == "Bearer tok"


@pytest.mark.asyncio
@respx.mock
async def test_chat_defaults_provider_and_model_when_absent():
    respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json={"answer": "just answer"}))

    result = await chat(_cfg(), query="Hi")

    assert result.answer == "just answer"
    assert result.provider == ""
    assert result.model == ""


# ---------------------------------------------------------------------------
# chat() — failure modes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chat_rejects_empty_query():
    with pytest.raises(LLMPingInvalidResponseError):
        await chat(_cfg(), query="")


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize("status", [400, 404, 500, 503])
async def test_chat_http_error_maps_to_llmping_http_error(status):
    respx.post(CHAT_URL).mock(return_value=httpx.Response(status))

    with pytest.raises(LLMPingHTTPError) as excinfo:
        await chat(_cfg(), query="Hi")
    assert excinfo.value.status_code == status


@pytest.mark.asyncio
@respx.mock
async def test_chat_timeout_maps_to_llmping_timeout_error():
    respx.post(CHAT_URL).mock(side_effect=httpx.ReadTimeout("too slow"))

    with pytest.raises(LLMPingTimeoutError):
        await chat(_cfg(), query="Hi")


@pytest.mark.asyncio
@respx.mock
async def test_chat_connection_error_maps_to_llmping_connection_error():
    respx.post(CHAT_URL).mock(side_effect=httpx.ConnectError("refused"))

    with pytest.raises(LLMPingConnectionError):
        await chat(_cfg(), query="Hi")


@pytest.mark.asyncio
@respx.mock
async def test_chat_malformed_json_maps_to_invalid_response_error():
    respx.post(CHAT_URL).mock(
        return_value=httpx.Response(200, text="this is not json")
    )

    with pytest.raises(LLMPingInvalidResponseError):
        await chat(_cfg(), query="Hi")


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize("body", [{}, {"answer": None}, {"answer": ""}, {"answer": "   "}])
async def test_chat_missing_or_empty_answer_maps_to_invalid_response_error(body):
    respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=body))

    with pytest.raises(LLMPingInvalidResponseError):
        await chat(_cfg(), query="Hi")


# ---------------------------------------------------------------------------
# System prompt + article prompt builder
# ---------------------------------------------------------------------------

def test_resolve_system_prompt_placeholder_default():
    assert resolve_system_prompt(_cfg()) == DEFAULT_SYSTEM_PROMPT


def test_resolve_system_prompt_prefers_configured():
    cfg = _cfg(system_prompt="CUSTOM EDITORIAL PROMPT")
    assert resolve_system_prompt(cfg) == "CUSTOM EDITORIAL PROMPT"


def test_build_article_prompt_keeps_sections_separate():
    prompt = build_article_prompt(_cfg(), _article(description="Desc text", content="Body text"))

    # Section order: instructions first, article data after, never interleaved.
    assert prompt.index("=== SYSTEM INSTRUCTIONS ===") < prompt.index("=== ARTICLE DATA ===")
    assert DEFAULT_SYSTEM_PROMPT in prompt
    assert "TITLE: Test Title" in prompt
    assert "URL: https://example.com/story" in prompt
    assert "DESCRIPTION: Desc text" in prompt
    assert "CONTENT: Body text" in prompt
    assert "PUBLISHED_AT: 2026-10-01T12:00:00Z" in prompt


def test_build_article_prompt_uses_configured_prompt():
    cfg = _cfg(system_prompt="CUSTOM EDITORIAL PROMPT")
    prompt = build_article_prompt(cfg, _article())

    assert "CUSTOM EDITORIAL PROMPT" in prompt
    assert DEFAULT_SYSTEM_PROMPT not in prompt


def test_build_article_prompt_truncates_long_content():
    cfg = _cfg(llm_max_content_chars=100)
    article = _article(content="x" * 5000, description="y" * 5000)
    prompt = build_article_prompt(cfg, article)

    assert "…" in prompt
    assert prompt.count("x" * 100) == 1          # truncated to exactly 100 chars
    assert "x" * 101 not in prompt
    assert len(prompt) < 2000


def test_build_article_prompt_omits_empty_fields():
    prompt = build_article_prompt(_cfg(), _article())

    assert "DESCRIPTION:" not in prompt
    assert "CONTENT:" not in prompt
    assert "SOURCE:" not in prompt
    assert "AUTHOR:" not in prompt
    assert "CATEGORY:" not in prompt
