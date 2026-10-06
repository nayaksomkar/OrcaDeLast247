# Last247 — Database Architecture & Schema Reference

Complete reference for the Last247 database layer. This document explains the database technology, schema, data lifecycle, indexing strategy, deduplication logic, and SQL access patterns used by the backend.

---

## 1. Overview & Architecture

The Last247 database acts as the **single source of truth** for all news articles in the system.

```text
┌────────────────────────────────────────────────────────┐
│ External News APIs (NewsAPI, GNews, NewsData, WebFetch)│
└──────────────────────────┬─────────────────────────────┘
                           │ Ingestion Pipeline (ingest.py)
                           ▼
                 [ Normalized Article ]
                           │
                           │ Upsert (ON CONFLICT DO UPDATE)
                           ▼
          ┌───────────────────────────────────┐
          │      Turso Database / SQLite      │
          │         Table: `news`             │
          │  - Deduplicated by canonical URL  │
          │  - Rolling 7-day retention        │
          │  - Indexed by published_at DESC   │
          └─────────────────┬─────────────────┘
                            │
                            │ SELECT queries (ORDER BY published_at DESC)
                            ▼
               [ FastAPI Endpoints (/api/news) ]
                            │
                            ▼
                 [ Next.js Frontend UI ]
```

### Key Principles
1. **Frontend Isolation**: The Next.js UI **never** contacts external news providers directly. It reads exclusively from the database via `/api/news`.
2. **Stateless Backend**: The backend does not maintain in-memory news state. All persistence, filtering, and ordering happen directly in the database.
3. **Dual Driver Support**:
   - **Local Development**: Built-in Python `sqlite3` using a local file (`file:./news.db` or `./news.db`). Zero accounts, zero credentials, zero network needed.
   - **Production**: Turso cloud libSQL (`libsql://your-db.turso.io`) via `libsql-experimental` over secure WebSockets.

---

## 2. Table Schema: `news`

The schema is created automatically on application startup via `init_db()` in [`database.py`](file:///home/nsm/Documents/githubREPO/last247DB/database.py). It is strictly **idempotent** (`CREATE TABLE IF NOT EXISTS`).

```sql
CREATE TABLE IF NOT EXISTS news (
    id           TEXT PRIMARY KEY,       -- Deterministic 16-hex SHA-256 hash of URL
    title        TEXT NOT NULL,          -- Article title / headline
    description  TEXT,                   -- Short summary / subtitle (nullable)
    content      TEXT,                   -- Full article body text (nullable)
    url          TEXT NOT NULL UNIQUE,   -- Canonical URL & deduplication key
    image_url    TEXT,                   -- Hero image URL (nullable)
    source       TEXT,                   -- Publisher name (e.g., "BBC News")
    author       TEXT,                   -- Author name or joined list (nullable)
    category     TEXT,                   -- Primary category (e.g., "technology")
    published_at TEXT NOT NULL,          -- ISO 8601 UTC timestamp
    fetched_at   TEXT NOT NULL,          -- ISO 8601 UTC timestamp of ingestion
    provider     TEXT                    -- Ingestion provider ("newsapi", etc.)
);

CREATE INDEX IF NOT EXISTS idx_news_published_at ON news (published_at DESC);
CREATE INDEX IF NOT EXISTS idx_news_url          ON news (url);
```

---

## 3. Column Specifications

| Column | Type | Constraints | Description |
|---|---|---|---|
| `id` | `TEXT` | `PRIMARY KEY` | First 16 hex characters of `SHA-256(url)`. Stable and deterministic across runs. |
| `title` | `TEXT` | `NOT NULL` | Article headline. Articles without a title are discarded during ingestion. |
| `description` | `TEXT` | `NULL` | Summary or snippet. Omitted from JSON API responses if empty/null. |
| `content` | `TEXT` | `NULL` | Full story text (often truncated by external APIs). Omitted if empty/null. |
| `url` | `TEXT` | `NOT NULL UNIQUE` | Canonical article link. Acts as the primary uniqueness and deduplication key. |
| `image_url` | `TEXT` | `NULL` | Link to the hero thumbnail or story image. Omitted if empty/null. |
| `source` | `TEXT` | `NULL` | Publisher brand (e.g. "TechCrunch", "Bloomberg", "Reuters"). |
| `author` | `TEXT` | `NULL` | Byline or author name. Multi-author APIs are joined into a comma-separated string. |
| `category` | `TEXT` | `NULL` | Topic tag (e.g., "technology", "finance", "general"). |
| `published_at` | `TEXT` | `NOT NULL` | Original publication date/time in UTC ISO 8601 format (`YYYY-MM-DDTHH:MM:SS.fZ`). |
| `fetched_at` | `TEXT` | `NOT NULL` | UTC ISO 8601 timestamp recording when the ingestion service stored or refreshed the row. |
| `provider` | `TEXT` | `NULL` | Provider identifier that supplied the article (`newsapi`, `gnews`, `newsdata`, `webfetch`). |

---

## 4. Indexes & Performance

1. **`idx_news_published_at ON news (published_at DESC)`**:
   - **Purpose**: Powers the main news feed query: `ORDER BY published_at DESC LIMIT ? OFFSET ?`.
   - **Why string storage works**: UTC timestamps formatted as `YYYY-MM-DDTHH:MM:SS.fZ` have identical chronological and lexicographical sorting orders. This allows fast B-tree index scans without query-time date parsing.
2. **`idx_news_url ON news (url)`**:
   - **Purpose**: Accelerates conflict detection during upserts (`ON CONFLICT(url)`) and lookups by URL.

---

## 5. Core Data Invariants & Rules

### 5.1. Deterministic Article IDs
Articles are not assigned autoincrementing integers or random UUIDs. Instead:
```python
article_id = hashlib.sha256(url.encode()).hexdigest()[:16]
```
- **Why**: 16 hex characters (64 bits of entropy) guarantee negligible collision risk across rolling news sets.
- **Benefit**: Re-fetching the same URL always resolves to the exact same ID, making bookmarks and frontend links persistent.

### 5.2. Idempotent Deduplication (Upsert)
When an ingestion run receives an article whose URL already exists in the database:
```sql
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
    provider     = excluded.provider;
```
- **Outcome**: The existing row is updated in-place with fresher metadata, preventing duplicate cards from ever appearing in the UI.

### 5.3. Rolling 7-Day Retention Sweep
To keep the database lean and performant, every ingestion run automatically sweeps expired records at the end of its cycle:
```sql
DELETE FROM news WHERE published_at < ?;
```
- The cutoff is computed as `now - RETENTION_DAYS` (default 7 days).
- Older articles are purged automatically, maintaining a clean rolling window of recent news.

---

## 6. SQL Query Patterns Used in the Code

### 6.1. Feed Query with Filters and Pagination ([`database.py:list_articles`](file:///home/nsm/Documents/githubREPO/last247DB/database.py#L225))
```sql
-- 1. Get total count of matching articles
SELECT COUNT(*) FROM news WHERE category = ? AND source = ?;

-- 2. Fetch paginated slice (newest first, id ASC as stable tiebreaker)
SELECT id, title, description, content, url, image_url, source, author, category, published_at, fetched_at, provider
FROM news
WHERE category = ? AND source = ?
ORDER BY published_at DESC, id ASC
LIMIT ? OFFSET ?;
```

### 6.2. Single Article Lookup ([`database.py:get_article_by_id`](file:///home/nsm/Documents/githubREPO/last247DB/database.py#L274))
```sql
SELECT id, title, description, content, url, image_url, source, author, category, published_at, fetched_at, provider
FROM news
WHERE id = ?;
```

### 6.3. System Statistics ([`database.py:count_articles`](file:///home/nsm/Documents/githubREPO/last247DB/database.py#L290))
```sql
SELECT COUNT(*) FROM news;
```

---

## 7. Inspecting & Debugging the Database

### Local SQLite Database
If `TURSO_DATABASE_URL=file:./news.db`:

```bash
# Open SQLite shell
sqlite3 news.db

# Useful inspection queries:
sqlite> .schema news
sqlite> SELECT COUNT(*) FROM news;
sqlite> SELECT id, title, source, published_at FROM news ORDER BY published_at DESC LIMIT 5;
sqlite> SELECT provider, COUNT(*) FROM news GROUP BY provider;
```

### Remote Turso Cloud Database
If `TURSO_DATABASE_URL=libsql://your-db.turso.io`:

```bash
# Using Turso CLI
turso db shell your-db-name

# Queries work identically
turso> SELECT COUNT(*) FROM news;
```

---

## 8. Integration with Future LLM / Brain

The database schema has been intentionally designed to feed downstream AI summarizers or LLM Brain workflows:
1. **Deduplicated & Clean**: No duplicate links or redundant headlines.
2. **Normalized Metadata**: Author, category, and source are standardized regardless of whether the source was NewsAPI, GNews, or NewsData.io.
3. **Direct SQL Access**: An LLM agent can query the database directly using `SELECT title, description, content FROM news WHERE published_at >= ...` without needing external API credentials.
