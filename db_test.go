package main

import (
	"context"
	"database/sql"
	"os"
	"testing"
	"time"
)

func testDB(t *testing.T) *sql.DB {
	t.Helper()
	ctx := context.Background()
	db, err := OpenDB(ctx, &Config{
		TursoURL: "file:" + t.TempDir() + "/test.db",
	})
	if err != nil {
		t.Fatalf("OpenDB: %v", err)
	}
	if err := InitDB(ctx, db); err != nil {
		t.Fatalf("InitDB: %v", err)
	}
	return db
}

func TestArticleID_Deterministic(t *testing.T) {
	id1 := ArticleID("https://example.com/story")
	id2 := ArticleID("https://example.com/story")
	if id1 != id2 {
		t.Fatalf("ArticleID should be deterministic, got %s and %s", id1, id2)
	}
	if len(id1) != 16 {
		t.Fatalf("ArticleID should be 16 chars, got %d", len(id1))
	}
}

func TestUpsertAndCount(t *testing.T) {
	db := testDB(t)
	ctx := context.Background()

	article := &Article{
		ID:          ArticleID("https://example.com/1"),
		Title:       "Test Article",
		Description: "A test",
		Content:     "Test content",
		URL:         "https://example.com/1",
		ImageURL:    "https://example.com/img.jpg",
		Source:      "TestSource",
		Author:      "Test Author",
		Category:    "Technology",
		PublishedAt: time.Now().Add(-1 * time.Hour).UTC(),
		FetchedAt:   time.Now().UTC(),
		Provider:    "newsapi",
	}

	if err := UpsertArticle(ctx, db, article); err != nil {
		t.Fatalf("upsert: %v", err)
	}

	count, err := CountArticles(ctx, db)
	if err != nil {
		t.Fatalf("count: %v", err)
	}
	if count != 1 {
		t.Fatalf("expected 1 article, got %d", count)
	}

	// Same URL should not duplicate.
	if err := UpsertArticle(ctx, db, article); err != nil {
		t.Fatalf("upsert duplicate: %v", err)
	}

	count, err = CountArticles(ctx, db)
	if err != nil {
		t.Fatalf("count after duplicate: %v", err)
	}
	if count != 1 {
		t.Fatalf("expected 1 after duplicate, got %d", count)
	}
}

func TestDeleteStaleArticles(t *testing.T) {
	db := testDB(t)
	ctx := context.Background()

	old := &Article{
		ID:          ArticleID("https://example.com/old"),
		Title:       "Old Article",
		URL:         "https://example.com/old",
		PublishedAt: time.Now().Add(-10 * 24 * time.Hour).UTC(),
		FetchedAt:   time.Now().Add(-10 * 24 * time.Hour).UTC(),
		Provider:    "newsapi",
		Source:      "TestSource",
	}
	fresh := &Article{
		ID:          ArticleID("https://example.com/fresh"),
		Title:       "Fresh Article",
		URL:         "https://example.com/fresh",
		PublishedAt: time.Now().Add(-1 * time.Hour).UTC(),
		FetchedAt:   time.Now().UTC(),
		Provider:    "newsapi",
		Source:      "TestSource",
	}

	if err := UpsertArticle(ctx, db, old); err != nil {
		t.Fatalf("upsert old: %v", err)
	}
	if err := UpsertArticle(ctx, db, fresh); err != nil {
		t.Fatalf("upsert fresh: %v", err)
	}

	deleted, err := DeleteStaleArticles(ctx, db, 7)
	if err != nil {
		t.Fatalf("delete stale: %v", err)
	}
	if deleted != 1 {
		t.Fatalf("expected 1 stale article deleted, got %d", deleted)
	}

	count, _ := CountArticles(ctx, db)
	if count != 1 {
		t.Fatalf("expected 1 remaining, got %d", count)
	}
}

func TestMain(m *testing.M) {
	// Ensure the sqlite driver is registered for local file tests.
	os.Exit(m.Run())
}
