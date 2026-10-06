package providers

import (
	"context"
	"fmt"
	"net/url"
	"strings"
	"time"
)

// NewsDataIO is fallback provider #3. It fetches from
// https://newsdata.io/api/1/news (top category).
type NewsDataIO struct {
	// BaseURL overrides the API endpoint; empty uses the production URL.
	// Test seam: lets tests point the provider at an httptest server.
	BaseURL string
}

func (NewsDataIO) Name() string { return "newsdata" }

// endpoint returns the configured base URL or the production default.
func (nd NewsDataIO) endpoint() string {
	if nd.BaseURL != "" {
		return nd.BaseURL
	}
	return "https://newsdata.io/api/1/news"
}

// newsDataResponse mirrors newsdata.io/api/1/news JSON shape. App-level
// errors arrive as HTTP 200 with status="error".
type newsDataResponse struct {
	Status       string `json:"status"`
	TotalResults int    `json:"totalResults"`
	Results      []struct {
		ArticleID   string   `json:"article_id"`
		Title       string   `json:"title"`
		Description string   `json:"description"`
		Content     string   `json:"content"`
		Link        string   `json:"link"`
		ImageURL    string   `json:"image_url"`
		SourceID    string   `json:"source_id"`
		SourceName  string   `json:"source_name"`
		Creator     []string `json:"creator"`
		Category    []string `json:"category"`
		PubDate     string   `json:"pubDate"`
	} `json:"results"`
}

// Fetch queries NewsData.io and normalizes its response. Multiple creators
// are joined with ", " and only the first category is kept — the `news`
// table stores a single author/category string each.
func (nd NewsDataIO) Fetch(ctx context.Context, apiKey, lang string, max int, from time.Time) ([]ProviderArticle, error) {
	params := url.Values{}
	params.Set("apikey", apiKey)
	params.Set("category", "top")
	params.Set("language", lang)
	params.Set("q", "news")
	params.Set("from_date", from.Format("2006-01-02")) // retention window start
	params.Set("page", "1")

	fullURL := fmt.Sprintf("%s?%s", nd.endpoint(), params.Encode())

	var raw newsDataResponse
	if err := fetchJSON(ctx, fullURL, &raw, 30*time.Second); err != nil {
		return nil, fmt.Errorf("newsdata: %w", err)
	}
	if raw.Status != "success" {
		// App-level error inside an HTTP 200 body.
		return nil, fmt.Errorf("newsdata: status=%s", raw.Status)
	}

	out := make([]ProviderArticle, 0, len(raw.Results))
	for _, a := range raw.Results {
		if a.Link == "" {
			continue
		}
		category := ""
		if len(a.Category) > 0 {
			category = a.Category[0]
		}
		author := ""
		if len(a.Creator) > 0 {
			author = strings.Join(a.Creator, ", ")
		}
		out = append(out, ProviderArticle{
			Title:       a.Title,
			Description: a.Description,
			Content:     a.Content,
			URL:         a.Link,
			ImageURL:    a.ImageURL,
			Source:      a.SourceName,
			Author:      author,
			Category:    category,
			PublishedAt: parseTime(a.PubDate),
		})
	}
	return out, nil
}
