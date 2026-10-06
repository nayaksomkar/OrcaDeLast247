package main

import (
	"testing"
)

// isolateEnv neutralizes any values coming from a developer's local .env
// file. godotenv.Load never overrides variables already present in the
// environment, and t.Setenv makes a variable present (even when empty), so
// setting "" is equivalent to "unset" for LoadConfig's purposes.
func isolateEnv(t *testing.T) {
	t.Helper()
	for _, key := range []string{
		"TURSO_DATABASE_URL", "TURSO_AUTH_TOKEN",
		"NEWS_API_KEY", "GNEWS_API_KEY", "NEWS_DATA_API_KEY",
		"WEBFETCH_API_KEY", "WEBFETCH_API_URL",
		"RETENTION_DAYS", "MAX_ARTICLES", "NEWS_LANGUAGE",
	} {
		t.Setenv(key, "")
	}
}

func Test_LoadConfig_errors_when_TURSO_DATABASE_URL_missing(t *testing.T) {
	// Given — no database URL configured
	isolateEnv(t)

	// When
	cfg, err := LoadConfig()

	// Then
	if err == nil {
		t.Fatalf("expected error for missing TURSO_DATABASE_URL, got cfg=%+v", cfg)
	}
}

func Test_LoadConfig_applies_defaults(t *testing.T) {
	// Given — only the required variable is set
	isolateEnv(t)
	t.Setenv("TURSO_DATABASE_URL", "file:./news.db")

	// When
	cfg, err := LoadConfig()

	// Then
	if err != nil {
		t.Fatalf("LoadConfig: %v", err)
	}
	if cfg.RetentionDays != 7 {
		t.Errorf("default RetentionDays = %d, want 7", cfg.RetentionDays)
	}
	if cfg.MaxArticles != 50 {
		t.Errorf("default MaxArticles = %d, want 50", cfg.MaxArticles)
	}
	if cfg.Language != "en" {
		t.Errorf("default Language = %q, want %q", cfg.Language, "en")
	}
}

func Test_LoadConfig_env_overrides_win_over_defaults(t *testing.T) {
	// Given — non-default values in the environment (deliberately distinct
	// from the defaults, so the test fails if the override is ignored)
	isolateEnv(t)
	t.Setenv("TURSO_DATABASE_URL", "file:./news.db")
	t.Setenv("RETENTION_DAYS", "14")
	t.Setenv("MAX_ARTICLES", "10")
	t.Setenv("NEWS_LANGUAGE", "de")

	// When
	cfg, err := LoadConfig()

	// Then
	if err != nil {
		t.Fatalf("LoadConfig: %v", err)
	}
	if cfg.RetentionDays != 14 {
		t.Errorf("RetentionDays = %d, want 14", cfg.RetentionDays)
	}
	if cfg.MaxArticles != 10 {
		t.Errorf("MaxArticles = %d, want 10", cfg.MaxArticles)
	}
	if cfg.Language != "de" {
		t.Errorf("Language = %q, want %q", cfg.Language, "de")
	}
}

func Test_LoadConfig_ignores_invalid_numeric_env_values(t *testing.T) {
	// Given — invalid numbers must fall back to defaults, not error or crash
	isolateEnv(t)
	t.Setenv("TURSO_DATABASE_URL", "file:./news.db")
	t.Setenv("RETENTION_DAYS", "not-a-number")
	t.Setenv("MAX_ARTICLES", "-5")

	// When
	cfg, err := LoadConfig()

	// Then
	if err != nil {
		t.Fatalf("LoadConfig: %v", err)
	}
	if cfg.RetentionDays != 7 {
		t.Errorf("RetentionDays = %d, want default 7", cfg.RetentionDays)
	}
	if cfg.MaxArticles != 50 {
		t.Errorf("MaxArticles = %d, want default 50", cfg.MaxArticles)
	}
}

func Test_LoadConfig_reads_provider_keys_and_webfetch_url(t *testing.T) {
	// Given — all provider credentials configured
	isolateEnv(t)
	t.Setenv("TURSO_DATABASE_URL", "file:./news.db")
	t.Setenv("NEWS_API_KEY", "k1")
	t.Setenv("GNEWS_API_KEY", "k2")
	t.Setenv("NEWS_DATA_API_KEY", "k3")
	t.Setenv("WEBFETCH_API_KEY", "k4")
	t.Setenv("WEBFETCH_API_URL", "https://example.com/fetch")

	// When
	cfg, err := LoadConfig()

	// Then — every key maps to its config field
	if err != nil {
		t.Fatalf("LoadConfig: %v", err)
	}
	if cfg.NewsAPIKey != "k1" {
		t.Errorf("NewsAPIKey = %q, want k1", cfg.NewsAPIKey)
	}
	if cfg.GNewsAPIKey != "k2" {
		t.Errorf("GNewsAPIKey = %q, want k2", cfg.GNewsAPIKey)
	}
	if cfg.NewsDataAPIKey != "k3" {
		t.Errorf("NewsDataAPIKey = %q, want k3", cfg.NewsDataAPIKey)
	}
	if cfg.WebFetchAPIKey != "k4" {
		t.Errorf("WebFetchAPIKey = %q, want k4", cfg.WebFetchAPIKey)
	}
	if cfg.WebFetchAPIURL != "https://example.com/fetch" {
		t.Errorf("WebFetchAPIURL = %q, want https://example.com/fetch", cfg.WebFetchAPIURL)
	}
}
