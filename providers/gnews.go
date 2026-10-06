package providers

import (
	"context"
	"fmt"
	"net/url"
	"time"
)

// GNews is fallback provider #2. It fetches from
// https://gnews.io/v4/api/top-headlines (general category).
type GNews struct {
	// BaseURL overrides the API endpoint; empty uses the production URL.
	// Test seam: lets tests point the provider at an httptest server.
	BaseURL string
}

func (GNews) Name() string { return "gnews" }

// endpoint returns the configured base URL or the production default.
func (g GNews) endpoint() string {
	if g.BaseURL != "" {
		return g.BaseURL
	}
	return "https://gnews.io/v4/api/top-headlines"
}

// gnewsResponse mirrors gnews.io/v4 top-headlines JSON shape.
type gnewsResponse struct {
	ArticleCount int `json:"articleCount"`
	Articles     []struct {
		Title       string `json:"title"`
		Description string `json:"description"`
		Content     string `json:"content"`
		URL         string `json:"url"`
		Image       string `json:"image"`
		PublishedAt string `json:"publishedAt"`
		Author      string `json:"author"`
		Source      struct {
			Name string `json:"name"`
		} `json:"source"`
	} `json:"articles"`
}

// Fetch queries GNews and normalizes its response. The from parameter is
// accepted for interface parity but unused: top-headlines is always
// current news, not a windowed search.
func (g GNews) Fetch(ctx context.Context, apiKey, lang string, max int, from time.Time) ([]ProviderArticle, error) {
	params := url.Values{}
	params.Set("category", "general")
	params.Set("lang", lang)
	params.Set("max", fmt.Sprintf("%d", max))
	params.Set("token", apiKey) // gnews authenticates via the token query param

	fullURL := fmt.Sprintf("%s?%s", g.endpoint(), params.Encode())

	var raw gnewsResponse
	if err := fetchJSON(ctx, fullURL, &raw, 30*time.Second); err != nil {
		return nil, fmt.Errorf("gnews: %w", err)
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
			ImageURL:    a.Image,
			Source:      a.Source.Name,
			Author:      a.Author,
			PublishedAt: parseTime(a.PublishedAt),
		})
	}
	return out, nil
}
