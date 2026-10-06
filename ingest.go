package main

import (
	"context"
	"database/sql"
	"fmt"
	"time"

	"last247/providers"
)

// IngestionResult summarizes one ingestion run for the final report.
type IngestionResult struct {
	Provider   string // provider that supplied the articles ("" if none succeeded)
	Total      int    // articles returned by the provider, before dedup
	Inserted   int    // rows inserted or updated in the news table
	Skipped    int    // articles dropped: invalid, duplicate URL, or failed insert
	Deleted    int    // stale articles removed by the retention sweep
	SourceTime string // when the provider was queried (RFC 3339)
}

// runIngestion executes one full collection cycle:
//
//  1. Determine the retention window (from = now - RetentionDays).
//  2. Build the eligible provider list (only ones with API keys).
//  3. Try providers in order; the first that returns usable articles wins.
//  4. Normalize into Article, deduplicate by URL, upsert into the DB.
//  5. Delete articles published outside the retention window.
//
// All providers failing is not an error: it yields a result with Total == 0
// (main turns that into a non-zero exit code).
func runIngestion(ctx context.Context, cfg *Config, db *sql.DB) (*IngestionResult, error) {
	// Start of the retention window: only articles published after this
	// point are kept; anything older is swept at the end of the run.
	from := time.Now().AddDate(0, 0, -cfg.RetentionDays)
	result := &IngestionResult{
		SourceTime: time.Now().UTC().Format(time.RFC3339),
	}

	// Build the ordered provider list. Only providers with an API key are
	// eligible. The WebFetch provider needs its BaseURL from config.
	providerList := buildProviders(cfg)

	var articles []providers.ProviderArticle
	var usedProvider string

	// Sequential fallback: stop on the first provider that returns usable
	// articles. Never fan out — later providers run only when the earlier
	// one fails, is rate-limited, or returns nothing.
	for _, p := range providerList {
		fetched, err := p.Fetch(ctx, providerAPIKey(cfg, p.Name()), cfg.Language, cfg.MaxArticles, from)
		if err != nil {
			fmt.Printf("[warn] %s failed: %v\n", p.Name(), err)
			continue
		}
		if len(fetched) == 0 {
			fmt.Printf("[warn] %s returned 0 articles\n", p.Name())
			continue
		}
		articles = fetched
		usedProvider = p.Name()
		break // sequential fallback: stop on first success
	}

	result.Provider = usedProvider
	if articles == nil {
		// Every provider failed, was unconfigured, or returned nothing.
		result.Total = 0
		return result, nil
	}
	result.Total = len(articles)

	// Normalize + deduplicate by URL, then upsert each article.
	// seen tracks URLs already handled in this run; the DB's UNIQUE(url)
	// constraint is the second line of defense for cross-run duplicates.
	now := time.Now().UTC()
	seen := make(map[string]bool)
	var inserted, skipped int

	for _, pa := range articles {
		// An article without a title or URL can never be stored or
		// displayed — drop it and count it as skipped.
		if pa.URL == "" || pa.Title == "" {
			skipped++
			continue
		}
		// Same story appearing twice within one provider response.
		if seen[pa.URL] {
			skipped++
			continue
		}
		seen[pa.URL] = true

		a := &Article{
			ID:          ArticleID(pa.URL),
			Title:       pa.Title,
			Description: pa.Description,
			Content:     pa.Content,
			URL:         pa.URL,
			ImageURL:    pa.ImageURL,
			Source:      pa.Source,
			Author:      pa.Author,
			Category:    pa.Category,
			PublishedAt: pa.PublishedAt,
			FetchedAt:   now,
			Provider:    usedProvider,
		}

		if err := UpsertArticle(ctx, db, a); err != nil {
			fmt.Printf("[warn] failed to store article %s: %v\n", a.URL, err)
			skipped++
			continue
		}
		inserted++
	}

	result.Inserted = inserted
	result.Skipped = skipped

	// Retention sweep: remove articles published outside the window. A
	// failed sweep is logged but does not fail the run — the stored data
	// is still valid, just one run larger than the window.
	deleted, err := DeleteStaleArticles(ctx, db, cfg.RetentionDays)
	if err != nil {
		fmt.Printf("[warn] cleanup failed: %v\n", err)
	}
	result.Deleted = deleted

	return result, nil
}

// buildProviders constructs the ordered provider fallback list from config.
// A provider is eligible only when its API key (or, for WebFetch, its base
// URL) is configured; everything else is left out of the run entirely.
func buildProviders(cfg *Config) []providers.Provider {
	var list []providers.Provider

	if cfg.NewsAPIKey != "" {
		list = append(list, providers.NewsAPI{})
	}
	if cfg.GNewsAPIKey != "" {
		list = append(list, providers.GNews{})
	}
	if cfg.NewsDataAPIKey != "" {
		list = append(list, providers.NewsDataIO{})
	}
	if cfg.WebFetchAPIURL != "" {
		list = append(list, providers.WebFetch{BaseURL: cfg.WebFetchAPIURL})
	}

	return list
}

// providerAPIKey returns the API key for a given provider name. Unknown
// names get "" (no key), which providers surface as an auth error.
func providerAPIKey(cfg *Config, name string) string {
	switch name {
	case "newsapi":
		return cfg.NewsAPIKey
	case "gnews":
		return cfg.GNewsAPIKey
	case "newsdata":
		return cfg.NewsDataAPIKey
	case "webfetch":
		return cfg.WebFetchAPIKey
	}
	return ""
}
