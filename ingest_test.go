package main

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

// fakeWebfetchServer spins up an HTTP server that answers WebFetch.Fetch
// with the given status code and body — the wire-level fake for the
// provider HTTP contract. No real network, no API keys.
func fakeWebfetchServer(t *testing.T, status int, payload string) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_, _ = w.Write([]byte(payload))
	}))
	t.Cleanup(srv.Close)
	return srv
}

func Test_buildProviders_orders_providers_by_fallback_priority(t *testing.T) {
	// Given — all four providers configured
	cfg := &Config{
		NewsAPIKey:     "k1",
		GNewsAPIKey:    "k2",
		NewsDataAPIKey: "k3",
		WebFetchAPIURL: "https://example.com/fetch",
	}

	// When
	list := buildProviders(cfg)

	// Then — order matters: NewsAPI → GNews → NewsData.io → WebFetch
	want := []string{"newsapi", "gnews", "newsdata", "webfetch"}
	if len(list) != len(want) {
		t.Fatalf("got %d providers, want %d", len(list), len(want))
	}
	for i, p := range list {
		if p.Name() != want[i] {
			t.Errorf("provider[%d] = %q, want %q", i, p.Name(), want[i])
		}
	}
}

func Test_buildProviders_skips_unconfigured_providers(t *testing.T) {
	// Given — only GNews has a key
	cfg := &Config{GNewsAPIKey: "k2"}

	// When
	list := buildProviders(cfg)

	// Then
	if len(list) != 1 || list[0].Name() != "gnews" {
		t.Fatalf("expected only [gnews], got %d providers", len(list))
	}
}

func Test_providerAPIKey_maps_names_to_keys(t *testing.T) {
	// Given — one key per provider
	cfg := &Config{
		NewsAPIKey:     "k1",
		GNewsAPIKey:    "k2",
		NewsDataAPIKey: "k3",
		WebFetchAPIKey: "k4",
	}

	// When / Then — each name resolves to its own key; unknown names get ""
	cases := map[string]string{
		"newsapi":  "k1",
		"gnews":    "k2",
		"newsdata": "k3",
		"webfetch": "k4",
		"unknown":  "",
	}
	for name, want := range cases {
		if got := providerAPIKey(cfg, name); got != want {
			t.Errorf("providerAPIKey(%q) = %q, want %q", name, got, want)
		}
	}
}

func Test_runIngestion_fetches_from_provider_and_stores_deduplicated_articles(t *testing.T) {
	// Given — a provider fake returning 4 articles: one duplicate URL,
	// one missing a title, and two valid ones. published_at is generated
	// relative to now so the articles stay inside the retention window
	// regardless of when the test runs.
	published := time.Now().Add(-time.Hour).UTC().Format(time.RFC3339)
	payload := fmt.Sprintf(`{"articles":[
		{"title":"Story A","url":"https://example.com/a","source":"Src","published_at":%q},
		{"title":"Story A dup","url":"https://example.com/a","source":"Src","published_at":%q},
		{"title":"","url":"https://example.com/no-title","source":"Src","published_at":%q},
		{"title":"Story B","url":"https://example.com/b","source":"Src","published_at":%q}
	]}`, published, published, published, published)
	srv := fakeWebfetchServer(t, http.StatusOK, payload)

	db := testDB(t)
	cfg := &Config{
		RetentionDays:  7,
		Language:       "en",
		MaxArticles:    50,
		WebFetchAPIURL: srv.URL, // only webfetch configured → it is the run's provider
	}

	// When
	result, err := runIngestion(context.Background(), cfg, db)

	// Then — dup and invalid articles are skipped, valid ones stored
	if err != nil {
		t.Fatalf("runIngestion: %v", err)
	}
	if result.Provider != "webfetch" {
		t.Errorf("Provider = %q, want %q", result.Provider, "webfetch")
	}
	if result.Total != 4 {
		t.Errorf("Total = %d, want 4 (fetched before dedup)", result.Total)
	}
	if result.Inserted != 2 {
		t.Errorf("Inserted = %d, want 2", result.Inserted)
	}
	if result.Skipped != 2 {
		t.Errorf("Skipped = %d, want 2 (one dup + one invalid)", result.Skipped)
	}
	total, err := CountArticles(context.Background(), db)
	if err != nil {
		t.Fatalf("CountArticles: %v", err)
	}
	if total != 2 {
		t.Errorf("DB total = %d, want 2", total)
	}
}

func Test_runIngestion_deletes_stale_articles_outside_retention_window(t *testing.T) {
	// Given — one article already stored that is older than the window
	db := testDB(t)
	ctx := context.Background()

	old := &Article{
		ID:          ArticleID("https://example.com/old"),
		Title:       "Old Article",
		URL:         "https://example.com/old",
		PublishedAt: time.Now().Add(-10 * 24 * time.Hour).UTC(),
		FetchedAt:   time.Now().Add(-10 * 24 * time.Hour).UTC(),
		Provider:    "newsapi",
	}
	if err := UpsertArticle(ctx, db, old); err != nil {
		t.Fatalf("upsert old: %v", err)
	}

	// And a provider fake returning one fresh article
	published := time.Now().Add(-time.Hour).UTC().Format(time.RFC3339)
	payload := fmt.Sprintf(`{"articles":[{"title":"Fresh","url":"https://example.com/fresh","published_at":%q}]}`, published)
	srv := fakeWebfetchServer(t, http.StatusOK, payload)
	cfg := &Config{
		RetentionDays:  7,
		Language:       "en",
		MaxArticles:    50,
		WebFetchAPIURL: srv.URL,
	}

	// When
	result, err := runIngestion(ctx, cfg, db)

	// Then — the stale row is swept, the fresh one stored
	if err != nil {
		t.Fatalf("runIngestion: %v", err)
	}
	if result.Deleted != 1 {
		t.Errorf("Deleted = %d, want 1", result.Deleted)
	}
	total, err := CountArticles(ctx, db)
	if err != nil {
		t.Fatalf("CountArticles: %v", err)
	}
	if total != 1 {
		t.Errorf("DB total = %d, want 1 (fresh only)", total)
	}
}

func Test_runIngestion_returns_empty_result_when_all_providers_fail(t *testing.T) {
	// Given — the only configured provider returns HTTP 500
	db := testDB(t)
	srv := fakeWebfetchServer(t, http.StatusInternalServerError, `{"error":"boom"}`)
	cfg := &Config{
		RetentionDays:  7,
		WebFetchAPIURL: srv.URL,
	}

	// When
	result, err := runIngestion(context.Background(), cfg, db)

	// Then — all providers failing is an empty result, not an error
	if err != nil {
		t.Fatalf("runIngestion: %v", err)
	}
	if result.Provider != "" {
		t.Errorf("Provider = %q, want empty", result.Provider)
	}
	if result.Total != 0 {
		t.Errorf("Total = %d, want 0", result.Total)
	}
}

func Test_runIngestion_returns_empty_result_when_no_providers_configured(t *testing.T) {
	// Given — no provider keys or URLs at all
	db := testDB(t)
	cfg := &Config{RetentionDays: 7}

	// When
	result, err := runIngestion(context.Background(), cfg, db)

	// Then — not an error; just an empty run
	if err != nil {
		t.Fatalf("runIngestion: %v", err)
	}
	if result.Provider != "" || result.Total != 0 {
		t.Errorf("expected empty result, got provider=%q total=%d", result.Provider, result.Total)
	}
}
