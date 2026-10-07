"""
tests/conftest.py — Shared pytest fixtures.

Provides:
  - tmp_db     : a fresh in-memory SQLite connection (per test)
  - async_client : HTTPX AsyncClient wired to the FastAPI app with a test DB
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from database import init_db

# Pin the CORS origins before `main` is imported anywhere: main.py reads
# CORS_ALLOW_ORIGINS (falling back to the developer's local .env) at import
# time to configure its middleware. Tests must not depend on local .env
# contents. load_dotenv(override=False) inside main.py keeps this value.
os.environ.setdefault(
    "CORS_ALLOW_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000"
)


# ---------------------------------------------------------------------------
# Event loop — share one loop per session for async tests
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def event_loop():
    """Use a single event loop for the whole test session."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


# ---------------------------------------------------------------------------
# In-memory SQLite DB (per test, discarded after test)
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_db() -> Any:
    """
    Open a fresh in-memory SQLite DB, init the schema, yield the connection,
    then close it. Each test gets a clean, empty database.
    """
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# FastAPI test client with injected test DB
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def async_client(tmp_db):
    """
    HTTPX AsyncClient pointed at the FastAPI app, with the global _conn and
    _cfg overridden to use the test's in-memory SQLite DB and a dummy config.
    """
    import main as app_module
    from config import Config

    # Override globals so routes use test DB instead of real DB.
    original_conn = app_module._conn
    original_cfg = app_module._cfg

    app_module._conn = tmp_db
    app_module._cfg = Config(
        turso_url="file::memory:",
        retention_days=7,
        max_articles=10,
        language="en",
        port="8080",
        ingest_interval=999_999,
        ingest_timeout=30,
    )

    transport = ASGITransport(app=app_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    # Restore globals after test.
    app_module._conn = original_conn
    app_module._cfg = original_cfg
