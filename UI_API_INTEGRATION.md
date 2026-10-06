# Last247 — UI / API Integration Guide

Complete reference for the frontend/UI team to integrate with the Last247 Go backend microservice. All endpoints, schemas, and behaviors documented here are verified against the actual implementation.

---

## 1. Overview

The Last247 service is a Go HTTP microservice that:

1. **Ingests** news from provider APIs (NewsAPI, GNews, NewsData.io, WebFetch) using sequential fallback.
2. **Stores** normalized, deduplicated articles in a Turso database.
3. **Serves** the stored articles via a JSON HTTP API.

```text
News providers → Go Ingestion Service (this repo) → Turso `news` table
                                              ↘
                                           HTTP API (served here) ← Next.js Frontend
```

The frontend talks to this service via HTTP. This service never calls the frontend.

---

## 2. Base URL

The service binds to `0.0.0.0` and listens on the port from the `PORT` environment variable (default `8080`).

| Environment | Base URL |
|-------------|----------|
| Local dev    | `http://localhost:8080` |
| Render       | `https://<your-service>-<region>.onrender.com` |

All API paths are relative to this base URL.

---

## 3. Available Endpoints

### `GET /health` — Health Check

Liveness and readiness probe. Use this for Render health checks.

**Parameters:** None

**Response — 200 OK**

```json
{
  "status": "ok",
  "timestamp": "2026-10-02T06:10:22Z"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `status` | string | Always `"ok"` when the service is healthy. |
| `timestamp` | string | Current UTC time in RFC 3339 format. |

**Error responses:** None under normal operation. The endpoint does not depend on the database.

---

### `GET /api/news` — List Articles

Paginated list of stored articles, newest first. Supports optional filtering by category and source.

**Query Parameters**

| Parameter | Type | Required | Default | Max | Description |
|-----------|------|----------|---------|-----|-------------|
| `limit`   | int  | No       | 50      | 100 | Maximum articles to return per page. Values >100 are clamped to 100. Invalid values fall back to 50. |
| `offset`  | int  | No       | 0       | —   | Number of articles to skip (for pagination). Negative values fall back to 0. |
| `category`| string | No     | (none)  | —   | If provided, only articles with this exact category are returned. |
| `source`  | string | No     | (none)  | —   | If provided, only articles from this exact publisher are returned. |

**Response — 200 OK**

```json
{
  "articles": [
    {
      "id": "a1b2c3d4e5f6a7b8",
      "title": "Breaking: Example Story",
      "description": "A brief summary of the story.",
      "content": "Full article body text.",
      "url": "https://example.com/article-slug",
      "image_url": "https://example.com/image.jpg",
      "source": "BBC News",
      "author": "Jane Doe",
      "category": "Technology",
      "published_at": "2026-09-30T10:00:00Z",
      "fetched_at": "2026-09-30T12:34:56Z",
      "provider": "newsapi"
    }
  ],
  "total": 42,
  "limit": 50,
  "offset": 0
}
```

| Field | Type | Description |
|-------|------|-------------|
| `articles` | array | List of Article objects (see data model below). Empty array `[]` when no articles match. |
| `total` | int | Total count of matching articles (before pagination). |
| `limit` | int | The limit value used (after clamping/parsing). |
| `offset` | int | The offset value used (after parsing). |

**Error responses:** None. The endpoint always returns 200, even with zero results.

**Examples:**

```
GET /api/news
GET /api/news?limit=20&offset=40
GET /api/news?category=Technology
GET /api/news?source=BBC+News&limit=10
```

---

### `GET /api/news/{id}` — Get Article by ID

Retrieve a single article by its deterministic ID.

**Path Parameters**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `id` | string | Yes | 16-character hex string (first 16 chars of SHA-256 of the article URL). |

**Response — 200 OK**

```json
{
  "id": "a1b2c3d4e5f6a7b8",
  "title": "Breaking: Example Story",
  "description": "A brief summary of the story.",
  "content": "Full article body text.",
  "url": "https://example.com/article-slug",
  "image_url": "https://example.com/image.jpg",
  "source": "BBC News",
  "author": "Jane Doe",
  "category": "Technology",
  "published_at": "2026-09-30T10:00:00Z",
  "fetched_at": "2026-09-30T12:34:56Z",
  "provider": "newsapi"
}
```

**Error responses**

```json
{
  "error": "article with id \"nonexistent\" not found",
  "code": "NOT_FOUND"
}
```

| Status | Code | Description |
|--------|------|-------------|
| 404 | `NOT_FOUND` | No article with the given ID exists in the database. |
| 500 | `INTERNAL_ERROR` | Database query failed. |

---

### `GET /api/stats` — Database Statistics

Returns the total article count and metadata about the last ingestion run.

**Parameters:** None

**Response — 200 OK**

```json
{
  "total_articles": 142,
  "last_ingestion": {
    "provider": "newsapi",
    "total": 50,
    "inserted": 45,
    "skipped": 5,
    "deleted": 3,
    "source_time": "2026-10-02T06:00:00Z"
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `total_articles` | int | Total number of articles currently in the `news` table. |
| `last_ingestion` | object | Result of the most recent ingestion run. Absent if no ingestion has run yet. |

**`last_ingestion` sub-fields** (IngestionResult struct from `ingest.go`):

| Field | Type | Description |
|-------|------|-------------|
| `provider` | string | Provider that supplied articles: `"newsapi"`, `"gnews"`, `"newsdata"`, `"webfetch"`, or `""` if all providers failed/unconfigured. |
| `total` | int | Articles returned by the provider before dedup. |
| `inserted` | int | Rows inserted or updated in the database. |
| `skipped` | int | Articles dropped (invalid, duplicate URL within run, or DB insert failure). |
| `deleted` | int | Stale articles removed by the retention sweep. |
| `source_time` | string | When the provider was queried (RFC 3339 UTC). |

**Error responses**

| Status | Code | Description |
|--------|------|-------------|
| 500 | `INTERNAL_ERROR` | Database query failed. |

---

### `POST /api/ingest` — Trigger Ingestion

Triggers a manual ingestion run. The service also runs ingestion automatically on startup and then periodically (every `INGEST_INTERVAL`, default 6h). Use this endpoint to force a run on demand.

**Parameters:** None

**Request body:** None (empty body)

**Response — 200 OK**

```json
{
  "success": true,
  "result": {
    "provider": "newsapi",
    "total": 50,
    "inserted": 45,
    "skipped": 5,
    "deleted": 3,
    "source_time": "2026-10-02T06:00:00Z"
  }
}
```

**Error responses**

| Status | Code | Description |
|--------|------|-------------|
| 500 | `INGEST_ERROR` | Ingestion failed (e.g., database unreachable). |
| 503 | `INGEST_TIMEOUT` | Ingestion did not complete within `INGEST_TIMEOUT` (default 120s). *(Not yet implemented — returns 500 with `INGEST_ERROR` if the context is cancelled.)* |

---

## 4. Article Data Model

The `Article` struct (in `article.go`) is the canonical data shape for articles returned by all endpoints. It matches the `news` table columns exactly. Fields with `omitempty` JSON tags are omitted from the response when they are empty strings.

```typescript
interface Article {
  id: string;            // 16-char hex, SHA-256(URL)[:16], deterministic
  title: string;         // always present, non-empty, NOT NULL
  description?: string;  // may be absent (empty string → omitted)
  content?: string;      // may be absent
  url: string;           // always present, non-empty, UNIQUE
  image_url?: string;    // may be absent
  source?: string;       // publisher name, may be absent
  author?: string;       // may be absent (NewsData.io joins multiple with ", ")
  category?: string;     // single category, may be absent
  published_at: string;  // ISO 8601 / RFC 3339, UTC, NOT NULL
  fetched_at: string;   // ISO 8601 / RFC 3339, UTC, NOT NULL
  provider: string;     // "newsapi" | "gnews" | "newsdata" | "webfetch"
}
```

### Field invariants (always true for stored articles)

| Field | Invariant |
|-------|-----------|
| `id` | 16-character lowercase hex string. Deterministic — same URL always produces the same ID. |
| `title` | Never `null`, never `""`. Articles without titles are dropped before storage. |
| `url` | Never `null`, never `""`. The uniqueness/deduplication key. |
| `published_at` | Never `null`. Always valid ISO 8601. If the source timestamp is unparseable, it falls back to `now`. |
| `fetched_at` | Never `null`. UTC timestamp of when the service stored the article. |
| `provider` | Never `null`. One of `"newsapi"`, `"gnews"`, `"newsdata"`, `"webfetch"`. |
| `description`, `content`, `image_url`, `source`, `author`, `category` | May be `null` in the database. In JSON, they are **omitted** when empty (Go `omitempty`). |

### Optional fields handling in the UI

When an optional field is absent from the JSON response, the UI should treat it as empty/undefined:

```typescript
const imageUrl = article.image_url || '/placeholder.png';
const source = article.source || 'Unknown';
const author = article.author || 'Unknown';
// description and content may be undefined — check before rendering
```

---

## 5. CORS Support

The service includes a CORS middleware (`api.go`) configured via the `CORS_ALLOW_ORIGINS` environment variable.

| Env var | Default | Description |
|---------|---------|-------------|
| `CORS_ALLOW_ORIGINS` | `*` | Comma-separated list of allowed origins. `*` allows all origins (with `Access-Control-Allow-Credentials: false`). Specify exact origins for production. |

**CORS headers sent on every response:**

```
Access-Control-Allow-Methods: GET, POST, OPTIONS
Access-Control-Allow-Headers: Content-Type
```

**Pre-flight handling (`OPTIONS`):**

The service intercepts `OPTIONS` requests and responds with `204 No Content` and the appropriate CORS headers — the request is not forwarded to the route handler.

**Example — restricted origins** (production):

```
CORS_ALLOW_ORIGINS=https://last247.vercel.app,https://app.last247.dev
```

**Example — permissive** (local development):

```
CORS_ALLOW_ORIGINS=*
```

---

## 6. Data Flow

### Ingestion: providers → database (write path)

1. **Startup**: On service start, an initial ingestion run fires immediately.
2. **Background**: After the initial run, ingestion repeats every `INGEST_INTERVAL` (default 6h).
3. **Fallback chain**: Providers are tried in order — NewsAPI → GNews → NewsData.io → WebFetch. The first provider with a valid API key that returns ≥1 article wins. All providers are never called simultaneously.
4. **Normalization**: Each provider's response is normalized into the `Article` shape.
5. **Deduplication**: Within a run, duplicate URLs are skipped. Across runs, `ON CONFLICT(url) DO UPDATE` refreshes existing rows.
6. **Retention**: At the end of each run, articles with `published_at` older than `RETENTION_DAYS` (default 7) are deleted.
7. **Result**: The result is stored in memory and returned by `GET /api/stats` and `POST /api/ingest`.

### API: database → frontend (read path)

```
Next.js Frontend → GET /api/news (this service) → Turso `news` table → JSON
                                      ↓
                              SELECT ... ORDER BY published_at DESC
                              LIMIT n OFFSET m
```

The `news` table has an index `idx_news_published_at ON news (published_at DESC)` that makes the default ordering efficient.

---

## 7. Environment Variables (Backend)

All configuration is via environment variables. The `.env` file is supported for local development.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `TURSO_DATABASE_URL` | **Yes** | — | Turso connection string (`libsql://name.turso.io`) or local file (`file:./news.db`). |
| `TURSO_AUTH_TOKEN` | Remote only | `""` | Turso auth token. Leave empty for local file databases. |
| `PORT` | No | `8080` | HTTP listen port (Render provides this dynamically). |
| `INGEST_INTERVAL` | No | `6h` | Background ingestion cadence (e.g., `6h`, `30m`). |
| `INGEST_TIMEOUT` | No | `120s` | Max duration for one ingestion run. |
| `RETENTION_DAYS` | No | `7` | Rolling retention window in days. |
| `NEWS_API_KEY` | One of four | `""` | NewsAPI key (provider #1). |
| `GNEWS_API_KEY` | One of four | `""` | GNews key (provider #2). |
| `NEWS_DATA_API_KEY` | One of four | `""` | NewsData.io key (provider #3). |
| `WEBFETCH_API_URL` | One of four | `""` | WebFetch base URL (provider #4). |
| `WEBFETCH_API_KEY` | Conditional | `""` | WebFetch API key (optional — endpoint may be open). |
| `NEWS_LANGUAGE` | No | `en` | Article language filter. |
| `MAX_ARTICLES` | No | `50` | Max articles fetched per provider per run. |
| `CORS_ALLOW_ORIGINS` | No | `*` | Comma-separated allowed CORS origins. |

---

## 8. Pagination, Filtering, and Sorting

| Feature | Parameters | SQL Clause | Notes |
|---------|-----------|------------|-------|
| **Pagination** | `limit`, `offset` | `LIMIT ? OFFSET ?` | Defaults: limit=50, max=100. |
| **Filtering** | `category`, `source` | `WHERE category = ? AND source = ?` | Exact match. Multiple filters are AND-combined. |
| **Sorting** | (fixed) | `ORDER BY published_at DESC, id ASC` | No user-configurable sorting. Results are always newest-first. The `id ASC` tiebreaker ensures stable ordering when `published_at` values are identical. |
| **Search** | Not implemented | — | The backend does not provide text search. Frontend can filter by `category` or `source` only. Full-text search must be implemented in the frontend's own logic or a future backend enhancement. |

### Stable pagination

Pagination is cursor-based via `offset`. The `id ASC` tiebreaker guarantees stable ordering across requests even when multiple articles share the same `published_at` timestamp.

---

## 9. Error Handling

All error responses use a consistent JSON shape:

```json
{
  "error": "human-readable description",
  "code": "ERROR_CODE"
}
```

| Status Code | Code | When |
|-------------|------|------|
| 404 | `NOT_FOUND` | Article ID not found in `GET /api/news/{id}`. |
| 500 | `INTERNAL_ERROR` | Database query failure. |
| 500 | `INGEST_ERROR` | Ingestion run failed (`POST /api/ingest`). |

The service does not return HTTP 400 for invalid query parameters — it silently clamps them to safe defaults (e.g., `limit > 100` → 100, non-numeric → 50).

---

## 10. `fetch()` Examples

### Fetch a paginated list

```typescript
async function fetchArticles(
  limit = 50,
  offset = 0,
  category?: string,
  source?: string,
) {
  const params = new URLSearchParams({
    limit: String(limit),
    offset: String(offset),
  });
  if (category) params.set('category', category);
  if (source) params.set('source', source);

  const res = await fetch(`${BASE_URL}/api/news?${params.toString()}`);

  if (!res.ok) {
    const err = await res.json();
    throw new Error(`${err.code}: ${err.error}`);
  }

  const data: {
    articles: Article[];
    total: number;
    limit: number;
    offset: number;
  } = await res.json();

  return {
    articles: data.articles,
    total: data.total,
    hasMore: data.offset + data.articles.length < data.total,
  };
}

// Usage
const { articles, total, hasMore } = await fetchArticles(20, 0);
```

### Fetch a single article by ID

```typescript
async function fetchArticle(id: string): Promise<Article> {
  const res = await fetch(`${BASE_URL}/api/news/${id}`);

  if (!res.ok) {
    if (res.status === 404) {
      throw new Error('Article not found');
    }
    const err = await res.json();
    throw new Error(`${err.code}: ${err.error}`);
  }

  return res.json();
}

// Usage
const article = await fetchArticle('a1b2c3d4e5f6a7b8');
```

### Check health

```typescript
async function checkHealth() {
  const res = await fetch(`${BASE_URL}/health`);
  if (!res.ok) throw new Error('Backend unhealthy');
  const data: { status: string; timestamp: string } = await res.json();
  return data.status === 'ok';
}
```

### Get stats / last ingestion

```typescript
async function fetchStats() {
  const res = await fetch(`${BASE_URL}/api/stats`);
  if (!res.ok) throw new Error('Failed to fetch stats');
  const data = await res.json();
  // data.total_articles: number
  // data.last_ingestion: { provider, total, inserted, skipped, deleted, source_time } | undefined
  return data;
}
```

### Trigger manual ingestion

```typescript
async function triggerIngest() {
  const res = await fetch(`${BASE_URL}/api/ingest`, {
    method: 'POST',
  });

  if (!res.ok) {
    const err = await res.json();
    throw new Error(`${err.code}: ${err.error}`);
  }

  const data = await res.json();
  // data.success: true
  // data.result: IngestionResult
  return data.result;
}
```

---

## 11. Loading / Empty / Error States

### Loading

- The initial ingestion runs on startup. If the frontend loads before ingestion completes, the database may be empty. Show a loading spinner.
- Use `GET /health` to confirm the backend is up before fetching articles.

### Empty states

- `GET /api/news` returns `{"articles": [], "total": 0, "limit": 50, "offset": 0}` when no articles exist. The UI should display an empty-state message.
- `GET /api/news/{id}` returns `404 NOT_FOUND` when the ID does not exist. The UI should show a "not found" page.

### Error states

- Connection errors to `/api/news`, `/api/news/{id}`, `/api/stats`: retry with exponential backoff.
- `POST /api/ingest` returns 500 when ingestion fails (e.g., database unreachable). The UI should surface this as a user-visible error.
- `GET /health` does not depend on the database — a 200 response means the HTTP service is running, but does not guarantee data freshness.

---

## 12. Important Constraints & Notes

1. **Article ID format**: The `id` is a 16-character lowercase hex string derived from `SHA-256(URL)[:16]`. It is deterministic — the same URL always maps to the same ID across runs and restarts.
2. **Timestamps**: `published_at` and `fetched_at` are always UTC ISO 8601 strings. Use `new Date(published_at)` in JavaScript to parse.
3. **Optional fields**: Fields with `omitempty` are absent from JSON when empty. Check for `undefined` before rendering, not just falsy values — an empty string `""` is indistinguishable from absent in JSON.
4. **No search**: Text search is not implemented. Available filters: `category` and `source` (exact match) on `GET /api/news`.
5. **No user authentication**: No auth is implemented. In production, put the service behind a reverse proxy or firewall if needed.
6. **Provider order is fixed**: NewsAPI → GNews → NewsData.io → WebFetch. Only the first provider with a key that returns articles is used per run.
7. **Retention**: Articles older than `RETENTION_DAYS` (default 7) are deleted at the end of each ingestion run. The database only contains recent articles.
8. **Upsert semantics**: Re-fetching the same article URL refreshes all fields (title, description, content, image, etc.) via `ON CONFLICT(url) DO UPDATE`.

---

*Generated from the Go backend source. Last verified against:*
- `article.go` — Article struct and JSON/DB tags
- `db.go` — Database schema, ListArticles, GetArticleByID, GetArticleByURL, CountArticles
- `config.go` — Environment variable loading and defaults
- `ingest.go` — IngestionResult struct, runIngestion logic
- `api.go` — HTTP handlers, CORS middleware, helpers
- `main.go` — HTTP server setup, routes, background ingestion
- `providers/providers.go` — HTTP fetching and JSON decoding