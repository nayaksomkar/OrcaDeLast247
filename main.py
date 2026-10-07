"""
main.py — Last247 FastAPI HTTP server.

Endpoints
---------
GET  /health            — liveness probe
GET  /api/news          — paginated article list (limit, offset, category, source)
GET  /api/news/{id}     — single article by deterministic ID
POST /api/ingest        — trigger a manual ingestion run
GET  /api/stats         — total article count + last ingestion summary

The server also runs background ingestion on startup and every INGEST_INTERVAL
seconds (default 2 h = 12 runs/day) using an asyncio background task, plus a
NULL/empty repair check every NULL_CHECK_INTERVAL (default 2 h). Each
ingestion run: collect up to MAX_ARTICLES (10) usable articles → store → send
each article + the system prompt to the LLMPing LLM Brain → store the parsed
answer and its category on the row.

Run locally:
    python -m uvicorn main:app --host 0.0.0.0 --port 8080 --reload
or:
    python main.py
"""

from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from config import Config, load_config
from database import (
    count_articles,
    get_article_by_id,
    init_db,
    list_articles,
    list_categories,
    meta_get,
    meta_set,
    open_db,
)
from ingest import run_ingestion
from models import Article, IngestionResult
from repair import CATEGORY_MARKER_KEY, repair_articles

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
    datefmt="%Y/%m/%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("last247")

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

# Loaded once at startup; never modified after that.
_cfg: Optional[Config] = None
# Open database connection shared across all requests.
_conn: Optional[Any] = None
# Result of the most-recent ingestion run (read by GET /api/stats).
_last_result: Optional[IngestionResult] = None


# ---------------------------------------------------------------------------
# Background ingestion loop
# ---------------------------------------------------------------------------

async def _ingestion_loop() -> None:
    """
    Run ingestion immediately on startup, then repeat every cfg.ingest_interval
    seconds. Cancelled automatically when the server shuts down.
    """
    assert _cfg is not None
    assert _conn is not None
    global _last_result

    logger.info("[ingest] starting initial ingestion run...")
    try:
        result = await asyncio.wait_for(
            run_ingestion(_cfg, _conn), timeout=_cfg.ingest_timeout
        )
        _last_result = result
        _log_ingestion_result(result)
    except asyncio.TimeoutError:
        logger.warning("[ingest] initial run timed out after %ds", _cfg.ingest_timeout)
    except Exception as exc:
        logger.error("[ingest] initial run failed: %s", exc)

    while True:
        await asyncio.sleep(_cfg.ingest_interval)
        logger.info("[ingest] starting scheduled ingestion run...")
        try:
            result = await asyncio.wait_for(
                run_ingestion(_cfg, _conn), timeout=_cfg.ingest_timeout
            )
            _last_result = result
            _log_ingestion_result(result)
        except asyncio.TimeoutError:
            logger.warning("[ingest] scheduled run timed out after %ds", _cfg.ingest_timeout)
        except Exception as exc:
            logger.error("[ingest] scheduled run failed: %s", exc)


def _log_ingestion_result(r: IngestionResult) -> None:
    logger.info(
        "=== Ingestion Summary ===\n"
        "Provider:   %s\n"
        "Fetched:    %d articles\n"
        "Inserted:   %d articles\n"
        "Skipped:    %d articles\n"
        "Deleted:    %d stale articles\n"
        "LLM Parsed: %d articles\n"
        "LLM Failed: %d articles\n"
        "Categorized:%d articles\n"
        "SourceTime: %s\n"
        "=========================",
        r.provider, r.total, r.inserted, r.skipped, r.deleted,
        r.parsed, r.parse_failed, r.categorized, r.source_time,
    )


# ---------------------------------------------------------------------------
# NULL/empty repair + one-time category backfill
# ---------------------------------------------------------------------------

async def _maybe_category_backfill() -> None:
    """
    One-time category backfill over EXISTING rows, gated by the
    CATEGORY_BACKFILL_ONCE flag and the DB meta marker.

    The marker ("category_backfill_done") is written only after a fully
    successful pass, so a failed run is retried on the next startup —
    and a successful one is never repeated.
    """
    assert _cfg is not None
    assert _conn is not None

    if not _cfg.category_backfill_once:
        return
    if await asyncio.to_thread(meta_get, _conn, CATEGORY_MARKER_KEY) is not None:
        logger.info("[backfill] marker present — one-time category backfill skipped")
        return

    logger.info("[backfill] one-time category backfill over existing rows...")
    try:
        result = await asyncio.wait_for(
            repair_articles(_cfg, _conn), timeout=_cfg.ingest_timeout
        )
    except asyncio.TimeoutError:
        logger.warning(
            "[backfill] timed out after %ds — will retry on next startup",
            _cfg.ingest_timeout,
        )
        return
    except Exception as exc:
        logger.error("[backfill] failed: %s — will retry on next startup", exc)
        return

    if result.failed == 0:
        await asyncio.to_thread(
            meta_set, _conn, CATEGORY_MARKER_KEY,
            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        logger.info("[backfill] complete, marker written: %s", result)
    else:
        logger.warning(
            "[backfill] %d row(s) failed — marker NOT written, retried next startup",
            result.failed,
        )


async def _repair_loop() -> None:
    """
    Every cfg.null_check_interval seconds (default 2h): scan Turso for rows
    with NULL/empty important fields and repair them. Only incomplete rows
    are fetched, so healthy articles are never reprocessed; a failed repair
    is simply retried on the next pass. Sleeps FIRST — the startup backfill
    (above) and the initial ingestion cover the startup state.
    """
    assert _cfg is not None
    assert _conn is not None

    while True:
        await asyncio.sleep(_cfg.null_check_interval)
        logger.info("[repair] starting scheduled NULL/empty check...")
        try:
            await asyncio.wait_for(
                repair_articles(_cfg, _conn), timeout=_cfg.ingest_timeout
            )
        except asyncio.TimeoutError:
            logger.warning(
                "[repair] scheduled pass timed out after %ds", _cfg.ingest_timeout
            )
        except Exception as exc:
            logger.error("[repair] scheduled pass failed: %s", exc)


# ---------------------------------------------------------------------------
# App lifecycle (startup / shutdown)
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI lifespan context manager — replaces deprecated on_event handlers.

    Startup:  load config → open DB → init schema → start background ingestion.
    Shutdown: cancel background task gracefully.
    """
    global _cfg, _conn

    # --- Startup ---
    try:
        _cfg = load_config()
    except ValueError as exc:
        logger.critical("config error: %s", exc)
        raise SystemExit(1) from exc

    try:
        _conn = open_db(_cfg.turso_url, _cfg.turso_token)
        init_db(_conn)
    except Exception as exc:
        logger.critical("database error: %s", exc)
        raise SystemExit(1) from exc

    logger.info("server starting on 0.0.0.0:%s", _cfg.port)

    # One-time category backfill over EXISTING rows (flag + marker gated).
    await _maybe_category_backfill()

    # Start background ingestion and the 2h NULL/empty repair check as
    # fire-and-forget tasks.
    task = asyncio.create_task(_ingestion_loop())
    repair_task = asyncio.create_task(_repair_loop())

    yield  # server is running

    # --- Shutdown ---
    logger.info("shutting down server...")
    task.cancel()
    repair_task.cancel()
    for t in (task, repair_task):
        try:
            await t
        except asyncio.CancelledError:
            pass
    if _conn:
        try:
            _conn.close()
        except Exception:
            pass
    logger.info("server shut down cleanly")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Last247 News API",
    description="News ingestion and read API backed by Turso/SQLite.",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS middleware is configured from the CORS_ALLOW_ORIGINS env var
# (comma-separated list, e.g. "http://localhost:3000,http://127.0.0.1:3000").
# The kwargs are built by a helper so tests can exercise the exact same
# middleware configuration the app uses.
import os as _os
from dotenv import load_dotenv as _load_dotenv
_load_dotenv(".env", override=False)


def cors_middleware_kwargs() -> dict:
    """Build the CORSMiddleware kwargs from CORS_ALLOW_ORIGINS.

    Comma-separated origins are parsed into a list; empty/unset falls back to
    ["*"] (allow all). No credentials — the API is public and cookie-free.
    """
    raw = _os.getenv("CORS_ALLOW_ORIGINS", "*").strip()
    origins = [o.strip() for o in raw.split(",") if o.strip()] or ["*"]
    return {
        "allow_origins": origins,
        "allow_methods": ["GET", "POST", "OPTIONS"],
        "allow_headers": ["Content-Type"],
        "allow_credentials": False,
    }


app.add_middleware(CORSMiddleware, **cors_middleware_kwargs())

# ---------------------------------------------------------------------------
# Request logging middleware
# ---------------------------------------------------------------------------

@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Log every request with method, path, status code, and duration."""
    import time
    start = time.perf_counter()
    response = await call_next(request)
    duration_ms = (time.perf_counter() - start) * 1000
    logger.info(
        "%s %s %d %.1fms",
        request.method, request.url.path, response.status_code, duration_ms,
    )
    return response


# ---------------------------------------------------------------------------
# Helper: consistent error response
# ---------------------------------------------------------------------------

def _error(status: int, message: str, code: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": message, "code": code},
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health", tags=["meta"])
async def health_check():
    """
    Liveness probe. Returns 200 when the HTTP server is running.

    Does NOT query the database — a 200 response means the process is alive,
    not that the DB is reachable. Use GET /api/stats to verify DB connectivity.
    """
    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


@app.get("/api/news", response_model=None, tags=["articles"])
async def list_articles_endpoint(
    limit: int = Query(default=50, ge=1, le=100, description="Max articles per page (1–100)."),
    offset: int = Query(default=0, ge=0, description="Rows to skip for pagination."),
    category: str = Query(default="", description="Filter by category (case-insensitive exact match)."),
    source: str = Query(default="", description="Filter by exact source/publisher."),
):
    """
    Paginated list of articles, newest first (stable order: published_at
    DESC, id ASC).

    Pagination and filtering happen entirely in the Turso SQL query — the
    backend only retrieves the requested page (plus one sentinel row).
    `has_more` is derived from that sentinel row (limit + 1), so no extra
    query is needed to detect the end of results.

    Optional filters: `category` (case-insensitive exact match) and `source`
    (exact match, AND-combined).
    `total` (count of matching rows before pagination) is kept for backward
    compatibility with existing UI paging; `has_more` is the preferred
    signal going forward.
    Always returns 200 — `articles` is [] when nothing matches.
    """
    try:
        # Fetch one extra row so has_more is known without a second query.
        fetched, total = await asyncio.to_thread(
            list_articles, _conn, limit + 1, offset, category, source
        )
        has_more = len(fetched) > limit
        articles = fetched[:limit]
    except Exception as exc:
        logger.error("list_articles failed: %s", exc)
        return _error(500, "failed to fetch articles", "INTERNAL_ERROR")

    return {
        "articles": [a.model_dump(exclude_none=True) for a in articles],
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": has_more,
    }


@app.get("/api/news/categories", response_model=None, tags=["articles"])
async def list_categories_endpoint():
    """
    Unique categories currently present in the news table, for filter UIs.

    Built with one SQL query (DISTINCT + NULL/empty filter + ordering in the
    database) — cheap, static, and never triggers ingestion or the LLM.
    """
    try:
        categories = await asyncio.to_thread(list_categories, _conn)
    except Exception as exc:
        logger.error("list_categories failed: %s", exc)
        return _error(500, "failed to fetch categories", "INTERNAL_ERROR")

    return {"categories": categories}


@app.get("/api/news/{article_id}", response_model=None, tags=["articles"])
async def get_article_endpoint(article_id: str):
    """
    Retrieve a single article by its deterministic 16-char hex ID.

    Returns 404 when the ID does not exist in the database.
    """
    try:
        article = await asyncio.to_thread(get_article_by_id, _conn, article_id)
    except Exception as exc:
        logger.error("get_article_by_id failed: %s", exc)
        return _error(500, "failed to fetch article", "INTERNAL_ERROR")

    if article is None:
        return _error(404, f'article with id "{article_id}" not found', "NOT_FOUND")

    return article.model_dump(exclude_none=True)


@app.get("/api/stats", tags=["meta"])
async def stats_endpoint():
    """
    Return the total article count and the last ingestion run summary.

    `last_ingestion` is absent if no ingestion run has completed yet.
    Returns 500 if the database count query fails.
    """
    try:
        total = await asyncio.to_thread(count_articles, _conn)
    except Exception as exc:
        logger.error("count_articles failed: %s", exc)
        return _error(500, "failed to get stats", "INTERNAL_ERROR")

    resp: dict[str, Any] = {"total_articles": total}
    if _last_result is not None:
        resp["last_ingestion"] = _last_result.to_dict()

    return resp


@app.post("/api/ingest", tags=["meta"])
async def trigger_ingest():
    """
    Trigger a manual ingestion run synchronously.

    The run is bounded by cfg.ingest_timeout (default 900 s) so it can cover
    the provider fetch plus up to 10 sequential LLMPing parse calls.
    Returns the IngestionResult when done.
    Returns 500 if ingestion itself errors (e.g., DB unreachable).
    """
    global _last_result

    try:
        result = await asyncio.wait_for(
            run_ingestion(_cfg, _conn), timeout=_cfg.ingest_timeout
        )
    except asyncio.TimeoutError:
        return _error(500, f"ingestion timed out after {_cfg.ingest_timeout}s", "INGEST_TIMEOUT")
    except Exception as exc:
        logger.error("manual ingest failed: %s", exc)
        return _error(500, f"ingestion failed: {exc}", "INGEST_ERROR")

    _last_result = result
    _log_ingestion_result(result)

    return {"success": True, "result": result.to_dict()}


# ---------------------------------------------------------------------------
# Entry point (direct run without uvicorn CLI)
# ---------------------------------------------------------------------------

async def _run_once_flow(cfg: Config) -> None:
    """
    RUN_ONCE=true one-shot flow (local testing): one-time backfill (if the
    flag and marker say so) → one ingestion run → one NULL/empty repair
    pass → exit cleanly. No scheduler, no HTTP server.
    """
    global _cfg, _conn, _last_result
    _cfg, _conn = cfg, None
    conn = None
    try:
        conn = open_db(cfg.turso_url, cfg.turso_token)
        init_db(conn)
        _conn = conn

        await _maybe_category_backfill()

        logger.info("[run-once] starting single ingestion run...")
        result = await asyncio.wait_for(
            run_ingestion(_cfg, _conn), timeout=_cfg.ingest_timeout
        )
        _last_result = result
        _log_ingestion_result(result)

        logger.info("[run-once] starting single NULL/empty repair pass...")
        await asyncio.wait_for(
            repair_articles(_cfg, _conn), timeout=_cfg.ingest_timeout
        )
        logger.info("[run-once] done — exiting cleanly")
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


if __name__ == "__main__":
    import uvicorn

    # Read port from env so `python main.py` respects PORT like `uvicorn` does.
    from dotenv import load_dotenv
    load_dotenv(".env", override=False)
    port = int(_os.getenv("PORT", "8080"))

    if load_config().run_once:
        # RUN_ONCE=true → execute the configured operation once and exit;
        # no endless local scheduler is left running.
        asyncio.run(_run_once_flow(load_config()))
    else:
        uvicorn.run(
            "main:app",
            host="0.0.0.0",
            port=port,
            log_level="info",
            # Reload is off for production; use --reload flag from CLI for dev.
        )
