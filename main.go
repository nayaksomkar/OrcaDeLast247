// Command last247-ingest fetches news from configured providers using sequential
// fallback (NewsAPI → GNews → NewsData.io → WebFetch), normalizes and
// deduplicates articles, and stores them in a Turso database.
//
// The service is designed to be invoked periodically (e.g., via cron or a
// scheduled job) 3–4 times per day. Each invocation performs one full
// collection cycle and then exits.
//
// Usage:
//
//	go run .                          # one ingestion cycle
//	go build -o ingest . && ./ingest  # build once, run repeatedly
//	./ingest -timeout 60s -retention 14
//
// Configuration comes from environment variables, optionally loaded from a
// .env file in the working directory. See .env.example and README.md for the
// full variable list.
package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"os"
	"os/signal"
	"syscall"
	"time"
)

func main() {
	// CLI flags. Both optional; -retention overrides env RETENTION_DAYS.
	var (
		timeout   = flag.Duration("timeout", 120*time.Second, "overall ingestion timeout")
		retention = flag.Int("retention", 0, "override retention window in days (default: from env RETENTION_DAYS or 7)")
	)
	flag.Parse()

	// Cancel the context on SIGINT/SIGTERM so in-flight HTTP requests and
	// DB writes abort cleanly instead of leaving partial state behind.
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	// Layer an overall deadline on top of the signal context: no single
	// run may exceed -timeout, whatever the providers are doing.
	ctx, cancel := context.WithTimeout(ctx, *timeout)
	defer cancel()

	cfg, err := LoadConfig()
	if err != nil {
		log.Fatalf("config error: %v", err)
	}

	// The -retention flag wins over env RETENTION_DAYS when set (> 0).
	if *retention > 0 {
		cfg.RetentionDays = *retention
	}

	db, err := OpenDB(ctx, cfg)
	if err != nil {
		log.Fatalf("database error: %v", err)
	}
	defer db.Close()

	// Idempotent: creates the news table + indexes only if missing, so it
	// is safe to call on every run.
	if err := InitDB(ctx, db); err != nil {
		log.Fatalf("schema error: %v", err)
	}

	start := time.Now()
	result, err := runIngestion(ctx, cfg, db)
	if err != nil {
		log.Fatalf("ingestion error: %v", err)
	}

	// Post-run snapshot of the whole table for the summary line.
	total, _ := CountArticles(ctx, db)
	elapsed := time.Since(start).Round(time.Second)

	fmt.Printf("\n=== Ingestion Summary ===\n")
	fmt.Printf("Provider:   %s\n", result.Provider)
	fmt.Printf("Fetched:    %d articles\n", result.Total)
	fmt.Printf("Inserted:   %d articles\n", result.Inserted)
	fmt.Printf("Skipped:    %d articles\n", result.Skipped)
	fmt.Printf("Deleted:    %d stale articles\n", result.Deleted)
	fmt.Printf("DB total:   %d articles\n", total)
	fmt.Printf("Elapsed:    %s\n", elapsed)
	fmt.Printf("=========================\n")

	// Exit non-zero when nothing was fetched so schedulers (cron + mail,
	// CI jobs, healthchecks) can detect a fully-failed run.
	if result.Total == 0 {
		fmt.Println("No articles fetched from any provider.")
		os.Exit(1)
	}
}
