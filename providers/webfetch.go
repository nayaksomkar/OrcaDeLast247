package providers

import (
	"context"
	"fmt"
	"net/url"
	"strconv"
	"time"
)

// WebFetch is fallback provider #4: a configurable generic endpoint. It
// expects WEBFETCH_API_URL as the base URL and appends query parameters
// for the search. Note the response uses snake_case "published_at"
// (unlike the other providers' "publishedAt").
type WebFetch struct {
	// BaseURL is the endpoint to query. Unlike the other providers there
	// is no production default — an empty BaseURL is a configuration error.
	BaseURL string
}

func (WebFetch) Name() string { return "webfetch" }

// webFetchResponse mirrors the expected fallback endpoint JSON shape.
type webFetchResponse struct {
	Articles []struct {
		Title       string `json:"title"`
		Description string `json:"description"`
		Content     string `json:"content"`
		URL         string `json:"url"`
		ImageURL    string `json:"image_url"`
		Image       string `json:"image"`
		Source      string `json:"source"`
		Author      string `json:"author"`
		Category    string `json:"category"`
		PublishedAt string `json:"published_at"`
	} `json:"articles"`
}

// Fetch queries the configured endpoint and normalizes its response.
// image_url is preferred over image when both are present.
func (wf WebFetch) Fetch(ctx context.Context, apiKey, lang string, max int, from time.Time) ([]ProviderArticle, error) {
	if wf.BaseURL == "" {
		return nil, fmt.Errorf("webfetch: WEBFETCH_API_URL is not configured")
	}

	params := url.Values{}
	params.Set("q", "news")
	params.Set("lang", lang)
	params.Set("max", strconv.Itoa(max))
	params.Set("from", from.Format("2006-01-02")) // retention window start
	if apiKey != "" {
		// The token is optional: the endpoint may be open.
		params.Set("token", apiKey)
	}

	fullURL := fmt.Sprintf("%s?%s", wf.BaseURL, params.Encode())

	var raw webFetchResponse
	if err := fetchJSON(ctx, fullURL, &raw, 30*time.Second); err != nil {
		return nil, fmt.Errorf("webfetch: %w", err)
	}

	out := make([]ProviderArticle, 0, len(raw.Articles))
	for _, a := range raw.Articles {
		if a.URL == "" {
			continue
		}
		image := a.ImageURL
		if image == "" {
			image = a.Image
		}
		out = append(out, ProviderArticle{
			Title:       a.Title,
			Description: a.Description,
			Content:     a.Content,
			URL:         a.URL,
			ImageURL:    image,
			Source:      a.Source,
			Author:      a.Author,
			Category:    a.Category,
			PublishedAt: parseTime(a.PublishedAt),
		})
	}
	return out, nil
}
