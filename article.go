package main

import "time"

// Article is the normalized news record stored in the Turso `news` table.
// It is also the shape served to the Last247 frontend by /api/news, so the
// stored data is the single source of truth for both UI and a future LLM
// Brain.
type Article struct {
	ID          string    `json:"id" db:"id"`       // deterministic SHA-256 hash of URL (see ArticleID)
	Title       string    `json:"title" db:"title"` // required
	Description string    `json:"description,omitempty" db:"description"`
	Content     string    `json:"content,omitempty" db:"content"`
	URL         string    `json:"url" db:"url"` // required; UNIQUE — the deduplication key
	ImageURL    string    `json:"image_url,omitempty" db:"image_url"`
	Source      string    `json:"source,omitempty" db:"source"` // publisher name, e.g. "BBC News"
	Author      string    `json:"author,omitempty" db:"author"`
	Category    string    `json:"category,omitempty" db:"category"`
	PublishedAt time.Time `json:"published_at" db:"published_at"` // when the article was published
	FetchedAt   time.Time `json:"fetched_at" db:"fetched_at"`     // when this service stored it
	Provider    string    `json:"provider" db:"provider"`         // provider that supplied it
}
