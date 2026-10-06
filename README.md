# Last247 News API Service

A Go HTTP microservice that fetches news from external providers (NewsAPI, GNews, NewsData.io, WebFetch), normalizes and deduplicates articles into a Turso database, and serves them via a JSON HTTP API for the Last247 Next.js frontend.

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
Go Ingestion Service  (this repo — HTTP microservice)
   ↓
Turso `news` table
   ↓
Next.js Frontend  (GET /api/news via this service's HTTP API)
   ↓
Browser UI
```

The service runs as a long-lived HTTP server. It ingests news on startup and then periodically (every 6 hours by default). The Next.js frontend queries this service's API to read stored articles — it never calls news providers directly.

### Sequential fallback

The service tries providers in order and stops on the first one that returns usable articles. It never calls all providers simultaneously — it only moves to the next provider when the previous one fails, times out, is rate-limited, or returns empty data.

| Order | Provider    | Env var          | Endpoint                                |
|-------|-------------|------------------|-----------------------------------------|
| 1     | NewsAPI     | `NEWS_API_KEY`   | `https://newsapi.org/v2/everything`     |
| 2     | GNews       | `GNEWS_API_KEY`  | `https://gnews.io/v4/api/top-headlines` |
| 3     | NewsData.io | `NEWS_DATA_API_KEY` | `https://newsdata.io/api/1/news`     |
| 4     | WebFetch    | `WEBFETCH_API_URL` + `WEBFETCH_API_KEY` | configurable              |

A provider is only attempted if it has the required API key (or, for WebFetch, its base URL). If a provider returns an error, an HTTP non-2xx status, or zero articles, the service moves to the next one.

---

## API Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/health` | Health liveness/readiness check. |
| `GET` | `/api/news` | Paginated list of articles with optional `category` and `source` filters. |
| `GET` | `/api/news/{id}` | Get a single article by its deterministic ID. |
| `POST` | `/api/ingest` | Trigger a manual ingestion run. |
| `GET` | `/api/stats` | Get total article count and last ingestion result. |

See [UI_API_INTEGRATION.md](./UI_API_INTEGRATION.md) for full endpoint documentation, request/response schemas, and `fetch()` examples.

---

## Database Schema

```sql
CREATE TABLE IF NOT EXISTS news (
    id           TEXT PRIMARY KEY,       -- stable SHA-256 hash of the URL (16 hex chars)
    title        TEXT NOT NULL,
    description  TEXT,
    content      TEXT,
    url          TEXT NOT NULL UNIQUE,   -- UNIQUE prevents duplicate stories
    image_url    TEXT,
    source       TEXT,
    author       TEXT,
    category     TEXT,
    published_at TEXT NOT NULL,          -- ISO 8601 (UTC RFC 3339)
    fetched_at   TEXT NOT NULL,          -- ISO 8601 (UTC RFC 3339)
    provider     TEXT
);

CREATE INDEX IF NOT EXISTS idx_news_published_at ON news (published_at DESC);
CREATE INDEX IF NOT EXISTS idx_news_url          ON news (url);
```

### Seven-day retention

Each ingestion run deletes articles whose `published_at` is older than `RETENTION_DAYS` (default 7). The database therefore always represents the most recent rolling seven-day window of news.

### Deduplication

The `url` column has a `UNIQUE` constraint. When the same story appears again, `ON CONFLICT(url) DO UPDATE` refreshes the stored fields instead of inserting a duplicate. The `id` column is a deterministic SHA-256 hash of the URL.

---

## Prerequisites

- **Go 1.26+** (the module declares `go 1.26.0`; the toolchain auto-downloads if needed). Run `go version` to check.
- **Docker** (optional, for containerized deployment).
- A **Turso database** (remote or local file). For local development, use `file:./news.db`.
- At least one **news provider API key** for ingestion to fetch anything.

---

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `TURSO_DATABASE_URL` | **Yes** | — | Turso connection string (`libsql://name.turso.io`) or local file (`file:./news.db`). |
| `TURSO_AUTH_TOKEN` | Remote only | `""` | Turso auth token. Leave empty for local files. |
| `PORT` | No | `8080` | HTTP listen port. Render provides this dynamically. |
| `INGEST_INTERVAL` | No | `6h` | Background ingestion cadence. |
| `INGEST_TIMEOUT` | No | `120s` | Max duration for one ingestion run. |
| `RETENTION_DAYS` | No | `7` | Rolling retention window in days. |
| `NEWS_API_KEY` | One of four | `""` | NewsAPI key (provider #1). |
| `GNEWS_API_KEY` | One of four | `""` | GNews key (provider #2). |
| `NEWS_DATA_API_KEY` | One of four | `""` | NewsData.io key (provider #3). |
| `WEBFETCH_API_URL` | One of four | `""` | WebFetch base URL (provider #4). |
| `WEBFETCH_API_KEY` | Conditional | `""` | WebFetch API key (optional). |
| `NEWS_LANGUAGE` | No | `en` | Article language filter. |
| `MAX_ARTICLES` | No | `50` | Max articles per provider per run. |
| `CORS_ALLOW_ORIGINS` | No | `*` | Comma-separated allowed CORS origins. |

---

## Local Development

### Setup

```bash
# 1. Initialize / download dependencies
go mod download

# 2. Configure environment
cp .env.example .env
# Then edit .env:
#   TURSO_DATABASE_URL=file:./news.db      # local SQLite file
#   NEWS_API_KEY=your-key                  # at least one provider key
#   # optional: GNEWS_API_KEY, NEWS_DATA_API_KEY, WEBFETCH_API_URL
```

### Running

```bash
# Run the HTTP server (default port 8080)
go run .

# Run with custom port
PORT=9090 go run .

# Build a binary
go build -o last247 .
./last247
```

Once running, the server:
- Starts an initial ingestion run on startup.
- Serves HTTP on `0.0.0.0:<PORT>`.
- Runs subsequent ingestion runs every `INGEST_INTERVAL` (default 6h).

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
# Full suite (root package + providers), race detector on
go test -race -shuffle=on -count=1 ./...

# Verbose output with per-test names
go test -v -count=1 ./...

# Coverage report
go test -coverprofile=cover.out ./... && go tool cover -func=cover.out
```

The suite is self-contained: it uses throwaway SQLite files (`t.TempDir()`) and `httptest` fakes for provider HTTP endpoints — no network access and no real provider API keys required.

---

## Docker

### Build

```bash
docker build -t last247 .
```

The Dockerfile uses a multi-stage build:
- **Builder stage**: `golang:1.26-alpine` — compiles a static binary with `CGO_ENABLED=0`.
- **Runtime stage**: `alpine:3.20` — runs the binary with just `ca-certificates`.

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

For a remote Turso database:

```bash
docker run -d \
  -p 8080:8080 \
  -e TURSO_DATABASE_URL=libsql://your-db.turso.io \
  -e TURSO_AUTH_TOKEN=your-token \
  -e NEWS_API_KEY=your-key \
  --name last247 \
  last247
```

### Docker Compose (optional)

```yaml
# docker-compose.yml
version: "3.8"
services:
  api:
    build: .
    ports:
      - "8080:8080"
    environment:
      - TURSO_DATABASE_URL=file:./news.db
      - REDIS_URL=redis://redis:6379
      - CORS_ALLOW_ORIGINS=http://localhost:3000
    volumes:
      - news-data:/app
    depends_on:
      - redis

volumes:
  news-data:
```

---

## Render Deployment

### `render.yaml`

```yaml
services:
  - type: web
    name: last247-api
    runtime: docker
    plan: free
    dockerfilePath: ./Dockerfile
    healthCheckPath: /health
    envVars:
      - key: TURSO_DATABASE_URL
        value: libsql://your-db.turso.io
      - key: TURSO_AUTH_TOKEN
        value: your-token
      - key: NEWS_API_KEY
        value: your-key
      # optional: GNEWS_API_KEY, NEWS_DATA_API_KEY, WEBFETCH_API_URL, WEBFETCH_API_KEY
      - key: CORS_ALLOW_ORIGINS
        value: https://your-frontend.vercel.app
      - key: INGEST_INTERVAL
        value: 6h
      - key: RETENTION_DAYS
        value: 7
```

### Manual Render setup

1. **Create a Web Service** on [render.com](https://render.com).
2. **Environment**: Docker.
3. **Build Command**: `docker build -t last247 .`
4. **Start Command**: Leave empty (the Dockerfile's `CMD` handles it).
5. **Health Check Path**: `/health`
6. **Port**: Render sets `PORT` automatically — the service reads it from the environment.
7. Add the environment variables listed above under **Environment → Environment Variables**.

Render provides the `PORT` environment variable automatically. The service binds to `0.0.0.0:<PORT>`, which is required for Render's container networking.

### Deploy via GitHub

1. Push to your `main` branch.
2. Connect the repository to Render as a Web Service.
3. Render auto-builds and deploys on each push.

---

## Data Contract

The `Article` struct (`article.go`) is the canonical data shape for all API responses. It matches the `news` table columns exactly:

```typescript
interface Article {
  id: string;            // 16-char hex, SHA-256(URL)[:16], deterministic
  title: string;         // always present, non-empty
  description?: string;  // may be absent (empty → omitted by omitempty)
  content?: string;      // may be absent
  url: string;           // always present, non-empty, UNIQUE
  image_url?: string;    // may be absent
  source?: string;       // publisher name, may be absent
  author?: string;       // may be absent
  category?: string;     // single category, may be absent
  published_at: string;  // ISO 8601 / RFC 3339, UTC, always present
  fetched_at: string;   // ISO 8601 / RFC 3339, UTC, always present
  provider: string;     // "newsapi" | "gnews" | "newsdata" | "webfetch"
}
```

See [UI_API_INTEGRATION.md](./UI_API_INTEGRATION.md) for full details including `fetch()` examples.

---

## Production Notes

- The service is **stateless** beyond the database connection. Multiple instances can run behind a load balancer — each will independently attempt ingestion (idempotent due to `ON CONFLICT` dedup).
- The `/health` endpoint does not depend on the database — it checks process liveness only. A 200 from `/health` means the HTTP server is running, not that the database is reachable.
- No authentication is implemented. For production, place behind a reverse proxy (e.g., Cloudflare, Nginx) or Render's built-in networking controls.
- No background workers, queues, or AI functionality are used. Ingestion runs in-process via a `time.Ticker`.
- The binary is ~12 MB (stripped, CGO_ENABLED=0). Docker image is ~12 MB total.