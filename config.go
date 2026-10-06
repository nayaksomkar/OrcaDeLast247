package main

import (
	"fmt"
	"os"
	"strconv"

	"github.com/joho/godotenv"
)

// Config holds all runtime configuration for the ingestion service.
// Values come from environment variables, optionally pre-loaded from a
// .env file in the working directory (see .env.example).
type Config struct {
	// Database (Turso / libSQL). The token is only needed for remote
	// databases; local file: URLs work without one.
	TursoURL      string // e.g. libsql://name.turso.io or file:./news.db
	TursoToken    string // empty for local file databases
	RetentionDays int    // rolling window in days; older articles are deleted each run

	// Provider API keys (sequential fallback order: NewsAPI → GNews →
	// NewsData.io → WebFetch). At least one provider must be configured
	// for a run to fetch anything.
	NewsAPIKey     string
	GNewsAPIKey    string
	NewsDataAPIKey string
	WebFetchAPIKey string
	WebFetchAPIURL string // base endpoint for the WebFetch fallback provider

	// Fetch options.
	Language    string // article language filter, e.g. "en"
	MaxArticles int    // max articles fetched per provider per run
}

// LoadConfig reads .env (if present) and environment variables, applies
// defaults, and validates required fields. It returns an error only when
// TURSO_DATABASE_URL is missing — every other field has a usable default.
func LoadConfig() (*Config, error) {
	// Best-effort: ignore the error when no .env file exists.
	// godotenv never overrides variables already set in the real
	// environment, so explicit env vars always win over .env values.
	_ = godotenv.Load(".env")

	cfg := &Config{
		TursoURL:       os.Getenv("TURSO_DATABASE_URL"),
		TursoToken:     os.Getenv("TURSO_AUTH_TOKEN"),
		NewsAPIKey:     os.Getenv("NEWS_API_KEY"),
		GNewsAPIKey:    os.Getenv("GNEWS_API_KEY"),
		NewsDataAPIKey: os.Getenv("NEWS_DATA_API_KEY"),
		WebFetchAPIKey: os.Getenv("WEBFETCH_API_KEY"),
		WebFetchAPIURL: os.Getenv("WEBFETCH_API_URL"),
		Language:       os.Getenv("NEWS_LANGUAGE"),
		MaxArticles:    50,
		RetentionDays:  7,
	}

	if cfg.TursoURL == "" {
		return nil, fmt.Errorf("TURSO_DATABASE_URL is required")
	}

	// Optional retention override via env. Invalid values are ignored and
	// the default (7) stays in effect — a bad env var never crashes a run.
	if rd := os.Getenv("RETENTION_DAYS"); rd != "" {
		if val, err := strconv.Atoi(rd); err == nil && val > 0 {
			cfg.RetentionDays = val
		}
	}

	// Optional max-articles override. Same policy: ignore invalid values.
	if ma := os.Getenv("MAX_ARTICLES"); ma != "" {
		if val, err := strconv.Atoi(ma); err == nil && val > 0 {
			cfg.MaxArticles = val
		}
	}

	// Defaults for optional fields.
	if cfg.Language == "" {
		cfg.Language = "en"
	}

	return cfg, nil
}
