"""
tests/test_config.py — Unit tests for config.py.
"""

import os
import pytest
from config import Config, _parse_duration_seconds, _parse_positive_int, load_config


# ---------------------------------------------------------------------------
# Helper: isolate environment from real .env / shell for each test
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Disable .env loading and remove all Last247 env vars before every config test."""
    monkeypatch.setattr("config.load_dotenv", lambda *args, **kwargs: None)
    for key in [
        "TURSO_DATABASE_URL", "TURSO_AUTH_TOKEN", "PORT",
        "INGEST_INTERVAL", "INGEST_TIMEOUT", "RETENTION_DAYS",
        "NEWS_API_KEY", "GNEWS_API_KEY", "NEWS_DATA_API_KEY",
        "WEBFETCH_API_URL", "WEBFETCH_API_KEY",
        "NEWS_LANGUAGE", "MAX_ARTICLES", "CORS_ALLOW_ORIGINS",
        "LLMPING_BASE_URL", "LLMPING_CHAT_PATH", "LLMPING_TIMEOUT",
        "LLMPING_API_TOKEN", "LLM_SYSTEM_PROMPT", "LLM_MAX_CONTENT_CHARS",
        "LLMPING_API_URL",
    ]:
        monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------------------
# load_config tests
# ---------------------------------------------------------------------------

def test_load_config_raises_when_turso_url_missing():
    with pytest.raises(ValueError, match="TURSO_DATABASE_URL"):
        load_config()


def test_load_config_applies_defaults(monkeypatch):
    monkeypatch.setenv("TURSO_DATABASE_URL", "file:./test.db")
    cfg = load_config()

    assert cfg.turso_url == "file:./test.db"
    assert cfg.turso_token == ""
    assert cfg.retention_days == 7
    assert cfg.max_articles == 7
    assert cfg.language == "en"
    assert cfg.port == "8080"
    assert cfg.ingest_interval == 28_800          # 8h — 3 runs/day
    assert cfg.ingest_timeout == 900              # covers 7 sequential LLM calls
    assert cfg.cors_origins == ["*"]


def test_load_config_llmping_defaults(monkeypatch):
    monkeypatch.setenv("TURSO_DATABASE_URL", "file:./test.db")
    cfg = load_config()

    assert cfg.llmping_base_url == "https://llmping.onrender.com"
    assert cfg.llmping_chat_path == "/chat"
    assert cfg.llmping_timeout == 60
    assert cfg.llmping_api_token == ""            # no auth header sent by default
    assert cfg.system_prompt == ""                # placeholder prompt used
    assert cfg.llm_max_content_chars == 4000


def test_load_config_env_overrides_win(monkeypatch):
    monkeypatch.setenv("TURSO_DATABASE_URL", "libsql://mydb.turso.io")
    monkeypatch.setenv("TURSO_AUTH_TOKEN", "tok123")
    monkeypatch.setenv("PORT", "9090")
    monkeypatch.setenv("RETENTION_DAYS", "14")
    monkeypatch.setenv("MAX_ARTICLES", "25")
    monkeypatch.setenv("NEWS_LANGUAGE", "fr")
    monkeypatch.setenv("INGEST_INTERVAL", "30m")
    monkeypatch.setenv("INGEST_TIMEOUT", "60s")
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "https://app.last247.dev,https://other.dev")

    cfg = load_config()

    assert cfg.turso_url == "libsql://mydb.turso.io"
    assert cfg.turso_token == "tok123"
    assert cfg.port == "9090"
    assert cfg.retention_days == 14
    assert cfg.max_articles == 25
    assert cfg.language == "fr"
    assert cfg.ingest_interval == 1800          # 30m in seconds
    assert cfg.ingest_timeout == 60
    assert cfg.cors_origins == ["https://app.last247.dev", "https://other.dev"]


def test_load_config_llmping_env_overrides_win(monkeypatch):
    monkeypatch.setenv("TURSO_DATABASE_URL", "file:./test.db")
    monkeypatch.setenv("LLMPING_BASE_URL", "http://localhost:1234")
    monkeypatch.setenv("LLMPING_CHAT_PATH", "/v1/chat")
    monkeypatch.setenv("LLMPING_API_URL", "http://override.example.com/chat")
    monkeypatch.setenv("LLMPING_TIMEOUT", "30s")
    monkeypatch.setenv("LLMPING_API_TOKEN", "secret-token")
    monkeypatch.setenv("LLM_SYSTEM_PROMPT", "You are a test prompt.")
    monkeypatch.setenv("LLM_MAX_CONTENT_CHARS", "500")

    cfg = load_config()

    assert cfg.llmping_base_url == "http://localhost:1234"
    assert cfg.llmping_chat_path == "/v1/chat"
    assert cfg.llmping_api_url == "http://override.example.com/chat"
    assert cfg.llmping_timeout == 30
    assert cfg.llmping_api_token == "secret-token"
    assert cfg.system_prompt == "You are a test prompt."
    assert cfg.llm_max_content_chars == 500


def test_load_config_ignores_invalid_numeric_values(monkeypatch):
    monkeypatch.setenv("TURSO_DATABASE_URL", "file:./test.db")
    monkeypatch.setenv("RETENTION_DAYS", "not-a-number")
    monkeypatch.setenv("MAX_ARTICLES", "-5")

    cfg = load_config()
    # Bad values → defaults stay in effect
    assert cfg.retention_days == 7
    assert cfg.max_articles == 7


def test_load_config_reads_provider_keys(monkeypatch):
    monkeypatch.setenv("TURSO_DATABASE_URL", "file:./test.db")
    monkeypatch.setenv("NEWS_API_KEY", "nk")
    monkeypatch.setenv("GNEWS_API_KEY", "gk")
    monkeypatch.setenv("NEWS_DATA_API_KEY", "ndk")
    monkeypatch.setenv("WEBFETCH_API_URL", "https://api.example.com/news")
    monkeypatch.setenv("WEBFETCH_API_KEY", "wk")

    cfg = load_config()
    assert cfg.newsapi_key == "nk"
    assert cfg.gnews_api_key == "gk"
    assert cfg.newsdata_api_key == "ndk"
    assert cfg.webfetch_api_url == "https://api.example.com/news"
    assert cfg.webfetch_api_key == "wk"


# ---------------------------------------------------------------------------
# Duration parser tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("6h",   21600),
    ("30m",  1800),
    ("120s", 120),
    ("3600", 3600),
    ("",     999),     # empty → default
    ("bad",  999),     # invalid → default
    ("0h",   0),       # parseable → 0 (only parse failures fall back)
])
def test_parse_duration_seconds(value, expected):
    assert _parse_duration_seconds(value, default=999) == expected


@pytest.mark.parametrize("value,expected", [
    ("50",  50),
    ("0",   7),    # zero → default
    ("-1",  7),    # negative → default
    ("bad", 7),    # invalid → default
    ("",    7),    # empty → default
])
def test_parse_positive_int(value, expected):
    assert _parse_positive_int(value, default=7) == expected
