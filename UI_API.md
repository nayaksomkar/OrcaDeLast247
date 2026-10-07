# Last247 Backend API

Single reference for the Last247 frontend/UI team. Everything here is verified
against the actual backend implementation (`main.py`, `database.py`, `models.py`).

The UI never talks to news providers or the LLM service directly — it talks only
to this backend.

---

## Base URL

The service binds to `0.0.0.0` on the `PORT` env var (default `8080`).

| Environment | Base URL |
|-------------|----------|
| Local dev   | `http://localhost:8080` |
| Deployed    | `https://<your-service>.onrender.com` (Render sets `PORT`/URL) |

## Authentication

**None.** No API keys, tokens, or headers are required. The UI calls the
endpoints directly. (For production deployments, restrict access at the
network/proxy level — e.g. Render's access controls or a reverse proxy.)

## CORS

Configured via `CORS_ALLOW_ORIGINS` — a comma-separated origin list, e.g.
`http://localhost:3000,http://127.0.0.1:3000` (the shipped default in
`.env.example`); empty/unset falls back to `*`. Allowed origins are echoed
exactly in `access-control-allow-origin`; disallowed origins get **no** CORS
header. Allowed methods: `GET`, `POST`, `OPTIONS`; allowed header:
`Content-Type`; credentials not allowed. Pre-flight (`OPTIONS`) replies
`HTTP 200` with the standard `access-control-allow-*` headers — verified live
for both dev origins and for `OPTIONS /api/news` + `OPTIONS /api/ingest`.

---

## Endpoints

### `GET /health` — liveness check

No parameters. Does **not** touch the database — a 200 means the HTTP process
is up, not that the DB is reachable (use `GET /api/stats` for that).

**200 response:**

```json
{"status": "ok", "timestamp": "2026-10-06T22:38:01Z"}
```

---

### `GET /api/news` — paginated article list

**Query parameters**

| Parameter  | Type   | Default | Limits  | Behavior |
|------------|--------|---------|---------|----------|
| `limit`    | int    | 50      | 1–100   | Page size. Outside 1–100 (or non-numeric) → `422`. |
| `offset`   | int    | 0       | ≥ 0     | Rows to skip. Negative → `422`. |
| `category` | string | (none)  | —       | Exact match; empty/omitted → no filter. |
| `source`   | string | (none)  | —       | Exact match (AND-combined with `category`). |

- **Sorting is fixed**: newest first (`published_at DESC`), with `id ASC`
  as a stable tiebreaker. No user-configurable sorting or text search.
- `total` = count of matching rows before pagination → use it for
  has-more/paging math.
- **Valid params always return 200**, even when nothing matches
  (`articles: []`).

**200 response (real example):**

```json
{
  "articles": [
    {
      "id": "cec160263817dd2b",
      "title": "Doc check article",
      "description": "A short summary",
      "content": "Body text",
      "url": "https://example.com/doc-check/article-1",
      "published_at": "2026-10-06T09:30:00+00:00",
      "fetched_at": "2026-10-06T11:00:05+00:00",
      "provider": "newsapi"
    }
  ],
  "total": 1,
  "limit": 50,
  "offset": 0
}
```

Examples: `GET /api/news`, `GET /api/news?limit=20&offset=40`,
`GET /api/news?category=technology&limit=10`.

**Errors:** `422` for invalid `limit`/`offset` (FastAPI validation);
`500 {"error": "...", "code": "INTERNAL_ERROR"}` if the DB query fails.

**422 body:** `{"detail":[{"loc":["query","limit"],"msg":"...","type":"..."}]}`

---

### `GET /api/news/{id}` — single article

`{id}` = 16-char lowercase hex = first 16 chars of `SHA-256(article URL)`.
Deterministic — the same URL always yields the same ID, so links stay stable
across re-ingestion.

**200:** one Article object (shape below).

**404 (article not in the rolling window / never existed):**

```json
{"error": "article with id \"does-not-exist\" not found", "code": "NOT_FOUND"}
```

**Errors:** `404 NOT_FOUND`, `500 INTERNAL_ERROR`.

---

### `GET /api/stats` — counts + last ingestion

**Params:** none.

**Response (real example, right after server start with no providers configured):**

```json
{
  "total_articles": 6,
  "last_ingestion": {
    "provider": "",
    "total": 0,
    "inserted": 0,
    "skipped": 0,
    "deleted": 0,
    "parsed": 0,
    "parse_failed": 0,
    "source_time": "2026-10-06T22:37:38Z"
  }
}
```

- `last_ingestion` is present only after at least one ingestion run has
  completed (including a no-op run). Before the first run completes, the
  response is only `{"total_articles": N}`.
- `provider`: which provider won the fallback chain (`"newsapi"`, `"gnews"`,
  `"newsdata"`, `"webfetch"`, `"sample"` in sample mode, `""` if all failed).
- `total` fetched → `inserted` stored → `parsed` LLM-processed;
  `skipped`/`parse_failed` surface partial failures (they never crash a run).

**Errors:** `500 INTERNAL_ERROR` (DB count failed).

---

### `POST /api/ingest` — trigger a run (admin/internal use)

Triggers one full ingestion cycle **synchronously** and returns its result:

```
news provider fetch (or samples) → normalize → MAX_ARTICLES cap → URL dedup
→ upsert into Turso → per-article LLMPing parse → llm_* fields filled
```

- Request: no body, no parameters.
- Blocks until the run finishes or `INGEST_TIMEOUT` (default 900 s) elapses.
- Each run re-parses its stored articles through LLMPing sequentially, so this
  call can take minutes. Real provider quota is consumed too.
- The backend also ingests automatically at startup and every
  `INGEST_INTERVAL` (8 h) — the UI does not need this endpoint to see fresh
  data; polling `GET /api/news` is enough.
- **Not for normal frontend usage:** it is unauthenticated and quota-consuming.
  Surface it only in an admin tool, if at all.

**200 response (real example values):**

```json
{
  "success": true,
  "result": {
    "provider": "newsapi",
    "total": 6,
    "inserted": 6,
    "skipped": 0,
    "deleted": 0,
    "parsed": 6,
    "parse_failed": 0,
    "source_time": "2026-10-06T22:45:19Z"
  }
}
```

**Errors:** `500 INGEST_TIMEOUT` (run exceeded `INGEST_TIMEOUT`);
`500 INGEST_ERROR` (run failed, e.g. DB unreachable).

---

## Article data model

Canonical shape for every article returned by `/api/news*`.

```typescript
interface Article {
  id: string;            // 16-char hex, SHA-256(URL)[:16]
  title: string;         // always present, non-empty
  url: string;           // always present (dedup key)
  published_at: string;  // ISO 8601 UTC — see note on format below
  fetched_at: string;    // ISO 8601 UTC
  provider: string;      // "newsapi" | "gnews" | "newsdata" | "webfetch"

  description?: string;  // optional — ABSENT (not null) when empty
  content?: string;
  image_url?: string;
  source?: string;
  author?: string;
  category?: string;

  llm_answer?: string;   // LLM Brain parse result — absent until parsed
  llm_provider?: string; // provider LLMPing reported using
  llm_model?: string;    // model LLMPing reported using
  llm_processed_at?: string;
}
```

**Field rules (all verified against the running service):**

- Optional fields are **omitted from the JSON when empty** — check
  `if ("image_url" in article)` / falsy checks before rendering, there is no
  `null`.
- `title`, `url`, `published_at`, `fetched_at`, `provider` are always present.
- `published_at`/`fetched_at` arrive as ISO 8601 UTC in **`+00:00` form**,
  e.g. `"2026-10-06T09:30:00+00:00"` (microseconds appear only when non-zero):
  `new Date(article.published_at)` parses directly. If a source timestamp is
  unparseable the backend stores "now" — the field is never missing.
- Same URL re-ingested → same `id`, raw fields refreshed, `llm_*` fields
  preserved.
- `llm_answer` is stored as received from the LLM Brain. With the shipped
  editorial prompt (`LLM_SYSTEM_PROMPT` in `.env`), it is a single strict JSON
  object — verified live on 6/6 sample articles:

  ```json
  {
    "title": "...",        // echoed from the article
    "url": "...",          // echoed, matches the row's url
    "source": "...",
    "author": "...",
    "category": "...",
    "published_at": "...", // echoed
    "summary": "2-3 sentence summary based only on the article",
    "key_points": ["up to 3 short factual strings"]
  }
  ```

  Treat it as best-effort (a different deployment prompt may change the
  shape): try `JSON.parse` and fall back to rendering the raw text.
- Articles older than `RETENTION_DAYS` (default 7) are deleted at the end of
  each run — a formerly valid `id` may legitimately become `404`.

---

## Internal Data Flow (for context — the UI only does the last step)

**Real mode (`SAMPLE_DATA=false` — default):**

```
News API (NewsAPI → GNews → NewsData.io → WebFetch, first success wins)
   ↓ provider normalization      (internal)
up to MAX_ARTICLES articles      (7, hard cap)
   ↓ LLMPing LLM Brain           (internal, per article, sequential)
parsed answer on the article row (llm_* fields)
   ↓
Turso database
   ↓
GET /api/news (this backend)
   ↓
Last247 UI
```

**Sample mode (`SAMPLE_DATA=true` — testing only):**

```
data/sample_news.json  (6 sample articles, no API keys)
   ↓ same normalization → cap → dedup → LLMPing → Turso → GET /api/news
```

- `SAMPLE_DATA=true` loads bundled sample data (2 NewsAPI + 2 GNews +
  2 NewsData.io entries) and runs it through the **same** pipeline — including
  real LLMPing calls. It is for integration testing; it does not represent
  production news.
- `SAMPLE_DATA=false` (default) uses the real configured providers.
- Either way the UI reads the latest rows from `GET /api/news` — no other
  signal is needed.

---

## What runs inside the backend only

| Component | What it does | Exposed to UI? |
|-----------|--------------|----------------|
| NewsAPI, GNews, NewsData.io, WebFetch | Fetch raw news (sequential fallback, first success wins) | **No** — never call these from the frontend; they're external APIs with credential-bearing requests |
| LLMPing (`POST https://llmping.onrender.com/chat`) LLM Brain | Parses each article with the configured system prompt; result stored in the row | **No** — internal service, credentials stay in backend env |
| Turso database | Storage/dedup/retention | **No** — access it only through this API |
| In-process scheduler | Startup run + every `INGEST_INTERVAL` | **No** |

Credentials (provider keys, Turso token, LLMPing token) live only in backend
`env` vars (`KEY`/`.env`) and never appear in API responses or logs.

---

## Automatic runner

- `INGEST_INTERVAL=8h` — ingestion at startup, then every 8 h (3 runs/day).
- `MAX_ARTICLES=7` — each run processes **up to** 7 articles (fetched count can
  be lower; e.g. the provider may return fewer than requested).
- Per run: fetch → normalize → cap at `MAX_ARTICLES` → dedup → store →
  one sequential LLMPing call per article → retention sweep.
- A failed article (LLM error) is logged and skipped — the run continues.

## Frontend integration example

```javascript
const API_BASE_URL = "http://localhost:8080"; // or your deployed URL

const response = await fetch(`${API_BASE_URL}/api/news?limit=20`);
if (!response.ok) {
  // 422 invalid params, 500 DB error
  throw new Error(`Backend error: ${response.status}`);
}

const data = await response.json();
// data.articles = Article[] (see data model above)
// data.total / data.offset -> pagination math
data.articles.forEach((a) => {
  // a.title, a.url, a.published_at (+00:00 ISO string) always present
  const image = a.image_url || "/placeholder.png"; // optional fields may be absent
  const link = a.url;                              // open the article directly
});
```

Single-article view: `GET /api/news/{id}` → expect `404 NOT_FOUND` JSON for
articles that aged out of the 7-day window — render a "not found" state.

## UI Contract

**The UI is responsible for:**
- Calling only this backend (`/api/news`, `/api/news/{id}`, `/api/stats`)
- Rendering articles, opening `article.url`
- Loading, empty (`articles: []`), and error states (422/500/404 + network)
- Treating optional fields as possibly absent

**The backend is responsible for:**
- Fetching news from providers (with fallback) — never asked by the UI
- Normalization, `MAX_ARTICLES` selection, URL dedup, stable IDs
- LLM parsing via internal LLMPing
- Turso storage + 7-day retention + scheduled/automatic ingestion
