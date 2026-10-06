package providers

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"
)

// fakeJSONServer returns an httptest server that responds with the given
// status code and body — the wire-level fake for provider endpoints.
func fakeJSONServer(t *testing.T, status int, body string) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_, _ = w.Write([]byte(body))
	}))
	t.Cleanup(srv.Close)
	return srv
}

// ──── fetchJSON ────

func Test_fetchJSON_decodes_success_response(t *testing.T) {
	// Given — a 200 endpoint returning JSON
	srv := fakeJSONServer(t, http.StatusOK, `{"status":"ok","count":2}`)
	var target struct {
		Status string `json:"status"`
		Count  int    `json:"count"`
	}

	// When
	err := fetchJSON(context.Background(), srv.URL, &target, 5*time.Second)

	// Then
	if err != nil {
		t.Fatalf("fetchJSON: %v", err)
	}
	if target.Status != "ok" || target.Count != 2 {
		t.Errorf("decoded %+v, want status=ok count=2", target)
	}
}

func Test_fetchJSON_errors_on_non_2xx_status(t *testing.T) {
	// Given — a rate-limited endpoint
	srv := fakeJSONServer(t, http.StatusTooManyRequests, `{"message":"rate limited"}`)
	var target struct{ Message string }

	// When
	err := fetchJSON(context.Background(), srv.URL, &target, 5*time.Second)

	// Then — the status code surfaces in the error
	if err == nil {
		t.Fatal("expected error for HTTP 429")
	}
	if !strings.Contains(err.Error(), "429") {
		t.Errorf("error should mention the status code: %v", err)
	}
}

func Test_fetchJSON_errors_on_invalid_json_body(t *testing.T) {
	// Given — a 200 endpoint returning garbage
	srv := fakeJSONServer(t, http.StatusOK, `not-json{`)
	var target struct{}

	// When
	err := fetchJSON(context.Background(), srv.URL, &target, 5*time.Second)

	// Then
	if err == nil {
		t.Fatal("expected error for invalid JSON body")
	}
}

// ──── parseTime ────

func Test_parseTime_parses_RFC3339_with_Z_suffix(t *testing.T) {
	// Given
	in := "2026-09-30T10:00:00Z"

	// When
	got := parseTime(in)

	// Then
	want := time.Date(2026, 9, 30, 10, 0, 0, 0, time.UTC)
	if !got.Equal(want) {
		t.Errorf("parseTime(%q) = %v, want %v", in, got, want)
	}
}

func Test_parseTime_parses_space_separated_format(t *testing.T) {
	// Given — the secondary format NewsData.io returns
	in := "2026-09-30 10:00:00"

	// When
	got := parseTime(in)

	// Then — time.Parse assumes UTC when no zone info is present
	want := time.Date(2026, 9, 30, 10, 0, 0, 0, time.UTC)
	if !got.Equal(want) {
		t.Errorf("parseTime(%q) = %v, want %v", in, got, want)
	}
}

func Test_parseTime_falls_back_to_now_for_unparseable_input(t *testing.T) {
	// Given
	in := "not-a-timestamp"

	// When
	got := parseTime(in)

	// Then — returns a plausible time (close to now), never zero
	if got.IsZero() {
		t.Fatal("parseTime returned zero time; expected now fallback")
	}
	if diff := time.Since(got); diff > time.Minute || diff < -time.Minute {
		t.Errorf("parseTime fallback far from now: %v", got)
	}
}

// ──── NewsAPI ────

func Test_NewsAPI_Fetch_normalizes_articles(t *testing.T) {
	// Given — a fake NewsAPI endpoint returning its real response shape,
	// with one URL-less article that must be dropped.
	published := time.Now().Add(-time.Hour).UTC().Format(time.RFC3339)
	body := fmt.Sprintf(`{"status":"ok","totalResults":2,"articles":[
		{"source":{"id":"bbc","name":"BBC News"},"author":"Jane Doe",
		 "title":"Test headline","description":"desc","url":"https://example.com/a",
		 "urlToImage":"https://example.com/a.jpg","publishedAt":%q,"content":"body"},
		{"source":{"id":"x","name":"NoURL"},"title":"No URL","url":""}]}`,
		published)

	// Server fake that also captures the query params it received, to
	// verify the auth param wiring.
	var gotQuery url.Values
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotQuery = r.URL.Query()
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(body))
	}))
	t.Cleanup(srv.Close)

	p := NewsAPI{BaseURL: srv.URL} // test seam: point at the fake

	// When
	articles, err := p.Fetch(context.Background(), "test-key", "en", 10, time.Now().Add(-24*time.Hour))

	// Then
	if err != nil {
		t.Fatalf("Fetch: %v", err)
	}
	if len(articles) != 1 {
		t.Fatalf("got %d articles, want 1 (URL-less article dropped)", len(articles))
	}
	a := articles[0]
	if a.Title != "Test headline" || a.URL != "https://example.com/a" ||
		a.Source != "BBC News" || a.Author != "Jane Doe" ||
		a.ImageURL != "https://example.com/a.jpg" {
		t.Errorf("normalization mismatch: %+v", a)
	}
	if a.PublishedAt.IsZero() {
		t.Error("PublishedAt should be parsed, got zero")
	}
	if q := gotQuery.Get("apiKey"); q != "test-key" {
		t.Errorf("apiKey param = %q, want test-key", q)
	}
	if q := gotQuery.Get("language"); q != "en" {
		t.Errorf("language param = %q, want en", q)
	}
}

func Test_NewsAPI_Fetch_errors_when_status_not_ok(t *testing.T) {
	// Given — NewsAPI reports app-level errors inside a 200 body
	srv := fakeJSONServer(t, http.StatusOK, `{"status":"error","message":"apiKeyInvalid"}`)
	p := NewsAPI{BaseURL: srv.URL}

	// When
	articles, err := p.Fetch(context.Background(), "bad-key", "en", 10, time.Now())

	// Then
	if err == nil {
		t.Fatal("expected error for status=error")
	}
	if articles != nil {
		t.Errorf("expected no articles, got %d", len(articles))
	}
}

// ──── GNews ────

func Test_GNews_Fetch_normalizes_articles(t *testing.T) {
	// Given
	published := time.Now().Add(-time.Hour).UTC().Format(time.RFC3339)
	body := fmt.Sprintf(`{"articleCount":1,"articles":[
		{"title":"G headline","description":"desc","content":"body",
		 "url":"https://example.com/g","image":"https://example.com/g.jpg",
		 "publishedAt":%q,"author":"John","source":{"name":"Reuters"}}]}`,
		published)
	srv := fakeJSONServer(t, http.StatusOK, body)
	p := GNews{BaseURL: srv.URL}

	// When
	articles, err := p.Fetch(context.Background(), "tok", "en", 10, time.Now().Add(-24*time.Hour))

	// Then
	if err != nil {
		t.Fatalf("Fetch: %v", err)
	}
	if len(articles) != 1 {
		t.Fatalf("got %d articles, want 1", len(articles))
	}
	a := articles[0]
	if a.Title != "G headline" || a.URL != "https://example.com/g" ||
		a.Source != "Reuters" || a.Author != "John" ||
		a.ImageURL != "https://example.com/g.jpg" {
		t.Errorf("normalization mismatch: %+v", a)
	}
}

// ──── NewsData.io ────

func Test_NewsDataIO_Fetch_joins_creators_and_keeps_first_category(t *testing.T) {
	// Given — NewsData returns creator/category as arrays; the schema
	// stores a single string each.
	pubDate := time.Now().Add(-time.Hour).UTC().Format("2006-01-02 15:04:05")
	body := fmt.Sprintf(`{"status":"success","totalResults":1,"results":[
		{"article_id":"x1","title":"ND headline","description":"d","content":"c",
		 "link":"https://example.com/nd","image_url":"https://example.com/nd.jpg",
		 "source_id":"src","source_name":"AP","creator":["A One","A Two"],
		 "category":["sports","world"],"pubDate":%q}]}`,
		pubDate)
	srv := fakeJSONServer(t, http.StatusOK, body)
	p := NewsDataIO{BaseURL: srv.URL}

	// When
	articles, err := p.Fetch(context.Background(), "k", "en", 10, time.Now().Add(-24*time.Hour))

	// Then
	if err != nil {
		t.Fatalf("Fetch: %v", err)
	}
	if len(articles) != 1 {
		t.Fatalf("got %d articles, want 1", len(articles))
	}
	a := articles[0]
	if a.Title != "ND headline" || a.URL != "https://example.com/nd" ||
		a.Source != "AP" || a.ImageURL != "https://example.com/nd.jpg" {
		t.Errorf("normalization mismatch: %+v", a)
	}
	if a.Author != "A One, A Two" {
		t.Errorf("Author = %q, want %q (creators joined)", a.Author, "A One, A Two")
	}
	if a.Category != "sports" {
		t.Errorf("Category = %q, want %q (first category kept)", a.Category, "sports")
	}
}

func Test_NewsDataIO_Fetch_errors_when_status_not_success(t *testing.T) {
	// Given — NewsData reports app-level errors inside a 200 body
	srv := fakeJSONServer(t, http.StatusOK, `{"status":"error"}`)
	p := NewsDataIO{BaseURL: srv.URL}

	// When
	_, err := p.Fetch(context.Background(), "bad-key", "en", 10, time.Now())

	// Then
	if err == nil {
		t.Fatal("expected error for status=error")
	}
}

// ──── WebFetch ────

func Test_WebFetch_Fetch_prefers_image_url_over_image(t *testing.T) {
	// Given — both image fields present
	published := time.Now().Add(-time.Hour).UTC().Format(time.RFC3339)
	body := fmt.Sprintf(`{"articles":[
		{"title":"WF headline","url":"https://example.com/wf",
		 "image_url":"https://example.com/wf1.jpg","image":"https://example.com/wf2.jpg",
		 "source":"Src","author":"Auth","category":"tech","published_at":%q}]}`,
		published)
	srv := fakeJSONServer(t, http.StatusOK, body)
	p := WebFetch{BaseURL: srv.URL}

	// When
	articles, err := p.Fetch(context.Background(), "k", "en", 10, time.Now().Add(-24*time.Hour))

	// Then
	if err != nil {
		t.Fatalf("Fetch: %v", err)
	}
	if len(articles) != 1 {
		t.Fatalf("got %d articles, want 1", len(articles))
	}
	a := articles[0]
	if a.ImageURL != "https://example.com/wf1.jpg" {
		t.Errorf("ImageURL = %q, want the image_url field value", a.ImageURL)
	}
	if a.Category != "tech" {
		t.Errorf("Category = %q, want tech", a.Category)
	}
}

func Test_WebFetch_Fetch_falls_back_to_image_field(t *testing.T) {
	// Given — no image_url, only image
	published := time.Now().Add(-time.Hour).UTC().Format(time.RFC3339)
	body := fmt.Sprintf(`{"articles":[
		{"title":"WF headline","url":"https://example.com/wf",
		 "image":"https://example.com/wf2.jpg","published_at":%q}]}`,
		published)
	srv := fakeJSONServer(t, http.StatusOK, body)
	p := WebFetch{BaseURL: srv.URL}

	// When
	articles, err := p.Fetch(context.Background(), "k", "en", 10, time.Now().Add(-24*time.Hour))

	// Then
	if err != nil {
		t.Fatalf("Fetch: %v", err)
	}
	if len(articles) != 1 || articles[0].ImageURL != "https://example.com/wf2.jpg" {
		t.Fatalf("expected image fallback, got %+v", articles)
	}
}

func Test_WebFetch_Fetch_errors_without_base_url(t *testing.T) {
	// Given — an unconfigured WebFetch provider
	p := WebFetch{}

	// When
	_, err := p.Fetch(context.Background(), "k", "en", 10, time.Now())

	// Then
	if err == nil {
		t.Fatal("expected error for missing BaseURL")
	}
}
