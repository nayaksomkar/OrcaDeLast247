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
seconds (default 6 h) using an asyncio background task.

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
    open_db,
)
from ingest import run_ingestion
from models import Article, IngestionResult

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
        "SourceTime: %s\n"
        "=========================",
        r.provider, r.total, r.inserted, r.skipped, r.deleted, r.source_time,
    )


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

    # Start background ingestion as a fire-and-forget task.
    task = asyncio.create_task(_ingestion_loop())

    yield  # server is running

    # --- Shutdown ---
    logger.info("shutting down server...")
    task.cancel()
    try:
        await task
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

# CORS middleware is configured from cfg.cors_origins, but since we need
# cfg at app-creation time and it isn't loaded yet, we add CORS dynamically
# after startup — or use a simpler approach: read from env directly here.
import os as _os
from dotenv import load_dotenv as _load_dotenv
_load_dotenv(".env", override=False)
_raw_cors = _os.getenv("CORS_ALLOW_ORIGINS", "*").strip()
_cors_origins = [o.strip() for o in _raw_cors.split(",") if o.strip()] or ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
    allow_credentials=False,
)

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
    category: str = Query(default="", description="Filter by exact category."),
    source: str = Query(default="", description="Filter by exact source/publisher."),
):
    """
    Paginated list of articles, newest first.

    Optional filters: `category` and `source` (exact match, AND-combined).
    `total` in the response is the count of matching rows before pagination.
    Always returns 200 — `articles` is [] when nothing matches.
    """
    try:
        articles, total = await asyncio.to_thread(
            list_articles, _conn, limit, offset, category, source
        )
    except Exception as exc:
        logger.error("list_articles failed: %s", exc)
        return _error(500, "failed to fetch articles", "INTERNAL_ERROR")

    return {
        "articles": [a.model_dump(exclude_none=True) for a in articles],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


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

    The run is bounded by cfg.ingest_timeout (default 120 s).
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

if __name__ == "__main__":
    import uvicorn

    # Read port from env so `python main.py` respects PORT like `uvicorn` does.
    from dotenv import load_dotenv
    load_dotenv(".env", override=False)
    port = int(_os.getenv("PORT", "8080"))

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        log_level="info",
        # Reload is off for production; use --reload flag from CLI for dev.
    )
