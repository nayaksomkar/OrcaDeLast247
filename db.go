package main

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"fmt"
	"time"

	"github.com/tursodatabase/libsql-client-go/libsql"
)

// ArticleID generates a stable, deterministic ID from an article URL: the
// first 16 hex characters of the URL's SHA-256 hash (64 bits).
//
// Deterministic IDs mean the same story re-stored later maps to the same
// primary key, so upserts refresh the existing row instead of duplicating it.
func ArticleID(url string) string {
	h := sha256.Sum256([]byte(url))
	return hex.EncodeToString(h[:])[:16]
}

// OpenDB opens a connection to the Turso database (remote libSQL or a local
// SQLite file) and verifies it with a ping before returning.
func OpenDB(ctx context.Context, cfg *Config) (*sql.DB, error) {
	var opts []libsql.Option
	// Auth token is only required for remote Turso databases; local
	// file: URLs work without one.
	if cfg.TursoToken != "" {
		opts = append(opts, libsql.WithAuthToken(cfg.TursoToken))
	}
	connector, err := libsql.NewConnector(cfg.TursoURL, opts...)
	if err != nil {
		return nil, fmt.Errorf("create connector: %w", err)
	}
	db := sql.OpenDB(connector)

	// Fail fast on a bad URL/token here instead of erroring mid-ingestion.
	if err := db.PingContext(ctx); err != nil {
		return nil, fmt.Errorf("ping database: %w", err)
	}
	return db, nil
}

// InitDB creates the `news` table and indexes if they do not exist.
// Idempotent — safe to call on every run.
func InitDB(ctx context.Context, db *sql.DB) error {
	schema := `
CREATE TABLE IF NOT EXISTS news (
    id           TEXT PRIMARY KEY,       -- stable SHA-256 hash of the URL
    title        TEXT NOT NULL,
    description  TEXT,
    content      TEXT,
    url          TEXT NOT NULL UNIQUE,   -- UNIQUE prevents duplicate stories
    image_url    TEXT,
    source       TEXT,
    author       TEXT,
    category     TEXT,
    published_at TEXT NOT NULL,          -- ISO 8601
    fetched_at   TEXT NOT NULL,          -- ISO 8601, when this run stored it
    provider     TEXT                    -- which provider supplied the article
);
CREATE INDEX IF NOT EXISTS idx_news_published_at ON news (published_at DESC);
CREATE INDEX IF NOT EXISTS idx_news_url ON news (url);
`
	_, err := db.ExecContext(ctx, schema)
	return err
}

// UpsertArticle inserts an article, or refreshes the stored copy when the
// URL already exists (deduplication). published_at/fetched_at are stored as
// UTC RFC 3339 strings so the string comparison in DeleteStaleArticles is
// chronologically correct.
func UpsertArticle(ctx context.Context, db *sql.DB, a *Article) error {
	query := `
INSERT INTO news (id, title, description, content, url, image_url, source, author, category, published_at, fetched_at, provider)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(url) DO UPDATE SET
    title = excluded.title,
    description = excluded.description,
    content = excluded.content,
    image_url = excluded.image_url,
    source = excluded.source,
    author = excluded.author,
    category = excluded.category,
    published_at = excluded.published_at,
    fetched_at = excluded.fetched_at,
    provider = excluded.provider
`
	args := []any{
		a.ID, a.Title, a.Description, a.Content, a.URL,
		a.ImageURL, a.Source, a.Author, a.Category,
		a.PublishedAt.UTC().Format(time.RFC3339Nano),
		a.FetchedAt.UTC().Format(time.RFC3339Nano),
		a.Provider,
	}
	_, err := db.ExecContext(ctx, query, args...)
	if err != nil {
		return fmt.Errorf("insert article %s: %w", a.URL, err)
	}
	return nil
}

// DeleteStaleArticles removes articles published outside the retention
// window (older than retentionDays). Returns the number of rows deleted.
func DeleteStaleArticles(ctx context.Context, db *sql.DB, retentionDays int) (int, error) {
	// Cutoff is a UTC RFC 3339 string; stored timestamps use the same
	// format, so lexicographic comparison is chronological comparison.
	cutoff := time.Now().AddDate(0, 0, -retentionDays).UTC().Format(time.RFC3339Nano)
	res, err := db.ExecContext(ctx, "DELETE FROM news WHERE published_at < ?", cutoff)
	if err != nil {
		return 0, fmt.Errorf("delete stale articles: %w", err)
	}
	affected, err := res.RowsAffected()
	if err != nil {
		return 0, fmt.Errorf("rows affected: %w", err)
	}
	return int(affected), nil
}

// CountArticles returns the total number of stored articles.
func CountArticles(ctx context.Context, db *sql.DB) (int, error) {
	var count int
	err := db.QueryRowContext(ctx, "SELECT COUNT(*) FROM news").Scan(&count)
	if err != nil {
		return 0, err
	}
	return count, nil
}
