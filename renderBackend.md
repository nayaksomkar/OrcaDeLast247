# Last247 Render Backend — UI Sync Guide

The backend is **live and deployed**: everything below was tested against the
actual running service (real requests, real captured responses).

| What | Value |
|------|-------|
| Base URL | **`https://orcadelast247.onrender.com`** |
| Auth | None — no keys or headers required |
| CORS | Configured via `CORS_ALLOW_ORIGINS` (comma-separated). This deployment currently runs with `*`; setting the env var to e.g. `http://localhost:3000,https://your-frontend.vercel.app` restricts it — allowed origins are echoed exactly, disallowed ones get no CORS header. Preflight answered `200` with `access-control-allow-methods: GET, POST, OPTIONS` and `Content-Type` allowed. The UI can call it from any allowed origin |
| Methods allowed | `GET`, `POST`, `OPTIONS` |

The backend auto-ingests at startup and every 8 h; the UI only ever **reads**.
Detailed field docs: [`UI_API.md`](./UI_API.md).

---

## Endpoints (verified on this deployment)

### `GET /health`

Liveness only (does not check the DB). Real response:

```json
{"status": "ok", "timestamp": "2026-10-06T23:39:40Z"}
```

### `GET /api/news` — the feed

Query params: `limit` (1–100, default 50), `offset` (≥0), `category`, `source`
(exact match). Sort is fixed: newest first. Out-of-range/invalid params →
**422**, never a silent clamp. Always `200` otherwise, `articles: []` when empty.

Real response values (deployed instance, current data):

```json
{
  "total": 20,
  "limit": 3,
  "offset": 0,
  "articles": [
    {
      "id": "098baacdc7806e54",
      "title": "4 Shane Beamer replacements South Carolina must start eyeing",
      "url": "https://…",
      "source": "247Sports",
      "author": "…",
      "category": "…",          // NOTE: sometimes absent — see field rules below
      "published_at": "2026-10-05T23:37:23+00:00",
      "fetched_at": "…",
      "provider": "newsapi",
      "llm_answer": "…",          // JSON string — parsed by the LLM Brain (see below)
      "llm_provider": "groq",
      "llm_model": "openai/gpt-oss-20b",
      "llm_processed_at": "…"
    }
  ]
}
```

Live checks on this deployment: `total=20`, all rows `provider:"newsapi"`,
every sampled row fully LLM-parsed.

### `GET /api/news/{id}` — single article

`{id}` = 16-char lowercase hex (SHA-256 of the URL, first 16 chars) — stable
across restarts, so bookmark/deep-link friendly.

- 200 → one article object (same shape as a list item)
- Missing/aged-out id → `404 {"error":"article with id \"X\" not found","code":"NOT_FOUND"}`
  (verified live)
- Optional fields (e.g. `category` on this real article) are **omitted from
  the JSON when empty** — always guard before rendering:

  ```js
  const cat = article.category || 'General';
  const img = article.image_url || '/placeholder.png';
  ```

### `GET /api/stats`

```json
{"total_articles": 20}
```

→ becomes the full shape after a completed run:

```json
{
  "total_articles": 20,
  "last_ingestion": {
    "provider": "newsapi",
    "total": 7,
    "inserted": 7,
    "skipped": 0,
    "deleted": 0,
    "parsed": 7,
    "parse_failed": 0,
    "source_time": "2026-10-06T23:40:11Z"
  }
}
```

**Important for sync UIs:** `last_ingestion` is in-memory and **absent right
after a cold start/restart** until the first ingestion run completes — the
response can be just `{"total_articles": N}` in the middle of a boot. Don't
treat its absence as an error or an empty feed signal; the articles are there
(persisted in the database).

### `POST /api/ingest` — admin/internal, do NOT call from product UI

Triggers a real ingestion cycle synchronously (provider fetch → normalize →
dedup → Turso → one LLMPing parse per article). It is **unauthenticated** and
quota-consuming, and took ~3–4 minutes of wall time on this deployment.

Verified live result:

```json
{
  "success": true,
  "result": {
    "provider": "newsapi",
    "total": 7,
    "inserted": 7,
    "skipped": 0,
    "deleted": 0,
    "parsed": 7,
    "parse_failed": 0,
    "source_time": "2026-10-06T23:40:11Z"
  }
}
```

After that run, `GET /api/stats` returned exactly the full shape above, and
`total_articles` stayed `20` — re-ingested stories are upserted in place, never
duplicated.

---

## How the UI should sync

```js
const API_BASE_URL = "https://orcadelast247.onrender.com";

async function loadArticles(limit = 20, offset = 0) {
  const res = await fetch(`${API_BASE_URL}/api/news?limit=${limit}&offset=${offset}`);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);          // 422 / 500
  const data = await res.json();
  return {
    articles: data.articles,
    hasMore: data.offset + data.articles.length < data.total,  // offset-based paging
  };
}
```

- **Poll `GET /api/news`** (e.g. on load + when the user refreshes). New data
  arrives automatically every 8 h — no other trigger exists.
- **Never call `POST /api/ingest`** from the product UI — it can take minutes
  and burns provider/LLM quota. It exists for admin/testing.
- `llm_answer` is a JSON string on the wire. With the shipped editorial prompt
  it always parses to:
  `{title, url, source, author, category, published_at, summary, key_points[]}`
  (verified live on this deployment — the LLM used groq `openai/gpt-oss-20b`).

  ```js
  let parsed = null;
  try { parsed = JSON.parse(article.llm_answer); } catch { /* raw text */ }
  const summary = parsed?.summary ?? article.description ?? "";
  ```
- Render free tier cold-starts the service — an occasional 5–60 s first-connect
  latency is normal; retry once or twice rather than erroring.

## Error handling (all real shapes)

| Situation | Response |
|-----------|----------|
| Unknown/too-old article id | `404 {"error": "...", "code": "NOT_FOUND"}` |
| `limit`/`offset` invalid | `422 {"detail":[{...}]}` (FastAPI validation) |
| DB failure | `500 {"error": "...", "code": "INTERNAL_ERROR"}` |
| Ingest failed/timed out | `500 {"code":"INGEST_ERROR"|"INGEST_TIMEOUT"}` (admin only) |
| Cold start delay | connection timeout — retry |

## Field rules (verified)

- Always present: `id`, `title`, `url`, `published_at`, `fetched_at`, `provider`
  (`"newsapi" | "gnews" | "newsdata" | "webfetch"`).
- Timestamps: ISO 8601 UTC in `+00:00` form (`2026-10-05T23:37:23+00:00`) —
  `new Date(value)` parses directly.
- May be omitted entirely: `description`, `content`, `image_url`, `source`,
  `author`, `category`, and all `llm_*` until the article has been parsed.
- Rolling 7-day retention: articles outside the window are deleted at the end
  of each run — a previously valid id can legitimately become `404`.
