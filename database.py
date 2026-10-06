"""
database.py — All database operations for the Last247 service.

Supports two backends transparently:
  - Local SQLite  : TURSO_DATABASE_URL starts with "file:" or ends in ".db"
                    Uses the built-in sqlite3 module — zero extra dependencies.
  - Remote Turso  : TURSO_DATABASE_URL starts with "libsql://" or "https://"
                    Uses the libsql-experimental package.

Public API
----------
article_id(url)                      → 16-char hex string (deterministic)
open_db(turso_url, turso_token)      → connection object
init_db(conn)                        → creates table + indexes + llm columns (idempotent)
upsert_article(conn, article)        → INSERT OR REPLACE
save_llm_parse(conn, article_id, answer, provider, model, processed_at)
                                     → store the LLM parse result on an article row
list_articles(conn, ...)             → ([Article], total_count)
get_article_by_id(conn, id)          → Article | None
count_articles(conn)                 → int
delete_stale_articles(conn, days)    → int (rows deleted)

All blocking DB calls are wrapped with asyncio.to_thread() in the ingestion
and API layers so they don't block the FastAPI event loop.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Any, Optional

from models import Article

logger = logging.getLogger(__name__)

# Thread lock for the shared sqlite3 connection (sqlite3 is not thread-safe
# by default). For remote libsql connections each call creates a fresh
# connection, so no lock is needed there.
_sqlite_lock = Lock()


# ---------------------------------------------------------------------------
# ID generation
# ---------------------------------------------------------------------------

def article_id(url: str) -> str:
    """
    Generate a deterministic 16-char hex article ID from its URL.

    Uses the first 16 characters of the SHA-256 hash of the URL — identical
    to Go's ArticleID() function. The same URL always produces the same ID
    across runs, making upserts idempotent.
    """
    return hashlib.sha256(url.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Connection management
# ---------------------------------------------------------------------------

def _is_remote_url(url: str) -> bool:
    """Return True if the URL points to a remote Turso database."""
    return url.startswith("libsql://") or url.startswith("https://")


def _local_path(url: str) -> str:
    """
    Strip 'file:' prefix and return a filesystem path for sqlite3.

    Examples:
        "file:./news.db"  → "./news.db"
        "file:/abs/path"  → "/abs/path"
        "news.db"         → "news.db"   (already a plain path)
    """
    if url.startswith("file:"):
        return url[len("file:"):]
    return url


def open_db(turso_url: str, turso_token: str = "") -> Any:
    """
    Open and return a database connection.

    For local file: URLs, returns a sqlite3.Connection.
    For remote libsql:// URLs, returns a libsql_experimental connection.

    In both cases the connection is verified by executing a lightweight
    SELECT 1 — this surfaces bad URLs / bad tokens immediately on startup
    rather than silently failing during the first article insert.

    Returns the connection object (caller owns it and must close it).
    """
    if _is_remote_url(turso_url):
        try:
            import libsql_experimental as libsql  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "libsql-experimental is required for remote Turso databases. "
                "Install it with: pip install libsql-experimental"
            ) from exc

        conn = libsql.connect(turso_url, auth_token=turso_token)
        conn.execute("SELECT 1")  # liveness ping
        logger.info("connected to remote Turso database: %s", turso_url)
    else:
        # Local SQLite file.
        path = _local_path(turso_url)
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.row_factory = sqlite3.Row  # rows accessible by column name
        conn.execute("SELECT 1")
        logger.info("connected to local SQLite database: %s", path)

    return conn


def init_db(conn: Any) -> None:
    """
    Create the `news` table and indexes if they don't exist.

    Idempotent — safe to call on every startup. Never drops or alters
    existing data.

    Schema mirrors the original Go implementation exactly:
      id           TEXT PRIMARY KEY  — SHA-256(url)[:16]
      url          TEXT NOT NULL UNIQUE — deduplication key
      published_at TEXT NOT NULL     — ISO 8601 UTC (lexicographic = chronological)
      fetched_at   TEXT NOT NULL     — ISO 8601 UTC
      provider     TEXT              — "newsapi" | "gnews" | "newsdata" | "webfetch"

    Also adds the LLM parse-result columns (idempotent ALTER TABLE ADD COLUMN),
    so databases created before the LLM phase get them transparently.
    """
    schema = """
CREATE TABLE IF NOT EXISTS news (
    id           TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    description  TEXT,
    content      TEXT,
    url          TEXT NOT NULL UNIQUE,
    image_url    TEXT,
    source       TEXT,
    author       TEXT,
    category     TEXT,
    published_at TEXT NOT NULL,
    fetched_at   TEXT NOT NULL,
    provider     TEXT
);
CREATE INDEX IF NOT EXISTS idx_news_published_at ON news (published_at DESC);
CREATE INDEX IF NOT EXISTS idx_news_url          ON news (url);
"""
    with _sqlite_lock:
        conn.executescript(schema)
        conn.commit()
        _ensure_llm_columns(conn)


# Columns holding the per-article LLM parse result. Nullable — None means the
# article has not been processed by the LLM Brain (LLMPing) yet.
_LLM_COLUMNS: tuple[tuple[str, str], ...] = (
    ("llm_answer", "TEXT"),
    ("llm_provider", "TEXT"),
    ("llm_model", "TEXT"),
    ("llm_processed_at", "TEXT"),
)


def _ensure_llm_columns(conn: Any) -> None:
    """
    Add the llm_* columns to the news table if they are missing.

    Uses ALTER TABLE ADD COLUMN guarded by a duplicate-column check so it is
    idempotent and portable across SQLite and remote libsql connections
    (PRAGMA table_info is not reliably available over the Hrana protocol).
    """
    for name, ddl in _LLM_COLUMNS:
        try:
            conn.execute(f"ALTER TABLE news ADD COLUMN {name} {ddl}")
        except Exception as exc:
            msg = str(exc).lower()
            if "duplicate column" in msg or "already exists" in msg:
                continue
            raise
        conn.commit()


# ---------------------------------------------------------------------------
# CRUD helpers — all blocking; wrap with asyncio.to_thread() in async callers
# ---------------------------------------------------------------------------

def _fmt_dt(dt: datetime) -> str:
    """Format a datetime as UTC ISO 8601 string for storage."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def upsert_article(conn: Any, article: Article) -> None:
    """
    INSERT the article or UPDATE all fields when the URL already exists.

    ON CONFLICT(url) DO UPDATE means the same story re-fetched on a later
    run refreshes the stored metadata (title, description, image, etc.)
    without creating a duplicate row.
    """
    sql = """
INSERT INTO news (
    id, title, description, content, url, image_url,
    source, author, category, published_at, fetched_at, provider
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(url) DO UPDATE SET
    title        = excluded.title,
    description  = excluded.description,
    content      = excluded.content,
    image_url    = excluded.image_url,
    source       = excluded.source,
    author       = excluded.author,
    category     = excluded.category,
    published_at = excluded.published_at,
    fetched_at   = excluded.fetched_at,
    provider     = excluded.provider
"""
    args = (
        article.id,
        article.title,
        article.description,
        article.content,
        article.url,
        article.image_url,
        article.source,
        article.author,
        article.category,
        _fmt_dt(article.published_at),
        _fmt_dt(article.fetched_at),
        article.provider,
    )
    with _sqlite_lock:
        conn.execute(sql, args)
        conn.commit()


def save_llm_parse(
    conn: Any,
    article_id_val: str,
    answer: str,
    provider: str,
    model: str,
    processed_at: str,
) -> int:
    """
    Store the LLM Brain's parsed result on the article's existing row.

    Only touches the llm_* columns, so it can never clobber the raw article
    fields written by upsert_article(). The answer is stored exactly as
    received from LLMPing — the DB row is the final processed record.

    Returns the number of rows updated (0 means the article_id does not exist).
    """
    with _sqlite_lock:
        cursor = conn.execute(
            "UPDATE news SET "
            "llm_answer = ?, llm_provider = ?, llm_model = ?, llm_processed_at = ? "
            "WHERE id = ?",
            (answer, provider, model, processed_at, article_id_val),
        )
        conn.commit()
    return cursor.rowcount


def _row_to_article(row: Any) -> Article:
    """
    Convert a DB row (sqlite3.Row or libsql row) into an Article.

    Timestamps are stored as ISO 8601 strings; parse them back to datetime.
    Nullable text columns come back as None — coerced to "" for Pydantic.
    """
    def _str(v: Any) -> Optional[str]:
        return str(v) if v else None

    def _dt(v: Any) -> datetime:
        if isinstance(v, datetime):
            return v.astimezone(timezone.utc)
        try:
            s = str(v).replace("Z", "+00:00")
            return datetime.fromisoformat(s).astimezone(timezone.utc)
        except (ValueError, TypeError):
            return datetime.now(timezone.utc)

    # Support both sqlite3.Row (index by name) and plain tuples.
    if hasattr(row, "keys"):
        d = dict(row)
    else:
        keys = [
            "id", "title", "description", "content", "url",
            "image_url", "source", "author", "category",
            "published_at", "fetched_at", "provider",
            "llm_answer", "llm_provider", "llm_model", "llm_processed_at",
        ]
        d = dict(zip(keys, row))

    return Article(
        id=d["id"],
        title=d["title"],
        description=_str(d.get("description")),
        content=_str(d.get("content")),
        url=d["url"],
        image_url=_str(d.get("image_url")),
        source=_str(d.get("source")),
        author=_str(d.get("author")),
        category=_str(d.get("category")),
        published_at=_dt(d["published_at"]),
        fetched_at=_dt(d["fetched_at"]),
        provider=d.get("provider") or "",
        llm_answer=_str(d.get("llm_answer")),
        llm_provider=_str(d.get("llm_provider")),
        llm_model=_str(d.get("llm_model")),
        llm_processed_at=_str(d.get("llm_processed_at")),
    )


_SELECT_COLS = (
    "id, title, description, content, url, image_url, "
    "source, author, category, published_at, fetched_at, provider, "
    "llm_answer, llm_provider, llm_model, llm_processed_at"
)


def list_articles(
    conn: Any,
    limit: int,
    offset: int,
    category: str = "",
    source: str = "",
) -> tuple[list[Article], int]:
    """
    Return a paginated article list and the total matching count.

    Ordering: published_at DESC (newest first), id ASC as tiebreaker for
    stable pagination when multiple articles share the same timestamp.

    Filters: category and source are exact-match WHERE clauses (AND-combined).
    """
    conditions: list[str] = []
    args: tuple[Any, ...] = ()

    if category:
        conditions.append("category = ?")
        args += (category,)
    if source:
        conditions.append("source = ?")
        args += (source,)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    with _sqlite_lock:
        # Total count of matching rows (before pagination).
        count_row = conn.execute(
            f"SELECT COUNT(*) FROM news {where}", args
        ).fetchone()
        total = count_row[0] if count_row else 0

        # Paginated rows.
        # Bind parameters must be a tuple: the remote Turso driver rejects
        # lists with "'list' object cannot be converted to 'PyTuple'".
        rows = conn.execute(
            f"SELECT {_SELECT_COLS} FROM news {where} "
            f"ORDER BY published_at DESC, id ASC "
            f"LIMIT ? OFFSET ?",
            (*args, limit, offset),
        ).fetchall()

    articles = [_row_to_article(r) for r in rows]
    return articles, total


def get_article_by_id(conn: Any, article_id_val: str) -> Optional[Article]:
    """
    Retrieve a single article by its deterministic ID.

    Returns None if no article with that ID exists.
    """
    with _sqlite_lock:
        row = conn.execute(
            f"SELECT {_SELECT_COLS} FROM news WHERE id = ?",
            (article_id_val,),
        ).fetchone()

    return _row_to_article(row) if row else None


def count_articles(conn: Any) -> int:
    """Return the total number of articles in the news table."""
    with _sqlite_lock:
        row = conn.execute("SELECT COUNT(*) FROM news").fetchone()
    return row[0] if row else 0


def delete_stale_articles(conn: Any, retention_days: int) -> int:
    """
    Delete articles published more than retention_days ago.

    The cutoff is formatted as UTC ISO 8601 — the same format used when
    storing published_at — so the WHERE clause uses a plain string comparison
    that is also a chronological comparison.

    Returns the number of rows deleted.
    """
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=retention_days)
    ).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    with _sqlite_lock:
        cursor = conn.execute(
            "DELETE FROM news WHERE published_at < ?", (cutoff,)
        )
        conn.commit()

    return cursor.rowcount
