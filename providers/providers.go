// Package providers implements news source fetchers behind a common
// interface. Each provider normalizes its upstream JSON into
// ProviderArticle; the ingestion service (package main) then deduplicates
// and stores the result.
package providers

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"
)

// ProviderArticle is the normalized intermediate article format shared by
// all providers. Its fields map 1:1 to the `news` table columns (see db.go
// in the root package).
type ProviderArticle struct {
	Title       string // required — articles without a title are dropped downstream
	Description string
	Content     string
	URL         string // required — the deduplication key
	ImageURL    string
	Source      string // publisher name, e.g. "BBC News"
	Author      string
	Category    string
	PublishedAt time.Time
}

// Provider is the contract every news source implements. Name() must be
// stable: the ingestion service uses it to dispatch API keys and reports
// it in the ingestion summary.
type Provider interface {
	Name() string
	Fetch(ctx context.Context, apiKey string, lang string, max int, from time.Time) ([]ProviderArticle, error)
}

// fetchJSON performs an HTTP GET and decodes the JSON body into target.
// It fails on transport errors, non-2xx statuses, and malformed JSON —
// the ingestion service then falls through to the next provider in the
// fallback chain.
func fetchJSON(ctx context.Context, url string, target any, timeout time.Duration) error {
	// Per-call client: the timeout bounds the whole exchange (connect +
	// read), independent of the caller's context deadline.
	client := &http.Client{Timeout: timeout}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return fmt.Errorf("build request: %w", err)
	}
	// Some providers reject requests without a User-Agent.
	req.Header.Set("User-Agent", "Last247-Ingestion/1.0")

	resp, err := client.Do(req)
	if err != nil {
		return fmt.Errorf("http request: %w", err)
	}
	defer resp.Body.Close()

	// Read the full body first; the status check below decides how it is
	// used (decoded or discarded).
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return fmt.Errorf("read body: %w", err)
	}

	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("provider returned HTTP %d", resp.StatusCode)
	}

	if err := json.Unmarshal(body, target); err != nil {
		return fmt.Errorf("decode JSON: %w", err)
	}
	return nil
}
