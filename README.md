# Last247 News API Service

A Python (FastAPI) HTTP service that fetches news from external providers (NewsAPI, GNews, NewsData.io, WebFetch), normalizes and deduplicates articles into a Turso database, parses each article with the external **LLMPing** LLM Brain, and serves the processed articles via a JSON HTTP API for the Last247 Next.js frontend.

OrcaDeLast247 never runs a model itself. All inference happens in the external LLMPing service (`https://llmping.onrender.com`) — this project only orchestrates: fetch → store → send system prompt + article to LLMPing → store the parsed answer.

## Architecture

```text
NewsAPI
   ↓ failure / empty?
GNews
   ↓ failure / empty?
NewsData.io
   ↓ failure / empty?
WebFetch API
   ↓
Last247 Ingestion (this repo — FastAPI service)
   ├─ normalize + dedup → Turso `news` table
   ├─ per article (max 7/run): system prompt + article data
   │        ↓ POST /chat
   │    LLMPing LLM Brain (external — provider/model selection + inference)
   │        ↓ parsed answer
   └─ parsed answer stored on the article row (llm_* columns)
   ↓
Next.js Frontend  (GET /api/news via this service's HTTP API)
   ↓
Browser UI
```

The service runs as a long-lived HTTP server. It ingests on startup and then every 8 hours (`INGEST_INTERVAL`). Each run: fetch ≤ `MAX_ARTICLES` (7) articles, store them, then send each one to LLMPing sequentially and persist the parsed answer. **3 runs/day × 7 articles = 21 LLM parse calls/day.** No queue, broker, or worker system — just the existing in-process scheduler.

### Sequential provider fallback

The service tries providers in order and stops on the first one that returns usable articles. It never calls all providers simultaneously.

| Order | Provider    | Env var          | Endpoint                                |
|-------|-------------|------------------|-----------------------------------------|
| 1     | NewsAPI     | `NEWS_API_KEY`   | `https://newsapi.org/v2/everything`     |
| 2     | GNews       | `GNEWS_API_KEY`  | `https://gnews.io/v4/api/top-headlines` |
| 3     | NewsData.io | `NEWS_DATA_API_KEY` | `https://newsdata.io/api/1/news`     |
| 4     | WebFetch    | `WEBFETCH_API_URL` + `WEBFETCH_API_KEY` | configurable              |

A provider is only attempted if it has the required API key (or, for WebFetch, its base URL). If a provider errors, returns non-2xx, or zero articles, the next one is tried.

### LLMPing parse phase (per article)

After the upsert loop, each stored article is processed **sequentially**:

1. Build the prompt: `=== SYSTEM INSTRUCTIONS ===` (from `LLM_SYSTEM_PROMPT`, or the built-in placeholder) + `=== ARTICLE DATA ===` (title/url/source/author/category/published_at/description/content from the stored `Article`).
2. `POST {LLMPING_BASE_URL}{LLMPING_CHAT_PATH}` with `{"query": prompt}`.
3. Store `answer` (as received) plus the reported `provider`/`model` on the article row (`llm_answer`, `llm_provider`, `llm_model`, `llm_processed_at`).
4. A failed call is logged and skipped — the article stays stored with `llm_answer = NULL` and the run continues to the next article. Nothing is fabricated.

The system prompt is an instruction layer, kept in its own section — it is never mixed into article content. The final editorial prompt is supplied via the `LLM_SYSTEM_PROMPT` env var; the in-code default is a clearly marked placeholder.

---

## API Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/health` | Health liveness check. |
| `GET` | `/api/news` | Paginated list of articles (with optional `category`/`source` filters). Includes the LLM parse fields when available. |
| `GET` | `/api/news/{id}` | Get a single article by its deterministic ID. |
| `POST` | `/api/ingest` | Trigger a manual ingestion + LLM parse run. |
| `GET` | `/api/stats` | Total article count and last ingestion result (incl. `parsed`/`parse_failed`). |

See [UI_API_INTEGRATION.md](./UI_API_INTEGRATION.md) for full endpoint documentation, request/response schemas, and `fetch()` examples.

---

## Database Schema

```sql
CREATE TABLE IF NOT EXISTS news (
    id               TEXT PRIMARY KEY,  -- stable SHA-256 hash of the URL (16 hex chars)
    title            TEXT NOT NULL,
    description      TEXT,
    content          TEXT,
    url              TEXT NOT NULL UNIQUE,  -- UNIQUE prevents duplicate stories
    image_url        TEXT,
    source           TEXT,
    author           TEXT,
    category         TEXT,
    published_at     TEXT NOT NULL,     -- ISO 8601 (UTC RFC 3339)
    fetched_at       TEXT NOT NULL,     -- ISO 8601 (UTC RFC 3339)
    provider         TEXT,
    -- LLM parse result (nullable until the article has been processed)
    llm_answer       TEXT,              -- parsed answer from the LLM Brain, as received
    llm_provider     TEXT,              -- LLMPing-reported provider used for the parse
    llm_model        TEXT,              -- LLMPing-reported model used for the parse
    llm_processed_at TEXT               -- ISO 8601 UTC, when the parse was stored
);

CREATE INDEX IF NOT EXISTS idx_news_published_at ON news (published_at DESC);
CREATE INDEX IF NOT EXISTS idx_news_url          ON news (url);
```

Existing databases get the four `llm_*` columns transparently — `init_db()` runs an idempotent `ALTER TABLE ADD COLUMN` on every startup. `upsert_article()` never touches the `llm_*` columns, so re-ingesting the same URL cannot wipe a stored parse.

### Seven-day retention

Each ingestion run deletes articles whose `published_at` is older than `RETENTION_DAYS` (default 7). The database therefore always represents the most recent rolling seven-day window of news.

### Deduplication

The `url` column has a `UNIQUE` constraint. When the same story appears again, `ON CONFLICT(url) DO UPDATE` refreshes the stored raw fields instead of inserting a duplicate. The `id` column is a deterministic SHA-256 hash of the URL.

---

## Prerequisites

- **Python 3.11+** (`python --version`).
- A **Turso database** (remote or local file). For local development, use `file:./news.db`.
- At least one **news provider API key** for ingestion to fetch anything.
- The **LLMPing service** must be reachable (defaults to `https://llmping.onrender.com`).

---

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `TURSO_DATABASE_URL` | **Yes** | — | Turso connection string (`libsql://name.turso.io`) or local file (`file:./news.db`). |
| `TURSO_AUTH_TOKEN` | Remote only | `""` | Turso auth token. Leave empty for local files. |
| `PORT` | No | `8080` | HTTP listen port. Render provides this dynamically. |
| `INGEST_INTERVAL` | No | `8h` | Background ingestion cadence (3 runs/day). |
| `INGEST_TIMEOUT` | No | `900s` | Max duration for one run — must cover the fetch plus up to 7 sequential LLMPing calls. |
| `RETENTION_DAYS` | No | `7` | Rolling retention window in days. |
| `NEWS_API_KEY` | One of four | `""` | NewsAPI key (provider #1). |
| `GNEWS_API_KEY` | One of four | `""` | GNews key (provider #2). |
| `NEWS_DATA_API_KEY` | One of four | `""` | NewsData.io key (provider #3). |
| `WEBFETCH_API_URL` | One of four | `""` | WebFetch base URL (provider #4). |
| `WEBFETCH_API_KEY` | Conditional | `""` | WebFetch API key (optional). |
| `NEWS_LANGUAGE` | No | `en` | Article language filter. |
| `MAX_ARTICLES` | No | `7` | Max articles fetched (and LLM-parsed) per run. |
| `CORS_ALLOW_ORIGINS` | No | `*` | Comma-separated allowed CORS origins. |
| `LLMPING_BASE_URL` | No | `https://llmping.onrender.com` | LLMPing service base URL. |
| `LLMPING_CHAT_PATH` | No | `/chat` | Chat endpoint path appended to the base URL. |
| `LLMPING_TIMEOUT` | No | `60s` | Max duration of one LLMPing `/chat` call. |
| `LLMPING_API_TOKEN` | No | `""` | Optional Bearer token — sent only when set (LLMPing currently requires none). |
| `LLM_SYSTEM_PROMPT` | No | (placeholder) | System instructions sent with every article. The final editorial prompt goes here. |
| `LLM_MAX_CONTENT_CHARS` | No | `4000` | Max characters of description/content included in a prompt. |

Do not put secrets in source files — use `.env` (gitignored) or the platform's env-var UI.

---

## Local Development

### Setup

```bash
# 1. Install dependencies — either with uv (creates/updates .venv, respects uv.lock):
uv sync
#    ...or plain pip:
pip install -r requirements.txt

# 2. Configure environment
cp .env.example .env
# Then edit .env:
#   TURSO_DATABASE_URL=file:./news.db      # local SQLite file
#   NEWS_API_KEY=your-key                  # at least one provider key
#   LLM_SYSTEM_PROMPT=...                  # optional: the real editorial prompt
```

### Running

```bash
# Run the HTTP server (default port 8080; ingests immediately, then every 8h)
python main.py

# or with uv (uses the project .venv):
uv run python main.py
```

### Available endpoints (local)

```
GET  http://localhost:8080/health
GET  http://localhost:8080/api/news
GET  http://localhost:8080/api/news/{id}
POST http://localhost:8080/api/ingest
GET  http://localhost:8080/api/stats
```

### Running tests

```bash
# Full suite — self-contained: in-memory SQLite + respx/httpx mocks,
# no network access and no real API keys required
uv run pytest
# (or: pytest -v with a pip-installed environment)
```

### Live verification scripts (hit the real services)

```bash
uv run python scripts/test_providers_live.py   # one small real request per configured provider
uv run python scripts/test_turso_live.py       # Turso write/read/dedup/retention lifecycle
uv run python scripts/test_llmping_live.py     # {"query": "Hello"} + one real article prompt
TURSO_DATABASE_URL=file:./news.db MAX_ARTICLES=7 uv run python scripts/test_one_cycle_live.py
```

---

## Docker

### Build

```bash
docker build -t last247 .
```

### Run

```bash
docker run -d \
  -p 8080:8080 \
  -e TURSO_DATABASE_URL=file:./news.db \
  -e NEWS_API_KEY=your-key \
  -e CORS_ALLOW_ORIGINS=http://localhost:3000 \
  --name last247 \
  last247
```

For a remote Turso database, add `-e TURSO_AUTH_TOKEN=your-token` and use the `libsql://` URL.

### Docker Compose (optional)

```bash
docker compose up -d
```

`docker-compose.yml` starts the single `api` service with the env vars from `.env`.

---

## Render Deployment

1. **Create a Web Service** on [render.com](https://render.com).
2. **Environment**: Docker (the repo's `Dockerfile` builds the Python image).
3. **Health Check Path**: `/health`
4. **Port**: Render sets `PORT` automatically — the service reads it from the environment.
5. Add the environment variables listed above under **Environment → Environment Variables** (`TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN`, a news provider key, and optionally `LLM_SYSTEM_PROMPT`).

Render provides `PORT` automatically. The service binds to `0.0.0.0:<PORT>`, which is required for Render's container networking.

### Deploy via GitHub

1. Push to your `main` branch.
2. Connect the repository to Render as a Web Service.
3. Render auto-builds and deploys on each push.

---

## Data Contract

The `Article` model (`models.py`) is the canonical data shape for all API responses. It matches the `news` table columns exactly:

```typescript
interface Article {
  id: string;            // 16-char hex, SHA-256(URL)[:16], deterministic
  title: string;         // always present, non-empty
  description?: string;  // may be absent
  content?: string;      // may be absent
  url: string;           // always present, non-empty, UNIQUE
  image_url?: string;    // may be absent
  source?: string;       // publisher name, may be absent
  author?: string;       // may be absent
  category?: string;     // single category, may be absent
  published_at: string;  // ISO 8601 / RFC 3339, UTC, always present
  fetched_at: string;    // ISO 8601 / RFC 3339, UTC, always present
  provider: string;      // "newsapi" | "gnews" | "newsdata" | "webfetch"
  llm_answer?: string;   // parsed answer from the LLM Brain, as received
  llm_provider?: string; // provider LLMPing used for the parse
  llm_model?: string;    // model LLMPing used for the parse
  llm_processed_at?: string; // ISO 8601 UTC — when the parse was stored
}
```

The `llm_*` fields are `null`/absent until the article has been processed by the LLMPing phase.

See [UI_API_INTEGRATION.md](./UI_API_INTEGRATION.md) for full details including `fetch()` examples.

---

## Production Notes

- The service is **stateless** beyond the database connection. Multiple instances can run behind a load balancer — each will independently attempt ingestion (idempotent due to `ON CONFLICT` dedup and the `UPDATE`-based parse store).
- The `/health` endpoint does not depend on the database — it checks process liveness only.
- No authentication is implemented. For production, place behind a reverse proxy (e.g., Cloudflare, Nginx) or Render's built-in networking controls.
- Ingestion runs in-process via an `asyncio` background task (`INGEST_INTERVAL`). Per-article LLM failures never abort a run; failed articles simply remain un-parsed until the next run refreshes them.
- Workload is bounded by design: 3 runs/day × 7 articles = 21 LLMPing calls/day.
