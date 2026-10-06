package providers

import (
	"context"
	"fmt"
	"net/url"
	"strconv"
	"time"
)

// NewsAPI is fallback provider #1. It fetches from
// https://newsapi.org/v2/everything — newest first, filtered by language
// and the start of the retention window.
type NewsAPI struct {
	// BaseURL overrides the API endpoint; empty uses the production URL.
	// Test seam: lets tests point the provider at an httptest server.
	BaseURL string
}

func (NewsAPI) Name() string { return "newsapi" }

// endpoint returns the configured base URL or the production default.
func (n NewsAPI) endpoint() string {
	if n.BaseURL != "" {
		return n.BaseURL
	}
	return "https://newsapi.org/v2/everything"
}

// newsAPIResponse mirrors https://newsapi.org/v2/everything's JSON shape.
// App-level errors (bad key, rate limit) arrive as HTTP 200 with
// status="error", so the status field is checked after decoding.
type newsAPIResponse struct {
	Status       string `json:"status"`
	TotalResults int    `json:"totalResults"`
	Articles     []struct {
		Source struct {
			ID   string `json:"id"`
			Name string `json:"name"`
		} `json:"source"`
		Author      string `json:"author"`
		Title       string `json:"title"`
		Description string `json:"description"`
		URL         string `json:"url"`
		UrlToImage  string `json:"urlToImage"`
		PublishedAt string `json:"publishedAt"`
		Content     string `json:"content"`
	} `json:"articles"`
}

// Fetch queries NewsAPI and normalizes its response into ProviderArticle.
// Articles without a URL are dropped — they can never be deduplicated.
func (n NewsAPI) Fetch(ctx context.Context, apiKey, lang string, max int, from time.Time) ([]ProviderArticle, error) {
	params := url.Values{}
	params.Set("apiKey", apiKey)
	params.Set("language", lang)
	params.Set("sortBy", "publishedAt") // newest first
	params.Set("pageSize", strconv.Itoa(max))
	params.Set("from", from.Format("2006-01-02")) // retention window start

	fullURL := fmt.Sprintf("%s?%s", n.endpoint(), params.Encode())

	var raw newsAPIResponse
	if err := fetchJSON(ctx, fullURL, &raw, 30*time.Second); err != nil {
		return nil, fmt.Errorf("newsapi: %w", err)
	}
	if raw.Status != "ok" {
		// App-level error inside an HTTP 200 body.
		return nil, fmt.Errorf("newsapi: status=%s", raw.Status)
	}

	out := make([]ProviderArticle, 0, len(raw.Articles))
	for _, a := range raw.Articles {
		if a.URL == "" {
			continue
		}
		out = append(out, ProviderArticle{
			Title:       a.Title,
			Description: a.Description,
			Content:     a.Content,
			URL:         a.URL,
			ImageURL:    a.UrlToImage,
			Source:      a.Source.Name, // publisher lives in a nested object
			Author:      a.Author,
			PublishedAt: parseTime(a.PublishedAt),
		})
	}
	return out, nil
}

// parseTime converts provider timestamps into time.Time. Handles RFC 3339
// ("2026-09-30T10:00:00Z") and the space-separated variant
// ("2026-09-30 10:00:00") that some providers return. Unparseable input
// falls back to now so the article still lands inside the retention window
// instead of being silently dropped.
func parseTime(s string) time.Time {
	if t, err := time.Parse(time.RFC3339, s); err == nil {
		return t
	}
	if t, err := time.Parse("2006-01-02 15:04:05", s); err == nil {
		return t
	}
	return time.Now().UTC()
}
