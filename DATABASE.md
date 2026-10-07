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
                            │ per article (max 7/run, sequential):
                            │ system prompt + article data
                            ▼
          [ LLMPing LLM Brain — POST /chat (external) ]
                            │
                            │ parsed answer stored via
                            │ save_llm_parse → llm_* columns
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

The schema is created automatically on application startup via `init_db()` in [`database.py`](./database.py). It is strictly **idempotent** (`CREATE TABLE IF NOT EXISTS`).

```sql
CREATE TABLE IF NOT EXISTS news (
    id               TEXT PRIMARY KEY,  -- Deterministic 16-hex SHA-256 hash of URL
    title            TEXT NOT NULL,     -- Article title / headline
    description      TEXT,              -- Short summary / subtitle (nullable)
    content          TEXT,              -- Full article body text (nullable)
    url              TEXT NOT NULL UNIQUE, -- Canonical URL & deduplication key
    image_url        TEXT,              -- Hero image URL (nullable)
    source           TEXT,              -- Publisher name (e.g., "BBC News")
    author           TEXT,              -- Author name or joined list (nullable)
    category         TEXT,              -- Primary category (e.g., "technology")
    published_at     TEXT NOT NULL,     -- ISO 8601 UTC timestamp
    fetched_at       TEXT NOT NULL,     -- ISO 8601 UTC timestamp of ingestion
    provider         TEXT,              -- Ingestion provider ("newsapi", etc.)
    llm_answer       TEXT,              -- Parsed answer from the LLMPing LLM Brain (nullable)
    llm_provider     TEXT,              -- LLMPing-reported provider used for the parse (nullable)
    llm_model        TEXT,              -- LLMPing-reported model used for the parse (nullable)
    llm_processed_at TEXT               -- ISO 8601 UTC, when the parse was stored (nullable)
);

CREATE INDEX IF NOT EXISTS idx_news_published_at ON news (published_at DESC);
CREATE INDEX IF NOT EXISTS idx_news_url          ON news (url);
```

### Migration for the LLM parse columns

Databases created before the LLMPing phase are migrated transparently: on every startup `init_db()` runs an idempotent `ALTER TABLE news ADD COLUMN ...` for each of the four `llm_*` columns (skipped when the column already exists). No data is touched.

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
| `llm_answer` | `TEXT` | `NULL` | Parsed/structured result produced by the LLMPing LLM Brain for this article, stored exactly as received. `NULL` until processed. Written only by `save_llm_parse()`. |
| `llm_provider` | `TEXT` | `NULL` | Provider LLMPing reports using for the parse (LLMPing owns provider/model selection). |
| `llm_model` | `TEXT` | `NULL` | Model LLMPing reports using for the parse. |
| `llm_processed_at` | `TEXT` | `NULL` | UTC ISO 8601 timestamp of when the parse result was stored. |

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
- **LLM parse safety**: The upsert column list deliberately excludes the `llm_*` columns, so re-ingesting the same URL can never erase a stored parse. The parse phase re-writes them after each run's `save_llm_parse()` call.

### 5.3. LLM Parse Store (`save_llm_parse`)
After the upsert loop, each stored article (max `MAX_ARTICLES` = 7 per run) is sent to the LLMPing LLM Brain and the parsed answer is persisted on the row:
```sql
UPDATE news
SET llm_answer = ?, llm_provider = ?, llm_model = ?, llm_processed_at = ?
WHERE id = ?;
```
- Only the `llm_*` columns are touched — the raw article data written by the upsert is never modified.
- A failed LLMPing call is logged and skipped: the row keeps `llm_answer = NULL` and the run moves to the next article. Nothing is fabricated.

### 5.4. Rolling 7-Day Retention Sweep
To keep the database lean and performant, every ingestion run automatically sweeps expired records at the end of its cycle:
```sql
DELETE FROM news WHERE published_at < ?;
```
- The cutoff is computed as `now - RETENTION_DAYS` (default 7 days).
- Older articles are purged automatically, maintaining a clean rolling window of recent news.

---

## 6. SQL Query Patterns Used in the Code

### 6.1. Feed Query with Filters and Pagination ([`database.py:list_articles`](./database.py))
```sql
-- 1. Get total count of matching articles
SELECT COUNT(*) FROM news WHERE category = ? AND source = ?;

-- 2. Fetch paginated slice (newest first, id ASC as stable tiebreaker)
SELECT id, title, description, content, url, image_url, source, author, category,
       published_at, fetched_at, provider,
       llm_answer, llm_provider, llm_model, llm_processed_at
FROM news
WHERE category = ? AND source = ?
ORDER BY published_at DESC, id ASC
LIMIT ? OFFSET ?;
```

### 6.2. Single Article Lookup ([`database.py:get_article_by_id`](./database.py))
```sql
SELECT id, title, description, content, url, image_url, source, author, category,
       published_at, fetched_at, provider,
       llm_answer, llm_provider, llm_model, llm_processed_at
FROM news
WHERE id = ?;
```

### 6.3. System Statistics ([`database.py:count_articles`](./database.py))
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
-- LLM parse coverage (articles still missing a parsed result):
sqlite> SELECT COUNT(*) FROM news WHERE llm_answer IS NULL;
sqlite> SELECT title, llm_provider, llm_model, llm_processed_at FROM news WHERE llm_answer IS NOT NULL LIMIT 5;
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

## 8. Integration with the LLM Brain (LLMPing)

The `llm_*` columns are the database's interface to the LLM Brain. The LLMPing service (external, `https://llmping.onrender.com`) does the actual inference — OrcaDeLast247 only orchestrates:

1. **Clean input**: Deduplicated, normalized article rows (author/category/source standardized across providers) are the prompt payload.
2. **Per-article parse**: Each run sends up to 10 articles, one at a time, with the configured system prompt (`LLM_SYSTEM_PROMPT`) plus the article data. The parsed reply is stored as received in `llm_answer`, and its `category` value is written to the `category` column — the database remains the final persistent storage. A repair pass every 2 hours re-processes only rows still missing `category`/`llm_*` values.
3. **Provenance**: `llm_provider` and `llm_model` record which backend LLMPing used, and `llm_processed_at` records when.
4. **Read access**: Parsed results are exposed through the backend HTTP API (`GET /api/news` returns the `llm_*` fields on each article) — see [UI_API.md](./UI_API.md). The frontend never queries the database directly; SQL access is for operators/debugging only (see section 7).
